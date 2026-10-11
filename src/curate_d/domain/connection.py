"""Provider connections.

An authenticated link to one account at one provider. The credentials themselves are
`SecretStr` throughout so that the accidental log line, the exception repr, and the API
response that would otherwise leak them all print a placeholder instead.
"""

from enum import StrEnum

from pydantic import AwareDatetime, Field, SecretStr

from curate_d.domain.base import DomainModel
from curate_d.domain.source import Provider


class ConnectionState(StrEnum):
    """Whether a connection can currently be used, and if not, why not."""

    HEALTHY = "healthy"
    """Valid credentials; usable by scans and transfers."""

    EXPIRING = "expiring"
    """Inside the refresh window. Still usable; a refresh is due."""

    REQUIRES_REAUTHENTICATION = "requires_reauthentication"
    """A person must re-authorize. Dependent work stops rather than retrying forever."""


class OAuthTokens(DomainModel):
    """What a provider hands back after an authorization.

    `expires_at` is absolute rather than the `expires_in` most providers return: a
    relative lifetime is only meaningful next to the instant it was issued, and that
    instant is exactly what gets lost when the value is persisted.
    """

    access_token: SecretStr
    refresh_token: SecretStr | None = Field(
        default=None,
        description="Absent where the provider issues no refresh credential; such a "
        "connection can only be restored by a person.",
    )
    expires_at: AwareDatetime | None = Field(
        default=None,
        description="Absolute expiry. None means the provider declared no expiry.",
    )
    scope: str | None = None
    token_type: str = "bearer"

    @property
    def is_refreshable(self) -> bool:
        """Whether a refresh token is available to renew the access token."""
        return self.refresh_token is not None


class ProviderConnection(DomainModel):
    """One authenticated account at one provider, as the ledger records it."""

    provider: Provider
    account: str = Field(min_length=1)
    state: ConnectionState = ConnectionState.HEALTHY
    api_host: str | None = Field(
        default=None,
        description="API endpoint serving this account, where the provider is regional "
        "(pCloud serves US and EU accounts from different hosts).",
    )
    expires_at: AwareDatetime | None = None
    refreshable: bool = Field(
        default=False,
        description="Whether a refresh credential is stored, so expiry can be handled "
        "without a person.",
    )
    last_refreshed_at: AwareDatetime | None = None
    failure_reason: str | None = Field(
        default=None,
        description="Why re-authentication is needed. Never contains a token value.",
    )

    @property
    def is_usable(self) -> bool:
        """Whether work depending on this connection may proceed."""
        return self.state in (ConnectionState.HEALTHY, ConnectionState.EXPIRING)


class ConnectionHealth(DomainModel):
    """What the control plane reports about a connection.

    Deliberately a separate model from `ProviderConnection`: this one crosses the API
    boundary, so it must not grow a credential field by someone adding one to the record.
    """

    provider: Provider
    account: str = Field(min_length=1)
    state: ConnectionState
    expires_at: AwareDatetime | None = None
    requires_attention: bool = False
    detail: str | None = Field(
        default=None,
        description="Human-readable explanation. Redacted like everything else.",
    )
