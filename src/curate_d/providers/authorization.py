"""Starting and completing a provider authorization.

The OAuth `state` is a Fernet token over the provider and a random nonce, so it needs no
table: Fernet authenticates it and embeds its creation time, which bounds how long an
authorization may take. The nonce is also handed to the initiating browser (the control
plane sets it as a cookie), and the callback must present both. A state alone proves the
authorization started here; the nonce proves it is being finished by the browser that
started it, which is what stops someone else's consent being completed into this
deployment.
"""

import hmac
import json
import secrets
from typing import Final, Protocol

import httpx
from pydantic import AwareDatetime, Field, SecretStr, ValidationError

from curate_d.config import Settings
from curate_d.domain import DomainModel, OAuthTokens, Provider, ProviderConnection
from curate_d.providers.crypto import CredentialCipher, CredentialEncryptionError
from curate_d.providers.pcloud.oauth import PCloudOAuthConfig, authorization_url, exchange_code

_NONCE_BYTES: Final = 32


class ConnectionSaver(Protocol):
    """Where a completed authorization is persisted; `ConnectionRepository` in production."""

    async def save(
        self,
        provider: Provider,
        account: str,
        tokens: OAuthTokens,
        *,
        api_host: str | None = None,
        refreshed_at: AwareDatetime | None = None,
    ) -> ProviderConnection:
        """Store tokens for an account, replacing any it already had."""
        ...


class AuthorizationNotConfiguredError(RuntimeError):
    """The provider's OAuth client is not configured in this deployment."""


class AuthorizationStateError(ValueError):
    """A callback's state is forged, expired, for another provider, or from another browser."""


class PendingAuthorization(DomainModel):
    """What initiating an authorization hands back to the control plane."""

    provider: Provider
    redirect_url: str = Field(min_length=1, description="Provider consent page.")
    state: str = Field(min_length=1)
    browser_nonce: SecretStr = Field(description="Bind to the initiating browser.")


class _StatePayload(DomainModel):
    """What the state token encrypts."""

    provider: Provider
    nonce: str = Field(min_length=1)


class AuthorizationStates:
    """Issues and redeems OAuth states under the credential key."""

    def __init__(self, cipher: CredentialCipher, *, max_age_seconds: int) -> None:
        """Bind the cipher states are sealed with and how long they remain redeemable."""
        self._cipher = cipher
        self._max_age_seconds = max_age_seconds

    def issue(self, provider: Provider) -> tuple[str, SecretStr]:
        """Return a fresh state and the nonce the initiating browser must present."""
        nonce = secrets.token_urlsafe(_NONCE_BYTES)
        payload = _StatePayload(provider=provider, nonce=nonce).model_dump_json()
        state = self._cipher.encrypt(SecretStr(payload)).decode("ascii")
        return state, SecretStr(nonce)

    def redeem(self, state: str, *, provider: Provider, browser_nonce: str | None) -> None:
        """Accept a callback's state, or refuse it.

        Raises:
            AuthorizationStateError: the state was not issued here, has expired, names
                another provider, or the browser did not present the matching nonce.
        """
        try:
            plaintext = self._cipher.decrypt(
                state.encode("ascii"), max_age_seconds=self._max_age_seconds
            )
            payload = _StatePayload.model_validate(
                json.loads(plaintext.get_secret_value()), strict=False
            )
        except (CredentialEncryptionError, UnicodeEncodeError, ValueError, ValidationError):
            raise AuthorizationStateError(
                "authorization state is invalid or expired; start the authorization again"
            ) from None
        if payload.provider is not provider:
            raise AuthorizationStateError(
                f"authorization state was issued for {payload.provider.value}, not {provider.value}"
            )
        if browser_nonce is None or not hmac.compare_digest(
            payload.nonce.encode(), browser_nonce.encode()
        ):
            raise AuthorizationStateError(
                "authorization was not completed by the browser that started it"
            )


def pcloud_oauth_config(settings: Settings) -> PCloudOAuthConfig:
    """Return the pCloud client registration from settings.

    Raises:
        AuthorizationNotConfiguredError: a client id, secret or redirect URI is unset.
    """
    if (
        settings.pcloud_client_id is None
        or settings.pcloud_client_secret is None
        or settings.pcloud_redirect_uri is None
    ):
        raise AuthorizationNotConfiguredError(
            "pCloud authorization needs CURATE_D_PCLOUD_CLIENT_ID, "
            "CURATE_D_PCLOUD_CLIENT_SECRET and CURATE_D_PCLOUD_REDIRECT_URI"
        )
    return PCloudOAuthConfig(
        client_id=settings.pcloud_client_id,
        client_secret=settings.pcloud_client_secret,
        redirect_uri=settings.pcloud_redirect_uri,
    )


def begin_pcloud_authorization(
    config: PCloudOAuthConfig, states: AuthorizationStates
) -> PendingAuthorization:
    """Start an authorization: the consent URL, its state, and the browser's nonce."""
    state, nonce = states.issue(Provider.PCLOUD)
    return PendingAuthorization(
        provider=Provider.PCLOUD,
        redirect_url=authorization_url(config, state),
        state=state,
        browser_nonce=nonce,
    )


async def complete_pcloud_authorization(
    config: PCloudOAuthConfig,
    states: AuthorizationStates,
    http: httpx.AsyncClient,
    repository: ConnectionSaver,
    *,
    code: str,
    state: str,
    hostname: str,
    browser_nonce: str | None,
) -> ProviderConnection:
    """Finish an authorization and persist the connection it yields.

    The state is checked before the code is exchanged, so a forged callback never causes
    a request carrying our client secret.

    Raises:
        AuthorizationStateError: the state or the browser nonce is not acceptable.
        ProviderError: pCloud refused the code or could not be reached.
    """
    states.redeem(state, provider=Provider.PCLOUD, browser_nonce=browser_nonce)
    authorization = await exchange_code(config, http, code=code, hostname=hostname)
    return await repository.save(
        Provider.PCLOUD,
        authorization.account,
        authorization.tokens,
        api_host=authorization.api_host,
    )
