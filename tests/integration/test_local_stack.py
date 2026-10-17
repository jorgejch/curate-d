"""The local development stack is usable by the application."""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.integration

# Both are load-bearing: postgis clusters geotagged media into trips, pg_trgm associates
# sidecar files with their media by name. A stack missing either fails much later and
# much less obviously.
REQUIRED_EXTENSIONS = frozenset({"postgis", "pg_trgm"})


async def test_required_extensions_are_installed(ledger_engine: AsyncEngine) -> None:
    """postgis and pg_trgm are both installed in the ledger database."""
    async with ledger_engine.connect() as connection:
        result = await connection.execute(text("SELECT extname FROM pg_extension"))
        installed = {row[0] for row in result}

    assert installed >= REQUIRED_EXTENSIONS, (
        f"missing extensions: {sorted(REQUIRED_EXTENSIONS - installed)}. Run `make test-stack`."
    )


async def test_postgis_and_trigram_functions_are_callable(ledger_engine: AsyncEngine) -> None:
    """Presence in pg_extension is not the same as the functions being usable."""
    async with ledger_engine.connect() as connection:
        distance = await connection.scalar(
            text("SELECT ST_Distance(ST_MakePoint(0, 0)::geography, ST_MakePoint(0, 1)::geography)")
        )
        similarity = await connection.scalar(text("SELECT similarity('IMG_0421', 'IMG_0421.aae')"))

    assert distance is not None and distance > 0
    assert similarity is not None and 0 < similarity < 1
