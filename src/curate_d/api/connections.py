"""Provider connections: their health, and authorizing or re-authorizing an account.

Authorizing is two requests. Starting one returns the provider's consent page and sets a
cookie holding the browser nonce; the provider sends the browser back to the callback,
which must present that cookie. Re-authorizing an account is the same flow: the account
the provider names on the callback is stored over its old tokens.

No response carries a token: the models here are built from `ConnectionHealth`, which
has no credential field.
"""

from collections.abc import AsyncGenerator, Callable, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Annotated, Final, Protocol

import httpx
from fastapi import APIRouter, Cookie, Depends, Request, Response, status
from pydantic import AwareDatetime, BaseModel, SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from curate_d.api.errors import REJECTED, UNAVAILABLE, RejectedError, UnavailableError, problems
from curate_d.config import Settings
from curate_d.domain import ConnectionHealth, ConnectionState, Provider
from curate_d.providers.authorization import (
    AuthorizationNotConfiguredError,
    AuthorizationStateError,
    AuthorizationStates,
    begin_pcloud_authorization,
    complete_pcloud_authorization,
    pcloud_oauth_config,
)
from curate_d.providers.connections import ConnectionRepository
from curate_d.providers.crypto import CredentialCipher, CredentialEncryptionError
from curate_d.providers.errors import ProviderError
from curate_d.providers.health import assess_health

NONCE_COOKIE: Final = "curate_d_authorization"


class ConnectionStatus(BaseModel):
    """A connection's health, and how to restore it when it needs a person."""

    provider: Provider
    account: str
    state: ConnectionState
    expires_at: AwareDatetime | None = None
    requires_attention: bool
    detail: str | None = None
    reauthorize: str | None = None
    """Where to start re-authorizing, when the connection needs it."""


class AuthorizationStarted(BaseModel):
    """Where to send the browser to grant access."""

    provider: Provider
    redirect_url: str


class StartedAuthorization(BaseModel):
    """What a backend hands back when an authorization starts."""

    redirect_url: str
    browser_nonce: SecretStr


class ConnectionsBackend(Protocol):
    """Connections as the control plane sees them."""

    async def health(self) -> Sequence[ConnectionHealth]:
        """Every connection's health now."""
        ...

    def begin(self, provider: Provider) -> StartedAuthorization:
        """Start authorizing an account at `provider`."""
        ...

    async def complete(
        self,
        provider: Provider,
        *,
        code: str,
        state: str,
        hostname: str,
        browser_nonce: str | None,
    ) -> ConnectionHealth:
        """Finish an authorization and return the connection it stored."""
        ...


def authorization_path(provider: Provider) -> str:
    """Where authorizing an account at `provider` starts."""
    return f"/connections/{provider.value}/authorizations"


def status_of(health: ConnectionHealth) -> ConnectionStatus:
    """The response for one connection."""
    return ConnectionStatus(
        **health.model_dump(),
        reauthorize=authorization_path(health.provider) if health.requires_attention else None,
    )


def router(backend: Callable[[Request], ConnectionsBackend]) -> APIRouter:
    """The connection endpoints over `backend`."""
    routes = APIRouter(prefix="/connections", tags=["connections"])
    uses = Depends(backend)

    @routes.get("", response_model=list[ConnectionStatus])
    async def list_connections(
        connections: Annotated[ConnectionsBackend, uses],
    ) -> list[ConnectionStatus]:
        """Every connection, with what needs a person flagged and the way to restore it."""
        return [status_of(health) for health in await connections.health()]

    @routes.post(
        "/{provider}/authorizations",
        status_code=status.HTTP_201_CREATED,
        response_model=AuthorizationStarted,
        responses=problems(REJECTED, UNAVAILABLE),
    )
    async def start_authorization(
        provider: Provider, response: Response, connections: Annotated[ConnectionsBackend, uses]
    ) -> AuthorizationStarted:
        """Start authorizing, or re-authorizing, an account; the browser goes to the URL."""
        started = connections.begin(provider)
        response.set_cookie(
            NONCE_COOKIE,
            started.browser_nonce.get_secret_value(),
            path="/connections",
            httponly=True,
            secure=True,
            samesite="lax",
        )
        return AuthorizationStarted(provider=provider, redirect_url=started.redirect_url)

    @routes.get(
        "/{provider}/callback",
        response_model=ConnectionStatus,
        responses=problems(REJECTED, UNAVAILABLE),
    )
    async def finish_authorization(
        provider: Provider,
        code: str,
        state: str,
        hostname: str,
        response: Response,
        connections: Annotated[ConnectionsBackend, uses],
        nonce: Annotated[str | None, Cookie(alias=NONCE_COOKIE)] = None,
    ) -> ConnectionStatus:
        """Where the provider sends the browser back; stores the connection."""
        health = await connections.complete(
            provider,
            code=code,
            state=state,
            hostname=hostname,
            browser_nonce=nonce,
        )
        response.delete_cookie(NONCE_COOKIE, path="/connections")
        return status_of(health)

    return routes


