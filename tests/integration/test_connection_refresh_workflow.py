"""The scheduled refresh workflow under DBOS, against the real ledger."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from dbos import DBOS, SetWorkflowID
from pydantic import SecretStr
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine

from curate_d.config import Settings
from curate_d.domain import ConnectionState, OAuthTokens, Provider
from curate_d.ledger.tables import provider_connections
from curate_d.providers.connections import ConnectionRepository
from curate_d.providers.crypto import CredentialCipher, generate_key
from curate_d.providers.errors import CredentialsRejectedError
from curate_d.runtime import services
from curate_d.runtime.datasource import create_ledger_datasource
from curate_d.runtime.dbos_app import configure
from curate_d.workflows.connections import refresh_connections

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


class _Refresher:
    """Renews every account except the ones listed as rejected."""

    def __init__(self, rejected: set[str]) -> None:
        """Reject refreshes for `rejected` accounts."""
        self.rejected = rejected

    async def refresh(self, account: str, tokens: OAuthTokens) -> OAuthTokens:
        """Renew, or refuse as the provider would."""
        if account in self.rejected:
            raise CredentialsRejectedError(Provider.PCLOUD, account, "refresh", "invalid_grant")
        return OAuthTokens(access_token=SecretStr(f"renewed-{account}"))


@pytest.fixture
def cipher() -> CredentialCipher:
    """A cipher built from a freshly generated key."""
    return CredentialCipher(SecretStr(generate_key()))


@pytest.fixture
def accounts() -> tuple[str, str]:
    """Two account ids unique to this test run."""
    suffix = uuid4().hex[:8]
    return f"ok-{suffix}", f"bad-{suffix}"


@pytest.fixture
async def launched(
    settings: Settings,
    ledger_engine: AsyncEngine,
    cipher: CredentialCipher,
    accounts: tuple[str, str],
) -> AsyncIterator[None]:
    """DBOS launched with services installed, and two expiring connections seeded."""
    _, bad = accounts
    async with ledger_engine.begin() as connection:
        repository = ConnectionRepository(connection, cipher)
        for account in accounts:
            await repository.save(
                Provider.PCLOUD,
                account,
                OAuthTokens(
                    access_token=SecretStr("old"),
                    refresh_token=SecretStr("refresh"),
                    expires_at=_NOW + timedelta(minutes=5),
                ),
            )
    DBOS.destroy()
    configure(settings, executor_id=f"refresh-test-{uuid4().hex[:8]}")
    datasource = await create_ledger_datasource(settings)
    services.install(
        services.Services(
            settings=settings,
            datasource=datasource,
            cipher=cipher,
            refreshers={Provider.PCLOUD: _Refresher({bad})},
            clock=lambda: _NOW,
        )
    )
    DBOS.launch()
    try:
        yield
    finally:
        DBOS.destroy()
        services.uninstall()
        await datasource.engine.dispose()
        async with ledger_engine.begin() as connection:
            await connection.execute(
                delete(provider_connections).where(provider_connections.c.account.in_(accounts))
            )


async def test_a_pass_renews_one_token_and_flags_the_rejected_one(
    launched: None,
    ledger_engine: AsyncEngine,
    cipher: CredentialCipher,
    accounts: tuple[str, str],
) -> None:
    """The renewed token is stored and used; the rejected connection needs a person."""
    good, bad = accounts
    with SetWorkflowID(f"refresh-test-{uuid4()}"):
        await refresh_connections(_NOW, None)

    async with ledger_engine.connect() as connection:
        repository = ConnectionRepository(connection, cipher)
        tokens = await repository.tokens(Provider.PCLOUD, good)
        renewed = await repository.get(Provider.PCLOUD, good)
        rejected = await repository.get(Provider.PCLOUD, bad)

    assert tokens is not None
    assert tokens.access_token.get_secret_value() == f"renewed-{good}"
    assert tokens.refresh_token is not None
    assert renewed is not None
    assert renewed.last_refreshed_at == _NOW
    assert rejected is not None
    assert rejected.state is ConnectionState.REQUIRES_REAUTHENTICATION
    assert rejected.failure_reason == "invalid_grant"


async def test_replaying_a_completed_pass_writes_nothing_twice(
    launched: None,
    ledger_engine: AsyncEngine,
    cipher: CredentialCipher,
    accounts: tuple[str, str],
) -> None:
    """Re-running the same workflow id returns its result without refreshing again."""
    good, _ = accounts
    workflow_id = f"refresh-test-{uuid4()}"
    with SetWorkflowID(workflow_id):
        await refresh_connections(_NOW, None)
    async with ledger_engine.begin() as connection:
        await ConnectionRepository(connection, cipher).save(
            Provider.PCLOUD, good, OAuthTokens(access_token=SecretStr("set-by-person"))
        )

    with SetWorkflowID(workflow_id):
        await refresh_connections(_NOW, None)

    async with ledger_engine.connect() as connection:
        tokens = await ConnectionRepository(connection, cipher).tokens(Provider.PCLOUD, good)
    assert tokens is not None
    assert tokens.access_token.get_secret_value() == "set-by-person"
