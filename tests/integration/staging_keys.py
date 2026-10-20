"""Staging keys the integration suite issues, and the reservations an interrupted run left.

Reservations are counted across the whole database, so a live one left behind by a run
killed before its cleanup would eat a later test's small bound for up to its TTL. Every key
the suite issues carries a fixed prefix, which lets a test clear the suite's own leftovers
before it starts without touching a reservation any other writer holds.
"""

from typing import Final
from uuid import uuid4

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine

from curate_d.ledger.tables import staging_reservations
from curate_d.staging.keys import StagingKey

# "test" in hex-ish, padded to eight digits: one real derived key in 2**32 shares it.
SUITE_KEY_PREFIX: Final = "7e57c0de"


def suite_key() -> StagingKey:
    """Return a fresh staging key carrying the suite's prefix."""
    return StagingKey(SUITE_KEY_PREFIX + uuid4().hex[len(SUITE_KEY_PREFIX) :])


async def delete_suite_reservations(engine: AsyncEngine) -> None:
    """Delete every reservation made under a key the suite issued, in this run or an earlier one."""
    async with engine.begin() as connection:
        await connection.execute(
            delete(staging_reservations).where(
                staging_reservations.c.staging_key.startswith(SUITE_KEY_PREFIX)
            )
        )
