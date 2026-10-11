"""DBOS against the real ledger database.

Two properties are load-bearing and neither can be checked without a database: that
workflow state lands beside the ledger, and that a datasource transaction is not repeated
when a workflow is replayed after a crash. A deliberate rewind is the opposite case: it
re-executes, and the audit trail must record the re-execution.
"""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from dbos import DBOS, AsyncSQLAlchemyDatasource, SetWorkflowID
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from curate_d.config import Settings
from curate_d.domain import AuditEntry, AuditEntryType
from curate_d.ledger import AuditRepository
from curate_d.runtime.datasource import create_ledger_datasource
from curate_d.runtime.dbos_app import SYSTEM_SCHEMA, configure

pytestmark = pytest.mark.integration


@pytest.fixture
async def dbos(settings: Settings) -> AsyncIterator[None]:
    """Launch a DBOS instance against the test settings.

    Yields:
        Nothing; DBOS is destroyed again once the test completes.
    """
    DBOS.destroy()
    configure(settings, executor_id="test-executor")
    DBOS.launch()
    yield
    DBOS.destroy()


@pytest.fixture
async def datasource(settings: Settings) -> AsyncIterator[AsyncSQLAlchemyDatasource]:
    """Launch DBOS with a ledger datasource registered, as a worker would.

    DBOS rejects a datasource created after launch, so this fixture owns the launch
    rather than composing with `dbos`.

    Yields:
        The created `AsyncSQLAlchemyDatasource`.
    """
    DBOS.destroy()
    configure(settings, executor_id="test-executor")
    created = await create_ledger_datasource(settings)
    DBOS.launch()
    yield created
    DBOS.destroy()
    await created.engine.dispose()


async def test_the_workflow_system_schema_lives_beside_the_ledger(
    dbos: None, ledger_engine: AsyncEngine
) -> None:
    """Verify the DBOS system schema and source_objects table share the ledger database."""
    async with ledger_engine.connect() as connection:
        schemas = {
            row[0]
            for row in await connection.execute(
                text("SELECT schema_name FROM information_schema.schemata")
            )
        }
        ledger_tables = {
            row[0]
            for row in await connection.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            )
        }
    assert SYSTEM_SCHEMA in schemas, "DBOS system tables are not in the ledger database"
    assert "source_objects" in ledger_tables


async def test_the_datasource_tracking_table_is_in_the_ledger_database(
    datasource: AsyncSQLAlchemyDatasource, ledger_engine: AsyncEngine
) -> None:
    """The tracking record must commit in the same transaction as the ledger row.

    It can only do that if it lives in the same database; anywhere else and the write
    silently degrades from exactly-once to at-least-once.
    """

    @datasource.transaction()
    async def touch() -> int:
        """Run a trivial query inside a datasource transaction."""
        session = datasource.sql_session()
        result = await session.execute(text("SELECT 1"))
        return int(result.scalar_one())

    assert await touch() == 1

    async with ledger_engine.connect() as connection:
        present = await connection.scalar(
            text(
                "SELECT count(*) FROM pg_tables"
                " WHERE schemaname = :schema AND tablename = 'datasource_outputs'"
            ),
            {"schema": SYSTEM_SCHEMA},
        )
    assert present == 1


async def _wait_for_row(engine: AsyncEngine, table: str) -> None:
    """Poll until the workflow has got as far as its write, bounded so a bug fails fast."""
    for _ in range(100):
        async with engine.connect() as connection:
            if await connection.scalar(text(f"SELECT count(*) FROM {table}")):
                return
        await asyncio.sleep(0.05)
    raise AssertionError(f"{table} stayed empty; the workflow never reached its write")


async def test_a_replayed_workflow_does_not_repeat_a_completed_ledger_write(
    datasource: AsyncSQLAlchemyDatasource, ledger_engine: AsyncEngine
) -> None:
    """The whole reason DBOS is here.

    The workflow writes a ledger row and is then interrupted. Recovery re-invokes the
    workflow function from the top: the body runs again, but the committed write is
    served from its checkpoint rather than repeated.
    """
    async with ledger_engine.begin() as connection:
        await connection.execute(text("DROP TABLE IF EXISTS replay_probe"))
        await connection.execute(text("CREATE TABLE replay_probe (note text)"))

    body_runs = 0
    released = asyncio.Event()

    @datasource.transaction()
    async def record(note: str) -> None:
        """Insert one tracked row into replay_probe."""
        await datasource.sql_session().execute(
            text("INSERT INTO replay_probe (note) VALUES (:note)"), {"note": note}
        )

    @DBOS.step()
    async def wait_a_moment() -> None:
        """Yield briefly, as a pollable unit of workflow progress."""
        await asyncio.sleep(0.05)

    @DBOS.workflow()
    async def write_then_stall(note: str) -> None:
        """Write the tracked row, then stall until released."""
        nonlocal body_runs
        body_runs += 1
        await record(note)
        # Stalls until the test releases it, standing in for a worker that is still
        # holding the workflow when the process goes away.
        while not released.is_set():
            await wait_a_moment()

    # Unique per run: a workflow id is permanent, and a leftover row from an earlier
    # run would collide rather than replay.
    workflow_id = f"replay-probe-{uuid4()}"
    with SetWorkflowID(workflow_id):
        handle = await DBOS.start_workflow_async(write_then_stall, "once")

    await _wait_for_row(ledger_engine, "replay_probe")
    await DBOS.cancel_workflow_async(workflow_id)
    with pytest.raises(Exception, match="cancel"):
        await handle.get_result()

    released.set()
    resumed = await DBOS.resume_workflow_async(workflow_id)
    await resumed.get_result()

    async with ledger_engine.connect() as connection:
        rows = await connection.scalar(text("SELECT count(*) FROM replay_probe"))
    async with ledger_engine.begin() as connection:
        await connection.execute(text("DROP TABLE replay_probe"))

    assert body_runs == 2, "recovery should have re-invoked the workflow function"
    assert rows == 1, "the completed ledger write was repeated on replay"


