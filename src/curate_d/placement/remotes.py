"""Which rclone remote reaches a provider account, and the daemon it lives on.

The rclone sidecar holds live provider credentials: each connected pCloud account is an
rclone remote named after it, configured on first use from the ledger's encrypted tokens
and updated whenever they change. The sidecar runs beside the I/O profile only; nothing
else can reach it.
"""

import asyncio
import json
from typing import Final, Protocol

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine

from curate_d.domain import Provider
from curate_d.placement.rclone import RcloneClient
from curate_d.providers.connections import ConnectionRepository
from curate_d.providers.crypto import CredentialCipher
from curate_d.providers.errors import (
    ConnectionRequiresReauthenticationError,
    ProviderRequestError,
)

# pCloud tokens do not expire; rclone reads the zero time as "never".
_NO_EXPIRY: Final = "0001-01-01T00:00:00Z"
_HTTP_TIMEOUT: Final = httpx.Timeout(30.0, read=600.0)


class RemoteResolver(Protocol):
    """Names the rclone filesystem that reaches one provider account."""

    async def fs_for(self, provider: Provider, account: str) -> str:
        """The `remote:` string for an account, configured if it was not."""
        ...


class RcloneAccess:
    """One rclone daemon, with an HTTP client per event loop."""

    def __init__(self, base_url: str) -> None:
        """Bind the daemon's address."""
        self._base_url = base_url
        self._clients: dict[asyncio.AbstractEventLoop, httpx.AsyncClient] = {}

    def client(self) -> RcloneClient:
        """A client for this loop."""
        loop = asyncio.get_running_loop()
        http = self._clients.get(loop)
        if http is None or http.is_closed:
            http = httpx.AsyncClient(timeout=_HTTP_TIMEOUT)
            self._clients[loop] = http
        return RcloneClient(http, self._base_url)


def remote_name(provider: Provider, account: str) -> str:
    """The rclone remote an account is configured as."""
    return f"{provider.value}-{account}"


class ConnectedRemotes:
    """Production `RemoteResolver`: remotes configured from stored connections."""

    def __init__(self, rclone: RcloneAccess, engine: AsyncEngine, cipher: CredentialCipher) -> None:
        """Bind the daemon, where connections are read from, and their key."""
        self._rclone = rclone
        self._engine = engine
        self._cipher = cipher

    async def fs_for(self, provider: Provider, account: str) -> str:
        """Configure the account's remote from its current tokens and name it.

        Raises:
            ConnectionRequiresReauthenticationError: the connection is flagged.
            ProviderRequestError: the account is not connected.
            RcloneError: the daemon refused the configuration.
        """
        async with self._engine.connect() as connection:
            repository = ConnectionRepository(connection, self._cipher)
            record = await repository.get(provider, account)
            tokens = await repository.tokens(provider, account)
        if record is None or tokens is None:
            raise ProviderRequestError(provider, account, "configure remote", "not connected")
        if not record.is_usable:
            raise ConnectionRequiresReauthenticationError(
                provider, account, "configure remote", record.failure_reason or "flagged"
            )
        token = json.dumps(
            {
                "access_token": tokens.access_token.get_secret_value(),
                "token_type": tokens.token_type,
                "expiry": tokens.expires_at.isoformat() if tokens.expires_at else _NO_EXPIRY,
            }
        )
        name = remote_name(provider, account)
        parameters = {"token": token}
        if record.api_host is not None:
            parameters["hostname"] = record.api_host
        await self._rclone.client().ensure_remote(name, provider.value, parameters)
        return f"{name}:"
