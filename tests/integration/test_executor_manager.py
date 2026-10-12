"""Executor operations through the API, against real leases, DBOS rows and a launched worker.

The test process is the worker: it launches DBOS holding its executor and version leases,
listens to the maintenance queue the API enqueues recoveries on, and runs what they
re-enqueue. The API under test only reads `pg_locks` and calls `DBOSClient`, as deployed.
Rows are seeded through a registered no-op workflow on a queue nothing consumes, then
rewritten to look abandoned, so a recovered row can actually run to completion here.
"""

import asyncio
import os
import signal
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from datetime import datetime
from uuid import uuid4

import pytest
from dbos import DBOS, EnqueueOptions, SetWorkflowID, WorkflowHandleAsync
from dbos._utils import INTERNAL_QUEUE_NAME
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from curate_d.api.app import create_app
from curate_d.api.executors import ExecutorView
from curate_d.config import Settings
from curate_d.runtime import dbos_app, services
from curate_d.runtime.datasource import create_ledger_datasource
from curate_d.runtime.lease import ExecutorLease
from curate_d.runtime.queues import register_all_async
from curate_d.runtime.workflow_ids import operator_fork_workflow_id, operator_trigger_workflow_id
from curate_d.workflows import executors as _recovery_workflow  # noqa: F401 - registers it

pytestmark = pytest.mark.integration

UNCONSUMED = "executor-manager-unconsumed"
SETTLE_SECONDS = 30.0


@DBOS.workflow(name="tests.executor_manager.noop")
async def _noop(value: str) -> str:
    """Return `value`; the work a recovered row runs."""
    return value


@DBOS.workflow(name="tests.executor_manager.tick")
async def _tick(scheduled_at: datetime, context: object) -> None:
    """Do nothing; the work a triggered schedule runs."""


@pytest.fixture
def worker_id() -> str:
    """Return this test's worker identity."""
    return f"mgr-worker-{uuid4().hex[:8]}"


@pytest.fixture
async def worker(settings: Settings, worker_id: str) -> AsyncIterator[ExecutorLease]:
    """Launch DBOS as a leased worker listening to the maintenance queue."""
    lease = ExecutorLease.for_settings(settings, worker_id, version=dbos_app.APPLICATION_VERSION)
    lease.acquire(timeout=10)
    DBOS.destroy()
    dbos_app.configure(settings, executor_id=worker_id)
    datasource = await create_ledger_datasource(settings)
    services.install(services.Services(settings=settings, datasource=datasource))
    DBOS.listen_queues(["maintenance"])
    DBOS.launch()
    await register_all_async()
    # The API enqueues recoveries on the latest version; make it this process's.
    await DBOS.set_latest_application_version_async(dbos_app.APPLICATION_VERSION)
    try:
        yield lease
    finally:
        DBOS.destroy()
        services.uninstall()
        await datasource.engine.dispose()
        lease.release()


@pytest.fixture
def http(settings: Settings, worker: ExecutorLease) -> Iterator[TestClient]:
    """Return the API over the real backends, waiting long enough for a recovery."""
    configured = settings.model_copy(update={"executor_recovery_wait_seconds": SETTLE_SECONDS})
    with TestClient(create_app(configured)) as client:
        yield client


@pytest.fixture
async def seeded(ledger_engine: AsyncEngine) -> AsyncIterator[list[str]]:
    """Collect the workflow IDs a test seeds, and delete them and their forks afterwards."""
    ids: list[str] = []
    yield ids
    async with ledger_engine.begin() as connection:
        await connection.execute(
            text(
                "DELETE FROM dbos.workflow_status"
                " WHERE workflow_uuid = ANY(:ids) OR forked_from = ANY(:ids)"
            ),
            {"ids": ids},
        )


async def _seed(
    engine: AsyncEngine,
    seeded: list[str],
    *,
    executor_id: str,
    version: str | None = None,
    status: str = "PENDING",
    queue_name: str = UNCONSUMED,
) -> str:
    """Enqueue a no-op, then make it look owned and left by `executor_id`.

    Enqueued on a version nobody runs, so no worker dequeues it before it is rewritten.
    Recovery keeps a row's queue: one seeded on DBOS's internal queue runs here once
    re-enqueued, one on `UNCONSUMED` stays put.
    """
    workflow_id = f"mgr-{uuid4()}"
    seeded.append(workflow_id)
    client = dbos_app.client(Settings())
    try:
        options: EnqueueOptions = {
            "workflow_name": "tests.executor_manager.noop",
            "queue_name": queue_name,
            "workflow_id": workflow_id,
            "app_version": f"unclaimed-{uuid4().hex[:8]}",
        }
        await client.enqueue_async(options, workflow_id)
    finally:
        client.destroy()
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE dbos.workflow_status SET status = :status, executor_id = :executor,"
                " application_version = :version, created_at = created_at - 60000"
                " WHERE workflow_uuid = :id"
            ),
            {
                "status": status,
                "executor": executor_id,
                "version": version or dbos_app.APPLICATION_VERSION,
                "id": workflow_id,
            },
        )
    return workflow_id


