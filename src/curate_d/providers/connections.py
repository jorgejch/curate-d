"""Persistence for provider connections, with tokens encrypted at rest.

Row translation is split from the SQL as in `ledger/mapping.py`: which value is encrypted,
and that no plaintext token reaches a column, is a decision with isolated tests, while the
repository below only carries it to the database.

A stored connection is either healthy or awaiting re-authentication. "Expiring" is not
stored: it depends on the time of asking, so health reporting derives it.
"""

from collections.abc import Sequence

from pydantic import AwareDatetime, SecretStr
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from curate_d.domain import (
    ConnectionState,
    DomainModel,
    OAuthTokens,
    Provider,
    ProviderConnection,
)
from curate_d.ledger.mapping import (
    ColumnValues,
    Row,
    binary,
    opt_binary,
    opt_moment,
    opt_text,
    text,
)
from curate_d.ledger.tables import provider_connections
from curate_d.providers.crypto import CredentialCipher

# Columns a save replaces. The key and created_at are left alone.
_SAVED_COLUMNS = (
    "state",
    "api_host",
    "access_token_ciphertext",
    "refresh_token_ciphertext",
    "token_type",
    "scope",
    "expires_at",
    "last_refreshed_at",
    "failure_reason",
)


def to_provider_connection(row: Row) -> ProviderConnection:
    """Translate a provider_connections row into a `ProviderConnection`, without tokens."""
    return ProviderConnection(
        provider=Provider(text(row, "provider")),
        account=text(row, "account"),
        state=ConnectionState(text(row, "state")),
        api_host=opt_text(row, "api_host"),
        expires_at=opt_moment(row, "expires_at"),
        refreshable=opt_binary(row, "refresh_token_ciphertext") is not None,
        last_refreshed_at=opt_moment(row, "last_refreshed_at"),
        failure_reason=opt_text(row, "failure_reason"),
    )


def to_oauth_tokens(row: Row, cipher: CredentialCipher) -> OAuthTokens:
    """Decrypt a row's tokens.

    Raises:
        CredentialEncryptionError: the configured key does not match the one the tokens
            were stored under, or the ciphertext was modified.
    """
    refresh = opt_binary(row, "refresh_token_ciphertext")
    return OAuthTokens(
        access_token=cipher.decrypt(binary(row, "access_token_ciphertext")),
        refresh_token=None if refresh is None else cipher.decrypt(refresh),
        expires_at=opt_moment(row, "expires_at"),
        scope=opt_text(row, "scope"),
        token_type=text(row, "token_type"),
    )


class _SealedTokens(DomainModel):
    """The plaintext form of tokens, only ever held inside a sealed value."""

    access_token: str
    refresh_token: str | None
    expires_at: AwareDatetime | None
    scope: str | None
    token_type: str


def seal_tokens(tokens: OAuthTokens, cipher: CredentialCipher) -> bytes:
    """Encrypt tokens as one value, for passing between a step and a transaction.

    A step's return value is checkpointed in the DBOS system tables, so tokens renewed in
    a step and stored in a later transaction must cross that boundary encrypted.
    """
    plain = _SealedTokens(
        access_token=tokens.access_token.get_secret_value(),
        refresh_token=(
            None if tokens.refresh_token is None else tokens.refresh_token.get_secret_value()
        ),
        expires_at=tokens.expires_at,
        scope=tokens.scope,
        token_type=tokens.token_type,
    )
    return cipher.encrypt(SecretStr(plain.model_dump_json()))


def unseal_tokens(sealed: bytes, cipher: CredentialCipher) -> OAuthTokens:
    """Recover tokens sealed by `seal_tokens`.

    Raises:
        CredentialEncryptionError: the value was not sealed under this key.
    """
    plain = _SealedTokens.model_validate_json(cipher.decrypt(sealed).get_secret_value())
    return OAuthTokens(
        access_token=SecretStr(plain.access_token),
        refresh_token=None if plain.refresh_token is None else SecretStr(plain.refresh_token),
        expires_at=plain.expires_at,
        scope=plain.scope,
        token_type=plain.token_type,
    )


