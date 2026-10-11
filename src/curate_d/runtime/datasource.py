"""The ledger datasource.

This is where the exactly-once guarantee lives. A DBOS datasource wraps the *ledger*
database and records each transaction's completion in a tracking table inside that same
database, so the ledger row and the record that it was written commit together. On replay
after a crash, a completed ledger write is not repeated.

The invariant, stated once so it can be pointed at: **every ledger write issued from
inside a workflow goes through a datasource transaction.** A raw engine or session call
from a workflow is not tracked and will re-execute on replay, producing exactly the
duplicate placement this system exists to prevent. `make lint` fails the build if one
appears (`python -m curate_d.runtime.workflow_audit`).
"""

from collections.abc import Awaitable, Callable

from dbos import AsyncSQLAlchemyDatasource
from sqlalchemy.ext.asyncio import AsyncSession

from curate_d.config import Settings
from curate_d.runtime.dbos_app import SYSTEM_SCHEMA

# The datasource's tracking table shares the schema DBOS uses for its other tables, so a
# dump of that schema is the whole of the workflow-side state.
TRACKING_SCHEMA = SYSTEM_SCHEMA


async def create_ledger_datasource(settings: Settings) -> AsyncSQLAlchemyDatasource:
    """Build the datasource against the ledger database, which also holds DBOS's system tables.

    The tracking record must commit in the same transaction as the ledger row it tracks, so
    the datasource targets the ledger URL; pointing it anywhere else would silently
    downgrade every in-workflow write from exactly-once to at-least-once. DBOS's own tables
    share that database by design (dbos_app.py), which is what also lets a ledger write and
    a workflow enqueue commit together.

    Call before `DBOS.launch()`: DBOS registers every datasource so a rewind can drop the
    checkpoints it holds, and rejects one created after launch.
    """
    datasource: AsyncSQLAlchemyDatasource = await AsyncSQLAlchemyDatasource.create(
        str(settings.ledger_database_url),
        schema=TRACKING_SCHEMA,
    )
    return datasource


type LedgerTransaction[T] = Callable[[AsyncSession], Awaitable[T]]