async def _status(engine: AsyncEngine, workflow_id: str) -> dict[str, object]:
    """Return a row's status, executor, version and lineage."""
    async with engine.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT status, executor_id, application_version, forked_from, queue_name"
                " FROM dbos.workflow_status WHERE workflow_uuid = :id"
            ),
            {"id": workflow_id},
        )
        return dict(result.mappings().one())


async def _audit_count(engine: AsyncEngine) -> int:
    """Return how many audit entries exist."""
    async with engine.connect() as connection:
        return int(await connection.scalar(text("SELECT count(*) FROM audit_entries")) or 0)


async def _settled(engine: AsyncEngine, workflow_id: str, status: str) -> None:
    """Wait for a row to reach `status`, reporting the row as last seen if it does not."""
    deadline = asyncio.get_running_loop().time() + SETTLE_SECONDS
    row = await _status(engine, workflow_id)
    while row["status"] != status:
        assert asyncio.get_running_loop().time() < deadline, f"{workflow_id} stayed {row}"
        await asyncio.sleep(0.2)
        row = await _status(engine, workflow_id)


async def _post(http: TestClient, path: str, *, json: dict[str, object]) -> Response:
    """POST off the event loop, which the worker's async workflows run on meanwhile."""
    return await asyncio.to_thread(lambda: http.post(path, json=json))


async def _get(http: TestClient, path: str) -> Response:
    """GET off the event loop, which the worker's async workflows run on meanwhile."""
    return await asyncio.to_thread(lambda: http.get(path))


def _executor(view: dict[str, object], executor_id: str) -> dict[str, object]:
    """Return one executor's entry from `GET /executors`."""
    executors = view["executors"]
    assert isinstance(executors, list)
    return next(entry for entry in executors if entry["executor_id"] == executor_id)


async def test_the_view_shows_live_and_stranded_executors(
    http: TestClient,
    ledger_engine: AsyncEngine,
    seeded: list[str],
    worker_id: str,
) -> None:
    """A leased worker is live since its session began; a dead owner of PENDING work is stranded."""
    dead = f"mgr-dead-{uuid4().hex[:8]}"
    await _seed(ledger_engine, seeded, executor_id=dead)
    await _seed(ledger_engine, seeded, executor_id=dead, status="ENQUEUED")

    view = (await _get(http, "/executors")).json()

    live = ExecutorView.model_validate(_executor(view, worker_id))
    assert live.live is True
    assert live.lease_since is not None
    assert live.holder == f"curate-d executor lease {worker_id}"
    assert live.version == dbos_app.APPLICATION_VERSION
    stranded = ExecutorView.model_validate(_executor(view, dead))
    assert (stranded.live, stranded.stranded) == (False, True)
    assert stranded.oldest_pending_age_seconds is not None
    assert stranded.oldest_pending_age_seconds >= 0
    assert {(work.status, work.count) for work in stranded.pending} == {
        ("PENDING", 1),
        ("ENQUEUED", 1),
    }
    depths = {(q["queue_name"], q["status"]): q["count"] for q in view["queues"]}
    assert depths[(UNCONSUMED, "ENQUEUED")] >= 1
    versions = {entry["version"]: entry for entry in view["versions"]}
    assert versions[dbos_app.APPLICATION_VERSION]["running"] is True
    assert view["latest_version"] == dbos_app.APPLICATION_VERSION
    assert (await _get(http, f"/executors/{dead}")).json()["stranded"] is True


async def test_recover_refuses_a_live_executor_and_touches_nothing(
    http: TestClient, ledger_engine: AsyncEngine, seeded: list[str], worker_id: str
) -> None:
    """The worker holds its lease: it recovers its own work."""
    workflow_id = await _seed(ledger_engine, seeded, executor_id=worker_id)
    before = await _status(ledger_engine, workflow_id)
    response = await _post(
        http, f"/executors/{worker_id}/recover", json={"request_id": str(uuid4())}
    )
    assert response.status_code == 409
    assert "alive" in response.json()["detail"]
    assert await _status(ledger_engine, workflow_id) == before


