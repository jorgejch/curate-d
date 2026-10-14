"""Spikes for the scheduled recovery sweep: the two fork paths its design relies on.

The scheduled sweep forks from inside a `@DBOS.step`, and the operator sweep forks through
a `DBOSClient` in a process that never launched DBOS. Both must create the successor under
the identifier `SetWorkflowID` assigns, on the queue and partition passed in, or the
deterministic successor that arbitrates concurrent sweeps does not exist.

Seeded rows sit on a queue no process consumes and carry versions no worker runs, so only
what the fork itself did is observed.
"""

import json
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from dbos import DBOS, SetWorkflowID
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from curate_d.config import Settings
from curate_d.runtime.dbos_app import application_name, build_dbos_config
from curate_d.runtime.lease import ExecutorLease

pytestmark = pytest.mark.integration

EXECUTOR = f"spike-{uuid4().hex[:8]}"
TARGET_VERSION = f"9.9.9-spike-{uuid4().hex[:8]}"
SOURCE_VERSION = f"9.9.8-spike-{uuid4().hex[:8]}"
QUEUE = "spike-unconsumed"
PARTITION = "spike-partition"


@DBOS.step()
async def _fork_then_cancel(original: str, successor: str) -> str:
    """Fork `original` under `successor` onto the target version, then cancel it."""
    with SetWorkflowID(successor):
        handle = await DBOS.fork_workflow_async(
            original,
            1,
            application_version=TARGET_VERSION,
            queue_name=QUEUE,
            queue_partition_key=PARTITION,
        )
    await DBOS.cancel_workflow_async(original)
    return handle.get_workflow_id()


@DBOS.workflow()
async def spike_fork_from_step(original: str, successor: str) -> str:
    """Run the fork and the cancel in one step, as the scheduled sweep does."""
    return await _fork_then_cancel(original, successor)


async def _latest_version(engine: AsyncEngine, settings: Settings) -> str | None:
    """Return this application's latest version as DBOS ranks it, if any."""
    async with engine.connect() as connection:
        latest = await connection.scalar(
            text(
                "SELECT version_name FROM dbos.application_versions"
                " WHERE application_name = :app ORDER BY version_timestamp DESC LIMIT 1"
            ),
            {"app": application_name(settings.environment)},
        )
    return None if latest is None else str(latest)


@pytest.fixture
async def dbos(settings: Settings, ledger_engine: AsyncEngine) -> AsyncIterator[None]:
    """Launch DBOS on the target version, holding its executor lease as a worker does.

    Launching a new version makes it DBOS's latest, so the previous latest is restored
    and the target version deleted afterwards.
    """
    previous_latest = await _latest_version(ledger_engine, settings)
    DBOS.destroy()
    lease = ExecutorLease.for_settings(settings, EXECUTOR)
    lease.acquire(timeout=10)
    config = build_dbos_config(settings, executor_id=EXECUTOR)
    config["application_version"] = TARGET_VERSION
    DBOS(config=config)
    DBOS.launch()
    try:
        yield
    finally:
        rows = await DBOS.list_workflows_async(app_version=[TARGET_VERSION, SOURCE_VERSION])
        if rows:
            await DBOS.delete_workflows_async([row.workflow_id for row in rows])
        if previous_latest is not None:
            await DBOS.set_latest_application_version_async(previous_latest)
        DBOS.destroy()
        lease.release()
        async with ledger_engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM dbos.application_versions WHERE version_name = :v"),
                {"v": TARGET_VERSION},
            )


async def _seed(engine: AsyncEngine, workflow_id: str, app_name: str) -> None:
    """Write a PENDING row on the source version, as an abandoned worker leaves one."""
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO dbos.workflow_status"
                " (workflow_uuid, status, name, executor_id, application_version,"
                "  application_name, inputs, serialization, queue_name, queue_partition_key)"
                " VALUES (:id, 'PENDING', 'tests.spike.stranded', 'spike-retired', :version,"
                "  :app, NULL, 'py_pickle', 'spike-original', 'original-partition')"
            ),
            {"id": workflow_id, "version": SOURCE_VERSION, "app": app_name},
        )