def connection_values(
    provider: Provider,
    account: str,
    tokens: OAuthTokens,
    cipher: CredentialCipher,
    *,
    api_host: str | None,
    refreshed_at: AwareDatetime | None,
) -> ColumnValues:
    """Column values for a connection holding freshly issued tokens.

    Fresh tokens are usable by definition, so the state is healthy and any earlier
    failure reason is cleared.
    """
    return {
        "provider": provider.value,
        "account": account,
        "state": ConnectionState.HEALTHY.value,
        "api_host": api_host,
        "access_token_ciphertext": cipher.encrypt(tokens.access_token),
        "refresh_token_ciphertext": (
            None if tokens.refresh_token is None else cipher.encrypt(tokens.refresh_token)
        ),
        "token_type": tokens.token_type,
        "scope": tokens.scope,
        "expires_at": tokens.expires_at,
        "last_refreshed_at": refreshed_at,
        "failure_reason": None,
    }


class ConnectionRepository:
    """Reads and writes provider connections. Tokens only ever cross it encrypted."""

    def __init__(self, connection: AsyncConnection, cipher: CredentialCipher) -> None:
        """Wrap a caller-owned connection and the cipher tokens are stored under."""
        self._connection = connection
        self._cipher = cipher

    async def save(
        self,
        provider: Provider,
        account: str,
        tokens: OAuthTokens,
        *,
        api_host: str | None = None,
        refreshed_at: AwareDatetime | None = None,
    ) -> ProviderConnection:
        """Store tokens for an account, replacing any it already had.

        Used both for a new authorization and for a refresh; either leaves the connection
        healthy.
        """
        statement = insert(provider_connections).values(
            **connection_values(
                provider,
                account,
                tokens,
                self._cipher,
                api_host=api_host,
                refreshed_at=refreshed_at,
            )
        )
        upsert = statement.on_conflict_do_update(
            index_elements=["provider", "account"],
            set_={key: statement.excluded[key] for key in _SAVED_COLUMNS},
        ).returning(*provider_connections.c)
        result = await self._connection.execute(upsert)
        return to_provider_connection(result.mappings().one())

    async def get(self, provider: Provider, account: str) -> ProviderConnection | None:
        """Fetch a connection's record, or None if the account was never authorized."""
        result = await self._connection.execute(
            select(provider_connections).where(
                provider_connections.c.provider == provider.value,
                provider_connections.c.account == account,
            )
        )
        row = result.mappings().one_or_none()
        return to_provider_connection(row) if row is not None else None

    async def tokens(self, provider: Provider, account: str) -> OAuthTokens | None:
        """Decrypt an account's tokens, or None if the account was never authorized.

        Raises:
            CredentialEncryptionError: the tokens cannot be decrypted with this cipher.
        """
        result = await self._connection.execute(
            select(provider_connections).where(
                provider_connections.c.provider == provider.value,
                provider_connections.c.account == account,
            )
        )
        row = result.mappings().one_or_none()
        return to_oauth_tokens(row, self._cipher) if row is not None else None

    async def list_all(self) -> Sequence[ProviderConnection]:
        """Every connection, ordered by provider then account."""
        result = await self._connection.execute(
            select(provider_connections).order_by(
                provider_connections.c.provider, provider_connections.c.account
            )
        )
        return [to_provider_connection(row) for row in result.mappings()]

    async def mark_requires_reauthentication(
        self, provider: Provider, account: str, reason: str
    ) -> bool:
        """Flag a connection as needing a person, keeping its tokens for the record.

        Returns:
            False if no such connection exists.
        """
        result = await self._connection.execute(
            provider_connections.update()
            .where(
                provider_connections.c.provider == provider.value,
                provider_connections.c.account == account,
            )
            .values(
                state=ConnectionState.REQUIRES_REAUTHENTICATION.value,
                failure_reason=reason,
            )
        )
        return result.rowcount > 0