async def test_a_killed_then_removed_executor_is_recovered_once(
    http: TestClient, ledger_engine: AsyncEngine, seeded: list[str]
) -> None:
    """SIGKILL leaves PENDING rows; once the lease is gone, recover re-enqueues and runs them."""
    gone = f"gone-{uuid4().hex[:8]}"
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys, time\n"
            "from curate_d.config import Settings\n"
            "from curate_d.runtime.lease import ExecutorLease\n"
            "lease = ExecutorLease.for_settings(Settings(), sys.argv[1], version=sys.argv[2])\n"
            "lease.acquire(timeout=10)\n"
            "print('held', flush=True)\n"
            "time.sleep(600)\n",
            gone,
            dbos_app.APPLICATION_VERSION,
        ],
        stdout=subprocess.PIPE,
        text=True,
        env=os.environ.copy(),
    )
    try:
        assert holder.stdout is not None
        # The holder's own log lines share its stdout; "held" is printed after them.
        for line in holder.stdout:
            if line.strip() == "held":
                break
        rows = [
            await _seed(ledger_engine, seeded, executor_id=gone, queue_name=INTERNAL_QUEUE_NAME)
            for _ in range(2)
        ]
        audit_before = await _audit_count(ledger_engine)
        assert _executor((await _get(http, "/executors")).json(), gone)["live"] is True
        refused = await _post(http, f"/executors/{gone}/recover", json={"request_id": str(uuid4())})
        assert refused.status_code == 409
    finally:
        holder.send_signal(signal.SIGKILL)
        holder.wait(timeout=10)

    async with asyncio.timeout(SETTLE_SECONDS):
        while _executor((await _get(http, "/executors")).json(), gone)["live"]:
            await asyncio.sleep(0.2)

    request_id = str(uuid4())
    first = await _post(http, f"/executors/{gone}/recover", json={"request_id": request_id})
    assert first.status_code == 200, first.text
    report = first.json()
    assert sorted(report["reenqueued"]) == sorted(rows)
    assert report["refused"] is None
    for workflow_id in rows:
        await _settled(ledger_engine, workflow_id, "SUCCESS")

    repeat = await _post(http, f"/executors/{gone}/recover", json={"request_id": request_id})
    assert repeat.json() == report
    # Its work done elsewhere, nothing names the identity any more: a new request finds none.
    again = await _post(http, f"/executors/{gone}/recover", json={"request_id": str(uuid4())})
    assert again.status_code == 404
    assert await _audit_count(ledger_engine) == audit_before
    seeded.append(report["workflow_id"])


async def test_work_of_a_running_older_version_is_not_forked_until_it_stops(
    http: TestClient, ledger_engine: AsyncEngine, seeded: list[str], settings: Settings
) -> None:
    """Blue-green: refused while an old worker holds its version, forked once it is gone."""
    old = f"9.9.7-mgr-{uuid4().hex[:8]}"
    dead = f"mgr-old-dead-{uuid4().hex[:8]}"
    workflow_id = await _seed(ledger_engine, seeded, executor_id=dead, version=old)
    old_worker = ExecutorLease.for_settings(settings, f"mgr-old-{uuid4().hex[:8]}", version=old)
    old_worker.acquire(timeout=10)
    try:
        refused = await _post(http, f"/executors/{dead}/recover", json={"request_id": str(uuid4())})
        assert refused.status_code == 409
        assert old in refused.json()["detail"]
        assert (await _status(ledger_engine, workflow_id))["status"] == "PENDING"
    finally:
        old_worker.release()

    recovered = await _post(http, f"/executors/{dead}/recover", json={"request_id": str(uuid4())})
    assert recovered.status_code == 200, recovered.text
    ((original, successor),) = recovered.json()["forked"]
    assert original == workflow_id
    seeded.append(recovered.json()["workflow_id"])
    fork = await _status(ledger_engine, successor)
    assert fork["forked_from"] == workflow_id
    assert fork["application_version"] == dbos_app.APPLICATION_VERSION
    assert (await _status(ledger_engine, workflow_id))["status"] == "CANCELLED"