async def test_an_untracked_write_in_the_same_position_is_repeated(
    dbos: None, ledger_engine: AsyncEngine
) -> None:
    """The counterfactual, so the guarantee above is not mistaken for luck.

    Same workflow, same interruption, except the write goes straight at the engine
    instead of through the datasource. It happens twice. This is the mistake
    `curate_d.runtime.workflow_audit` fails the build over.
    """
    async with ledger_engine.begin() as connection:
        await connection.execute(text("DROP TABLE IF EXISTS raw_probe"))
        await connection.execute(text("CREATE TABLE raw_probe (note text)"))

    released = asyncio.Event()

    async def record_raw(note: str) -> None:
        """Insert one untracked row into raw_probe, bypassing the datasource."""
        async with ledger_engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO raw_probe (note) VALUES (:note)"), {"note": note}
            )

    @DBOS.step()
    async def wait_a_moment() -> None:
        """Yield briefly, as a pollable unit of workflow progress."""
        await asyncio.sleep(0.05)

    @DBOS.workflow()
    async def write_then_stall(note: str) -> None:
        """Write the untracked row, then stall until released."""
        await record_raw(note)
        while not released.is_set():
            await wait_a_moment()

    workflow_id = f"raw-probe-{uuid4()}"
    with SetWorkflowID(workflow_id):
        handle = await DBOS.start_workflow_async(write_then_stall, "once")

    await _wait_for_row(ledger_engine, "raw_probe")
    await DBOS.cancel_workflow_async(workflow_id)
    with pytest.raises(Exception, match="cancel"):
        await handle.get_result()

    released.set()
    resumed = await DBOS.resume_workflow_async(workflow_id)
    await resumed.get_result()

    async with ledger_engine.connect() as connection:
        rows = await connection.scalar(text("SELECT count(*) FROM raw_probe"))
    async with ledger_engine.begin() as connection:
        await connection.execute(text("DROP TABLE raw_probe"))

    assert rows == 2, "an untracked write should re-execute; if not, the guard is moot"


async def test_a_rewound_step_appends_the_decision_it_reaches(
    datasource: AsyncSQLAlchemyDatasource, ledger_engine: AsyncEngine
) -> None:
    """A rewind re-executes; its new decision is audited beside the original one.

    The trail is append-only and explains the ledger, so it must hold both decisions in
    order, and its newest entry must agree with what the ledger now says. Suppressing the
    re-run's entry would leave the ledger's current value unexplained.
    """
    async with ledger_engine.begin() as connection:
        await connection.execute(text("DROP TABLE IF EXISTS rewind_probe"))
        await connection.execute(
            text("CREATE TABLE rewind_probe (subject text PRIMARY KEY, decision text)")
        )

    subject = f"rewind-{uuid4()}"
    # Stands in for whatever changed between runs: fixed code, a new version, new input.
    decisions = iter(["2019", "2020"])

    @datasource.transaction()
    async def decide(subject: str, decision: str) -> None:
        """Upsert the ledger row and audit the decision in one transaction."""
        session = datasource.sql_session()
        await session.execute(
            text(
                "INSERT INTO rewind_probe (subject, decision) VALUES (:subject, :decision)"
                " ON CONFLICT (subject) DO UPDATE SET decision = excluded.decision"
            ),
            {"subject": subject, "decision": decision},
        )
        await AuditRepository(await session.connection()).append(
            AuditEntry(
                entry_type=AuditEntryType.CLASSIFICATION_DECIDED,
                occurred_at=datetime.now(UTC),
                decided_by="tests.rewind",
                details={"subject": subject, "decision": decision},
            )
        )

    @DBOS.step()
    async def evaluate() -> str:
        """Reach a decision; a rewound run reaches the next one."""
        return next(decisions)

    @DBOS.workflow()
    async def classify(subject: str) -> None:
        """Evaluate, then record the decision in the ledger and the audit trail."""
        await decide(subject, await evaluate())

    workflow_id = f"rewind-probe-{uuid4()}"
    with SetWorkflowID(workflow_id):
        await classify(subject)
    rewound = await DBOS.rewind_workflow_async(workflow_id, start_step=1)
    await rewound.get_result()

    async with ledger_engine.connect() as connection:
        ledger = await connection.scalar(
            text("SELECT decision FROM rewind_probe WHERE subject = :subject"),
            {"subject": subject},
        )
        audited = [
            row[0]
            for row in await connection.execute(
                text(
                    "SELECT details->>'decision' FROM audit_entries"
                    " WHERE details->>'subject' = :subject ORDER BY id"
                ),
                {"subject": subject},
            )
        ]
    async with ledger_engine.begin() as connection:
        await connection.execute(text("DROP TABLE rewind_probe"))

    assert audited == ["2019", "2020"], "the re-executed decision was not audited"
    assert ledger == audited[-1], "the newest audit entry does not explain the ledger"