async def _rows(engine: AsyncEngine, original: str, successor: str) -> dict[str, dict[str, object]]:
    """Return the original's and the successor's status rows, keyed by identifier."""
    async with engine.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT workflow_uuid, status, application_version, queue_name,"
                " queue_partition_key, forked_from"
                " FROM dbos.workflow_status WHERE workflow_uuid IN (:original, :successor)"
            ),
            {"original": original, "successor": successor},
        )
        return {row["workflow_uuid"]: dict(row) for row in result.mappings()}


async def _delete(engine: AsyncEngine, *workflow_ids: str) -> None:
    """Delete seeded and forked rows a test left behind."""
    async with engine.begin() as connection:
        await connection.execute(
            text("DELETE FROM dbos.workflow_status WHERE workflow_uuid = ANY(:ids)"),
            {"ids": list(workflow_ids)},
        )


def _assert_forked(rows: dict[str, dict[str, object]], original: str, successor: str) -> None:
    """Assert the successor exists under its assigned ID, routed as asked, original cancelled."""
    assert successor in rows, "the fork did not use the identifier SetWorkflowID assigned"
    fork = rows[successor]
    assert fork["forked_from"] == original
    assert fork["application_version"] == TARGET_VERSION
    assert fork["queue_name"] == QUEUE
    assert fork["queue_partition_key"] == PARTITION
    assert rows[original]["status"] == "CANCELLED"


async def test_a_step_forks_under_the_assigned_id_then_cancels(
    dbos: None, settings: Settings, ledger_engine: AsyncEngine
) -> None:
    """From inside a step, the fork honours SetWorkflowID, the queue and the partition."""
    original = f"spike-step-{uuid4()}"
    successor = f"spike-step-successor-{uuid4()}"
    await _seed(ledger_engine, original, application_name(settings.environment))

    returned = await spike_fork_from_step(original, successor)

    assert returned == successor
    _assert_forked(await _rows(ledger_engine, original, successor), original, successor)


_CLIENT_FORK = """
import asyncio, json, sys
from dbos import DBOS, SetWorkflowID, error
from curate_d.config import Settings
from curate_d.runtime.dbos_app import client

original, successor, version, queue, partition = sys.argv[1:6]


async def main() -> str:
    dbos_client = client(Settings())
    try:
        with SetWorkflowID(successor):
            handle = await dbos_client.fork_workflow_async(
                original, 1, application_version=version, queue_name=queue,
                queue_partition_key=partition,
            )
        await dbos_client.cancel_workflow_async(original)
    finally:
        dbos_client.destroy()
    return handle.get_workflow_id()


forked = asyncio.run(main())
# Every DBOS class method needs a DBOS instance; without one it raises DBOSException.
try:
    DBOS.list_workflows(limit=1)
    launched = True
except error.DBOSException:
    launched = False
print(json.dumps({"id": forked, "launched": launched}))
"""


async def test_a_client_that_never_launched_forks_under_the_assigned_id(
    settings: Settings, ledger_engine: AsyncEngine
) -> None:
    """A DBOSClient in a fresh process honours SetWorkflowID, the queue and the partition."""
    original = f"spike-client-{uuid4()}"
    successor = f"spike-client-successor-{uuid4()}"
    await _seed(ledger_engine, original, application_name(settings.environment))
    try:
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                _CLIENT_FORK,
                original,
                successor,
                TARGET_VERSION,
                QUEUE,
                PARTITION,
            ],
            capture_output=True,
            text=True,
            env=os.environ.copy(),
            timeout=60,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        reported = json.loads(completed.stdout.strip().splitlines()[-1])
        assert reported == {"id": successor, "launched": False}
        _assert_forked(await _rows(ledger_engine, original, successor), original, successor)
    finally:
        await _delete(ledger_engine, original, successor)