class LedgerConnections:
    """Connections from the ledger, authorized against the providers."""

    def __init__(
        self,
        settings: Settings,
        engine: AsyncEngine,
        http: httpx.AsyncClient,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        """Bind the settings, ledger, HTTP client for token exchange, and clock."""
        self._settings = settings
        self._engine = engine
        self._http = http
        self._clock = clock

    def _cipher(self) -> CredentialCipher:
        """The credential cipher.

        Raises:
            UnavailableError: no credential key is configured.
        """
        try:
            return CredentialCipher.from_settings(self._settings)
        except CredentialEncryptionError as error:
            raise UnavailableError(str(error)) from None

    def _states(self) -> AuthorizationStates:
        """OAuth states under the credential key."""
        return AuthorizationStates(
            self._cipher(), max_age_seconds=self._settings.oauth_state_max_age_seconds
        )

    async def health(self) -> Sequence[ConnectionHealth]:
        """Every connection's health now."""
        window = timedelta(minutes=self._settings.connection_refresh_window_minutes)
        now = self._clock()
        async with self._engine.connect() as connection:
            records = await ConnectionRepository(connection, self._cipher()).list_all()
        return [assess_health(record, now=now, window=window) for record in records]

    def begin(self, provider: Provider) -> StartedAuthorization:
        """Start authorizing a pCloud account.

        Raises:
            UnavailableError: pCloud authorization or the credential key is not configured.
        """
        try:
            pending = begin_pcloud_authorization(
                pcloud_oauth_config(self._settings), self._states()
            )
        except AuthorizationNotConfiguredError as error:
            raise UnavailableError(str(error)) from None
        return StartedAuthorization(
            redirect_url=pending.redirect_url, browser_nonce=pending.browser_nonce
        )

    async def complete(
        self,
        provider: Provider,
        *,
        code: str,
        state: str,
        hostname: str,
        browser_nonce: str | None,
    ) -> ConnectionHealth:
        """Finish a pCloud authorization and store the connection.

        Raises:
            RejectedError: the state or browser nonce is not acceptable.
            UnavailableError: authorization is not configured, or pCloud refused or could
                not be reached; the message names the provider, account and operation.
        """
        cipher = self._cipher()
        try:
            config = pcloud_oauth_config(self._settings)
            async with self._engine.begin() as connection:
                stored = await complete_pcloud_authorization(
                    config,
                    self._states(),
                    self._http,
                    ConnectionRepository(connection, cipher),
                    code=code,
                    state=state,
                    hostname=hostname,
                    browser_nonce=browser_nonce,
                )
        except AuthorizationStateError as error:
            raise RejectedError(str(error)) from None
        except (AuthorizationNotConfiguredError, ProviderError) as error:
            raise UnavailableError(str(error)) from None
        window = timedelta(minutes=self._settings.connection_refresh_window_minutes)
        return assess_health(stored, now=self._clock(), window=window)


@asynccontextmanager
async def ledger_connections(
    settings: Settings, engine: AsyncEngine
) -> AsyncGenerator[LedgerConnections, None]:
    """Ledger-backed connections with an HTTP client for the token exchange."""
    async with httpx.AsyncClient(timeout=30) as http:
        yield LedgerConnections(settings, engine, http)