async def test_the_commands_are_idempotent_per_request(
    http: TestClient, ledger_engine: AsyncEngine, seeded: list[str]
) -> None:
    """Resume of PENDING is refused; a repeated fork or trigger lands once; no audit entry."""
    audit_before = await _audit_count(ledger_engine)
    pending = await _seed(ledger_engine, seeded, executor_id=f"mgr-dead-{uuid4().hex[:8]}")
    resumed = await _post(http, f"/workflows/{pending}/resume", json={"request_id": str(uuid4())})
    assert resumed.status_code == 409
    assert (await _status(ledger_engine, pending))["status"] == "PENDING"

    queued = await _seed(ledger_engine, seeded, executor_id="mgr-x", status="ENQUEUED")
    cancel_id = str(uuid4())
    for _ in range(2):
        cancelled = await _post(http, f"/workflows/{queued}/cancel", json={"request_id": cancel_id})
        assert cancelled.status_code == 200
    assert (await _status(ledger_engine, queued))["status"] == "CANCELLED"
    resume_id = str(uuid4())
    first_resume = await _post(http, f"/workflows/{queued}/resume", json={"request_id": resume_id})
    assert first_resume.json()["changed"] is True
    second_resume = await _post(http, f"/workflows/{queued}/resume", json={"request_id": resume_id})
    assert (second_resume.status_code, second_resume.json()["changed"]) == (200, False)
    await _settled(ledger_engine, queued, "SUCCESS")

    done = f"mgr-done-{uuid4()}"
    seeded.append(done)
    with SetWorkflowID(done):
        handle: WorkflowHandleAsync[str] = await DBOS.start_workflow_async(_noop, "done")
    assert await handle.get_result() == "done"
    fork_id = str(uuid4())
    body = {"request_id": fork_id, "start_step": 1}
    forks = [await _post(http, f"/workflows/{done}/fork", json=body) for _ in range(2)]
    assert [response.status_code for response in forks] == [200, 200]
    assert [response.json()["changed"] for response in forks] == [True, False]
    successor = operator_fork_workflow_id(fork_id)
    async with ledger_engine.connect() as connection:
        successors = await connection.scalar(
            text("SELECT count(*) FROM dbos.workflow_status WHERE forked_from = :id"),
            {"id": done},
        )
    assert successors == 1
    await _settled(ledger_engine, successor, "SUCCESS")

    schedule = f"mgr-tick-{uuid4().hex[:8]}"
    await DBOS.apply_schedules_async(
        [
            {
                "schedule_name": schedule,
                "workflow_fn": _tick,
                "schedule": "0 0 1 1 *",
                "context": None,
            }
        ]
    )
    try:
        trigger_id = str(uuid4())
        triggers = [
            await _post(http, f"/schedules/{schedule}/trigger", json={"request_id": trigger_id})
            for _ in range(2)
        ]
        assert [response.json()["changed"] for response in triggers] == [True, False]
        run = operator_trigger_workflow_id(schedule, trigger_id)
        seeded.append(run)
        await _settled(ledger_engine, run, "SUCCESS")
        async with ledger_engine.connect() as connection:
            runs = await connection.scalar(
                text("SELECT count(*) FROM dbos.workflow_status WHERE workflow_uuid LIKE :id"),
                {"id": f"operator-trigger:{schedule}:%"},
            )
        assert runs == 1
    finally:
        await DBOS.delete_schedule_async(schedule)
    assert await _audit_count(ledger_engine) == audit_before


async def test_recovering_an_executor_with_no_lease_and_no_work_is_404(
    http: TestClient, ledger_engine: AsyncEngine, seeded: list[str]
) -> None:
    """An identity owning only finished work is unknown; no recovery is enqueued for it."""
    unknown = f"mgr-unknown-{uuid4().hex[:8]}"
    finished = await _seed(ledger_engine, seeded, executor_id=unknown, status="SUCCESS")
    request_id = str(uuid4())
    response = await _post(http, f"/executors/{unknown}/recover", json={"request_id": request_id})
    assert response.status_code == 404
    assert (await _get(http, f"/executors/{unknown}")).status_code == 404
    async with ledger_engine.connect() as connection:
        enqueued = await connection.scalar(
            text("SELECT count(*) FROM dbos.workflow_status WHERE workflow_uuid LIKE :id"),
            {"id": f"recover-executor:{unknown}:%"},
        )
    assert enqueued == 0
    assert (await _status(ledger_engine, finished))["status"] == "SUCCESS"


async def test_a_resume_onto_a_partitioned_queue_without_a_partition_is_refused(
    http: TestClient, ledger_engine: AsyncEngine, seeded: list[str]
) -> None:
    """The cancelled row stays cancelled and unmarked rather than stuck on a partitioned queue."""
    queued = await _seed(ledger_engine, seeded, executor_id="mgr-x", status="ENQUEUED")
    cancelled = await _post(http, f"/workflows/{queued}/cancel", json={"request_id": str(uuid4())})
    assert cancelled.status_code == 200
    response = await _post(
        http,
        f"/workflows/{queued}/resume",
        json={"request_id": str(uuid4()), "queue_name": "transfer-large"},
    )
    assert response.status_code == 422
    row = await _status(ledger_engine, queued)
    assert (row["status"], row["queue_name"]) == ("CANCELLED", None)
    async with ledger_engine.connect() as connection:
        attributes = await connection.scalar(
            text("SELECT attributes FROM dbos.workflow_status WHERE workflow_uuid = :id"),
            {"id": queued},
        )
    assert not attributes


async def test_conductor_style_writes_stay_out_of_the_api(http: TestClient) -> None:
    """No rewind route: rewinding is not an operator command here (issue #2)."""
    paths = (await _get(http, "/openapi.json")).json()["paths"]
    assert not any("rewind" in path for path in paths)
