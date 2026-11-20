"""The ledger datasource."""

from typing import cast

import pytest
from dbos import AsyncSQLAlchemyDatasource

from curate_d.config import Settings
from curate_d.runtime.datasource import TRACKING_SCHEMA, create_ledger_datasource


async def test_the_datasource_only_verifies_the_tracking_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The datasource targets the ledger and never migrates: `migrations/env.py` does."""
    calls: list[dict[str, object]] = []
    created = cast(AsyncSQLAlchemyDatasource, object())

    async def create(**kwargs: object) -> AsyncSQLAlchemyDatasource:
        """Record the keyword arguments and return a stand-in datasource."""
        calls.append(kwargs)
        return created

    monkeypatch.setattr(AsyncSQLAlchemyDatasource, "create", create)
    settings = Settings()
    assert await create_ledger_datasource(settings) is created
    assert calls == [
        {
            "database_url": str(settings.ledger_database_url),
            "schema": TRACKING_SCHEMA,
            "run_migrations": False,
        }
    ]
