"""Connection repository against a real database."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
import pytest
from pydantic import HttpUrl, SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from curate_d.domain import ConnectionState, OAuthTokens, Provider
from curate_d.ledger.tables import provider_connections
from curate_d.providers.authorization import (
    AuthorizationStates,
    begin_pcloud_authorization,
    complete_pcloud_authorization,
)
from curate_d.providers.connections import ConnectionRepository
from curate_d.providers.crypto import CredentialCipher, CredentialEncryptionError, generate_key
from curate_d.providers.pcloud.oauth import PCloudOAuthConfig

pytestmark = pytest.mark.integration

_WHEN = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
_ACCESS = "integration-access-token"
_REFRESH = "integration-refresh-token"


@pytest.fixture
async def connection(ledger_engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """A transaction rolled back at the end, so tests cannot see each other's rows."""
    async with ledger_engine.connect() as conn:
        transaction = await conn.begin()
        try:
            yield conn
        finally:
            await transaction.rollback()


@pytest.fixture
def cipher() -> CredentialCipher:
    """A cipher built from a freshly generated key."""
    return CredentialCipher(SecretStr(generate_key()))


def _tokens(access: str = _ACCESS) -> OAuthTokens:
    """Tokens as a provider would issue them."""
    return OAuthTokens(
        access_token=SecretStr(access),
        refresh_token=SecretStr(_REFRESH),
        expires_at=_WHEN,
    )


async def test_saved_tokens_round_trip(
    connection: AsyncConnection, cipher: CredentialCipher
) -> None:
    """Tokens saved for an account decrypt back to the same values."""
    repository = ConnectionRepository(connection, cipher)
    record = await repository.save(Provider.PCLOUD, "acct-rt", _tokens())

    assert record.state is ConnectionState.HEALTHY
    assert await repository.get(Provider.PCLOUD, "acct-rt") == record
    tokens = await repository.tokens(Provider.PCLOUD, "acct-rt")
    assert tokens is not None
    assert tokens.access_token.get_secret_value() == _ACCESS


async def test_the_stored_row_holds_no_plaintext(
    connection: AsyncConnection, cipher: CredentialCipher
) -> None:
    """The database row contains ciphertext, not the tokens."""
    await ConnectionRepository(connection, cipher).save(Provider.PCLOUD, "acct-raw", _tokens())

    row = (
        (
            await connection.execute(
                select(provider_connections).where(provider_connections.c.account == "acct-raw")
            )
        )
        .mappings()
        .one()
    )
    for value in row.values():
        rendered = value if isinstance(value, bytes) else str(value).encode()
        assert _ACCESS.encode() not in rendered
        assert _REFRESH.encode() not in rendered


async def test_stored_tokens_are_unreadable_under_another_key(
    connection: AsyncConnection, cipher: CredentialCipher
) -> None:
    """A repository holding a different key cannot read the tokens."""
    await ConnectionRepository(connection, cipher).save(Provider.PCLOUD, "acct-key", _tokens())

    other = ConnectionRepository(connection, CredentialCipher(SecretStr(generate_key())))
    with pytest.raises(CredentialEncryptionError):
        await other.tokens(Provider.PCLOUD, "acct-key")


async def test_reauthorizing_replaces_tokens_and_clears_the_failure(
    connection: AsyncConnection, cipher: CredentialCipher
) -> None:
    """A second save for the same account replaces its tokens and restores health."""
    repository = ConnectionRepository(connection, cipher)
    await repository.save(Provider.PCLOUD, "acct-re", _tokens())
    assert await repository.mark_requires_reauthentication(
        Provider.PCLOUD, "acct-re", "refresh rejected: invalid_grant"
    )

    record = await repository.save(Provider.PCLOUD, "acct-re", _tokens(access="second-token"))

    assert record.state is ConnectionState.HEALTHY
    assert record.failure_reason is None
    tokens = await repository.tokens(Provider.PCLOUD, "acct-re")
    assert tokens is not None
    assert tokens.access_token.get_secret_value() == "second-token"
    assert [c.account for c in await repository.list_all() if c.account == "acct-re"] == ["acct-re"]


async def test_marking_reauthentication_records_the_reason(
    connection: AsyncConnection, cipher: CredentialCipher
) -> None:
    """A rejected refresh leaves the connection flagged with why."""
    repository = ConnectionRepository(connection, cipher)
    await repository.save(Provider.PCLOUD, "acct-fail", _tokens())

    await repository.mark_requires_reauthentication(Provider.PCLOUD, "acct-fail", "revoked")

    record = await repository.get(Provider.PCLOUD, "acct-fail")
    assert record is not None
    assert record.state is ConnectionState.REQUIRES_REAUTHENTICATION
    assert record.failure_reason == "revoked"


async def test_marking_an_unknown_connection_reports_nothing_changed(
    connection: AsyncConnection, cipher: CredentialCipher
) -> None:
    """Flagging an account that was never authorized changes no row."""
    repository = ConnectionRepository(connection, cipher)
    assert not await repository.mark_requires_reauthentication(
        Provider.PCLOUD, "acct-none", "revoked"
    )
    assert await repository.get(Provider.PCLOUD, "acct-none") is None
    assert await repository.tokens(Provider.PCLOUD, "acct-none") is None


async def test_authorization_against_a_stubbed_pcloud_persists_a_usable_connection(
    connection: AsyncConnection, cipher: CredentialCipher
) -> None:
    """The full flow stores a healthy connection whose token reads back for use."""
    config = PCloudOAuthConfig(
        client_id="client-1",
        client_secret=SecretStr("client-secret"),
        redirect_uri=HttpUrl("https://curate-d.example/connections/pcloud/callback"),
    )
    states = AuthorizationStates(cipher, max_age_seconds=600)

    def pcloud(request: httpx.Request) -> httpx.Response:
        """Answer the token exchange as pCloud's EU host would."""
        assert request.url.path == "/oauth2_token"
        return httpx.Response(
            200, json={"result": 0, "access_token": "stub-token", "uid": 777, "locationid": 2}
        )

    repository = ConnectionRepository(connection, cipher)
    pending = begin_pcloud_authorization(config, states)
    async with httpx.AsyncClient(transport=httpx.MockTransport(pcloud)) as http:
        record = await complete_pcloud_authorization(
            config,
            states,
            http,
            repository,
            code="code-1",
            state=pending.state,
            hostname="eapi.pcloud.com",
            browser_nonce=pending.browser_nonce.get_secret_value(),
        )

    assert record.is_usable
    stored = await repository.get(Provider.PCLOUD, "777")
    assert stored is not None
    assert stored.api_host == "eapi.pcloud.com"
    tokens = await repository.tokens(Provider.PCLOUD, "777")
    assert tokens is not None
    assert tokens.access_token.get_secret_value() == "stub-token"
