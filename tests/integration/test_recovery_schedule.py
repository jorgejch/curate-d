"""The recovery sweep's persisted schedule, ticking on a launched I/O worker.

The schedule is created under a name unique to the run, from the entry `recovery.schedule`
builds with a seconds-field cron, so the deployed `recovery-sweep` row is never touched.
The suite's version is made DBOS's latest for the run, since every tick is enqueued on the
latest version, and the previous latest is restored afterwards.
"""

import asyncio
from collections.abc import Iterator
from uuid import uuid4

import pytest
import sqlalchemy as sa
from dbos import DBOS

from curate_d.config import Settings
from curate_d.runtime import dbos_app, services
from curate_d.runtime.lease import ExecutorLease
from curate_d.runtime.profiles import WorkerProfile
from curate_d.runtime.queues import register_all
from curate_d.worker.main import prepare_services
from curate_d.workflows import recovery

pytestmark = pytest.mark.integration

SCHEDULE = f"recovery-sweep-test-{uuid4().hex[:8]}"
EXECUTOR = f"recovery-schedule-{uuid4().hex[:8]}"


def _latest_version(settings: Settings) -> str | None:
    """Return this application's latest version as DBOS ranks it, if any."""
    engine = sa.create_engine(dbos_app.system_database_url(settings))
    try:
        with engine.connect() as connection:
            latest = connection.scalar(
                sa.text(
                    "SELECT version_name FROM dbos.application_versions"
                    " WHERE application_name = :app ORDER BY version_timestamp DESC LIMIT 1"
                ),
                {"app": dbos_app.application_name(settings.environment)},
            )
    finally:
        engine.dispose()
    return None if latest is None else str(latest)


@pytest.fixture
def io_worker(settings: Settings) -> Iterator[None]:
    """Launch DBOS as an I/O worker on the suite's version, made latest for the test.

    Synchronous: `prepare_services` runs its own event loop, as before a worker's launch.
    """
    previous_latest = _latest_version(settings)
    # Leased as a worker is: the tick's own row is PENDING under this executor, and only a
    # held lease tells the sweep that executor is alive.
    lease = ExecutorLease.for_settings(settings, EXECUTOR, version=dbos_app.APPLICATION_VERSION)
    lease.acquire(timeout=10)
    DBOS.destroy()
    # Configured before the datasource: a destroyed instance still in DBOS's registry
    # counts as launched, so a datasource created first would be refused.
    dbos_app.configure(settings, executor_id=EXECUTOR)
    prepare_services(settings, WorkerProfile.IO)
    DBOS.listen_queues([queue.value for queue in WorkerProfile.IO.queues])
    DBOS.launch()
    register_all()
    DBOS.set_latest_application_version(DBOS.application_version)
    try:
        yield
    finally:
        DBOS.delete_schedule(SCHEDULE)
        ticks = DBOS.list_workflows(schedule_name=SCHEDULE)
        if ticks:
            DBOS.cancel_workflows([tick.workflow_id for tick in ticks])
            DBOS.delete_workflows([tick.workflow_id for tick in ticks])
        if previous_latest is not None:
            DBOS.set_latest_application_version(previous_latest)
        DBOS.destroy()
        services.uninstall()
        lease.release()


async def test_a_tick_runs_on_maintenance_and_records_the_outcome(io_worker: None) -> None:
    """A seconds-field cron fires on a launched I/O worker; the step output reads back."""
    entry = recovery.schedule(Settings(recovery_sweep_schedule="*/2 * * * * *"))
    await DBOS.create_schedule_async(
        schedule_name=SCHEDULE,
        workflow_fn=entry["workflow_fn"],
        schedule=entry["schedule"],
        context=entry["context"],
        queue_name=entry["queue_name"],
    )

    tick = None
    seen: list[tuple[str, str, object]] = []
    for _ in range(150):
        ticks = await DBOS.list_workflows_async(schedule_name=SCHEDULE)
        seen = [(t.workflow_id, t.status, t.error) for t in ticks]
        done = [t for t in ticks if t.status in {"SUCCESS", "ERROR"}]
        if done:
            tick = done[0]
            break
        await asyncio.sleep(0.2)
    assert tick is not None, f"no tick of the recovery schedule finished: {seen}"
    assert tick.status == "SUCCESS", f"the tick failed: {tick.error!r}"
    assert tick.queue_name == "maintenance"
    assert tick.app_version == DBOS.application_version

    [step] = await DBOS.list_workflow_steps_async(tick.workflow_id)
    output = step["output"]
    assert isinstance(output, dict)
    assert set(output) >= {"reenqueued", "forked", "left_running", "failed", "skipped"}
    # The tick's own row is PENDING on this live executor while it sweeps.
    assert isinstance(output["left_running"], int) and output["left_running"] >= 1
