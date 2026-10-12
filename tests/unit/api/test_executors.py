"""Executor operations: the read model, recovery, and the commands on DBOS workflows."""

import asyncio
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from dbos import WorkflowStatus
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

from curate_d.api.configuration import OPERATOR_HEADER
from curate_d.api.executors import (
    RESUMED_BY,
    AdvisoryLock,
    ForkRequest,
    RecoveryReport,
    executors_view,
    refusal_of,
)
from curate_d.runtime.dbos_app import APPLICATION_NAME
from curate_d.runtime.lease import lease_key, version_lease_key
from curate_d.runtime.workflow_ids import (
    operator_fork_workflow_id,
    operator_trigger_workflow_id,
    recover_executor_workflow_id,
)
from tests.unit.api.fake_executors import FakeExecutors, version, workflow_status
from tests.unit.api.fakes import backends, client

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
NOW_MS = int(NOW.timestamp() * 1000)
STARTED = datetime(2026, 10, 11, 9, 0, tzinfo=UTC)


def _lease(executor_id: str, pid: int, *, name: str | None = None) -> AdvisoryLock:
    """Return the advisory lock a worker's lease session holds on its identity."""
    return AdvisoryLock(
        key=lease_key(APPLICATION_NAME, executor_id),
        mode="ExclusiveLock",
        pid=pid,
        application_name=name,
        backend_start=STARTED,
    )


def _version_lease(name: str, pid: int) -> AdvisoryLock:
    """Return the shared lock a worker holds on its version."""
    return AdvisoryLock(key=version_lease_key(APPLICATION_NAME, name), mode="ShareLock", pid=pid)


@pytest.fixture
def fake() -> FakeExecutors:
    """Return an executor backend over in-memory rows and locks."""
    return FakeExecutors()


@pytest.fixture
def http(fake: FakeExecutors) -> Iterator[TestClient]:
    """Return a client over the fake executor backend."""
    with client(replace(backends(), executors=fake)) as test_client:
        yield test_client


def _command(request_id: UUID | None = None, **fields: object) -> dict[str, object]:
    """Return a command body with a request ID."""
    return {"request_id": str(request_id or uuid4()), **fields}


# The read model.


def test_the_view_tells_live_executors_from_stranded_ones() -> None:
    """A dead identity owning PENDING rows is stranded; a live one is not."""
    rows = [
        workflow_status(
            "running", executor_id="io-0", app_version="v2", queue_name="scan", dequeued_at=NOW_MS
        ),
        workflow_status(
            "orphan-1",
            executor_id="io-1",
            app_version="v2",
            queue_name="scan",
            dequeued_at=NOW_MS - 90_000,
        ),
        workflow_status(
            "orphan-2",
            executor_id="io-1",
            app_version="v2",
            queue_name="scan",
            created_at=NOW_MS - 30_000,
        ),
        workflow_status("queued", status="ENQUEUED", app_version="v2", queue_name="scan"),
    ]
    locks = [
        _lease("io-0", 11, name="curate-d executor lease io-0"),
        _version_lease("v2", 11),
    ]
    view = executors_view(rows, rows, locks, [version("v2"), version("v1")], "v2", NOW)
    by_id = {executor.executor_id: executor for executor in view.executors}
    assert set(by_id) == {"io-0", "io-1"}
    live, dead = by_id["io-0"], by_id["io-1"]
    assert (live.live, live.stranded, live.lease_since, live.version) == (
        True,
        False,
        STARTED,
        "v2",
    )
    assert live.holder == "curate-d executor lease io-0"
    assert (dead.live, dead.stranded, dead.holder) == (False, True, None)
    assert dead.oldest_pending_age_seconds == 90.0
    assert [(work.status, work.count) for work in dead.pending] == [("PENDING", 2)]
    assert [(q.queue_name, q.status, q.count) for q in view.queues] == [
        ("scan", "ENQUEUED", 1),
        ("scan", "PENDING", 3),
    ]
    versions = {item.version: item for item in view.versions}
    assert versions["v2"].running and versions["v2"].latest
    assert not versions["v1"].running and not versions["v1"].latest


def test_a_lease_session_announces_an_executor_without_rows() -> None:
    """An idle worker is listed from its lease session's name alone."""
    view = executors_view(
        [], [], [_lease("io-2", 12, name="curate-d executor lease io-2")], [], None, NOW
    )
    assert [(e.executor_id, e.live, e.pending) for e in view.executors] == [("io-2", True, [])]
    assert view.latest_version is None


def test_an_invisible_holder_is_still_live() -> None:
    """Without pg_stat_activity, liveness is pg_locks' alone; since and holder are unknown."""
    lock = AdvisoryLock(key=lease_key(APPLICATION_NAME, "io-0"), mode="ExclusiveLock", pid=13)
    view = executors_view(
        [workflow_status("w", executor_id="io-0", app_version="v1")], [], [lock], [], None, NOW
    )
    (executor,) = view.executors
    assert (executor.live, executor.lease_since, executor.holder) == (True, None, "pid 13")


def test_a_live_build_without_a_version_lease_keeps_its_versions_running() -> None:
    """The second signal, for workers of a build that predates the version lease."""
    rows = [workflow_status("w", executor_id="old-0", app_version="v1")]
    view = executors_view(rows, [], [_lease("old-0", 14)], [version("v1")], "v1", NOW)
    assert view.versions[0].running
    assert view.executors[0].version is None


def test_a_live_worker_on_a_new_version_does_not_keep_old_work_running() -> None:
    """A pod restarted onto v2 never runs its v1 rows; v1 is not running."""
    rows = [workflow_status("w", executor_id="io-0", app_version="v1")]
    locks = [_lease("io-0", 15), _version_lease("v2", 15)]
    view = executors_view(rows, [], locks, [version("v2"), version("v1")], "v2", NOW)
    assert {item.version: item.running for item in view.versions} == {"v2": True, "v1": False}


def test_get_executors_and_one_executor(http: TestClient, fake: FakeExecutors) -> None:
    """Both read routes answer from the same view; an unknown executor is 404."""
    fake.workflows.add(workflow_status("w", executor_id="io-1", app_version="v1"))
    fake.workflows.versions = [version("v1")]
    listed = http.get("/executors")
    assert listed.status_code == 200
    assert listed.json()["latest_version"] == "v1"
    one = http.get("/executors/io-1")
    assert one.status_code == 200 and one.json()["stranded"] is True
    missing = http.get("/executors/io-9")
    assert (missing.status_code, missing.json()["detail"]) == (404, "no executor io-9")


# Recovery.


def test_recovering_a_live_executor_is_refused_before_anything_is_enqueued(
    http: TestClient, fake: FakeExecutors
) -> None:
    """A live executor recovers its own work at startup."""
    fake.locks = [_lease("io-0", 11)]
    response = http.post("/executors/io-0/recover", json=_command())
    assert response.status_code == 409
    assert "alive" in response.json()["detail"]
    assert fake.workflows.calls == []


def test_a_finished_recovery_answers_with_its_report(http: TestClient, fake: FakeExecutors) -> None:
    """Enqueued on the maintenance queue under the request's ID, on the latest version."""
    fake.workflows.versions = [version("v2")]
    fake.workflows.recovery_outcome = {"reenqueued": ("a",), "forked": (("b", "c"),)}
    request_id = uuid4()
    response = http.post(
        "/executors/io-1/recover", json=_command(request_id), headers={OPERATOR_HEADER: "ops@x"}
    )
    assert response.status_code == 200
    body = response.json()
    workflow_id = recover_executor_workflow_id("io-1", str(request_id))
    assert body["workflow_id"] == workflow_id
    assert (body["reenqueued"], body["forked"]) == (["a"], [["b", "c"]])
    options, args = fake.workflows.enqueued[0]
    assert options == {
        "workflow_name": "recover_executor",
        "queue_name": "maintenance",
        "workflow_id": workflow_id,
        "app_version": "v2",
    }
    assert args == ("io-1", "ops@x", str(request_id))


def test_a_repeated_recovery_returns_the_first_report_even_once_the_executor_lives(
    http: TestClient, fake: FakeExecutors
) -> None:
    """The request ID names the workflow; a retry waits on it rather than re-checking."""
    fake.workflows.recovery_outcome = {"reenqueued": ("a",)}
    request_id = uuid4()
    first = http.post("/executors/io-1/recover", json=_command(request_id))
    fake.locks = [_lease("io-1", 11)]
    second = http.post("/executors/io-1/recover", json=_command(request_id))
    assert first.json() == second.json()
    assert len([call for call in fake.workflows.calls if call[0] == "enqueue"]) == 1


def test_a_recovery_still_running_answers_202_with_the_workflow_to_poll(
    http: TestClient, fake: FakeExecutors
) -> None:
    """The API stops waiting; the recovery does not."""
    response = http.post("/executors/io-1/recover", json=_command())
    assert response.status_code == 202
    workflow_id = response.json()["workflow_id"]
    assert http.get(f"/executors/recoveries/{workflow_id}").status_code == 202
    fake.workflows.rows[workflow_id].status = "SUCCESS"
    fake.workflows.rows[workflow_id].output = {"reenqueued": ["a"]}
    done = http.get(f"/executors/recoveries/{workflow_id}")
    assert done.status_code == 200
    assert (done.json()["executor_id"], done.json()["reenqueued"]) == ("io-1", ["a"])


def test_a_recovery_refused_by_the_sweep_answers_409(http: TestClient, fake: FakeExecutors) -> None:
    """The lease was taken between the API's check and the sweep's."""
    fake.workflows.recovery_outcome = {"refused": "executor io-1 is alive (lease held)"}
    response = http.post("/executors/io-1/recover", json=_command())
    assert response.status_code == 409
    assert "alive" in response.json()["detail"]


def test_a_recovery_blocked_only_by_running_versions_answers_409_naming_them(
    http: TestClient, fake: FakeExecutors
) -> None:
    """Nothing could be forked yet: the operator waits for the old workers to go."""
    fake.workflows.recovery_outcome = {
        "skipped": (("a", "version 'old' is running"),),
        "running_versions": ("old",),
    }
    response = http.post("/executors/recover", json=_command())
    assert response.status_code == 409
    assert "old" in response.json()["detail"]


def test_a_failed_recovery_is_unavailable(http: TestClient, fake: FakeExecutors) -> None:
    """A recovery that ended in error has no report to give."""
    request_id = uuid4()
    workflow_id = recover_executor_workflow_id("io-1", str(request_id))
    fake.workflows.add(
        workflow_status(workflow_id, status="ERROR", name="recover_executor", error="boom")
    )
    response = http.post("/executors/io-1/recover", json=_command(request_id))
    assert response.status_code == 503
    assert http.get(f"/executors/recoveries/{workflow_id}").status_code == 503


def test_only_recoveries_are_read_as_recoveries(http: TestClient, fake: FakeExecutors) -> None:
    """Another workflow's output is not served through this route."""
    fake.workflows.add(workflow_status("other", status="SUCCESS", output={"x": 1}))
    assert http.get("/executors/recoveries/other").status_code == 404
    assert http.get("/executors/recoveries/missing").status_code == 404


def test_an_executor_id_that_cannot_name_a_workflow_is_rejected(http: TestClient) -> None:
    """The separator would let two requests share one workflow ID."""
    assert http.post("/executors/a:b/recover", json=_command()).status_code == 422


def test_partial_recoveries_are_not_refusals() -> None:
    """Something was recovered; the skipped rows are reported, not refused."""
    report = RecoveryReport(
        workflow_id="w",
        executor_id=None,
        reenqueued=["a"],
        skipped=[("b", "running")],
        running_versions=["old"],
    )
    assert refusal_of(report) is None


# Cancel.


def test_cancel_is_idempotent(http: TestClient, fake: FakeExecutors) -> None:
    """The first cancel changes the row; a repeat finds it cancelled."""
    fake.workflows.add(workflow_status("w", status="ENQUEUED", queue_name="scan"))
    first = http.post("/workflows/w/cancel", json=_command())
    second = http.post("/workflows/w/cancel", json=_command())
    assert (first.json()["changed"], first.json()["status"]) == (True, "CANCELLED")
    assert (second.json()["changed"], second.json()["status"]) == (False, "CANCELLED")
    assert fake.workflows.calls == [("cancel", "w")]


def test_a_finished_workflow_is_not_cancelled(http: TestClient, fake: FakeExecutors) -> None:
    """Cancelling finished work changes nothing."""
    fake.workflows.add(workflow_status("w", status="SUCCESS"))
    response = http.post("/workflows/w/cancel", json=_command())
    assert (response.json()["changed"], response.json()["status"]) == (False, "SUCCESS")


def test_a_workflow_id_with_slashes_reaches_the_command(
    http: TestClient, fake: FakeExecutors
) -> None:
    """Scan workflow IDs carry provider paths."""
    fake.workflows.add(workflow_status("scan:pcloud:1:/Camera:c1", status="ENQUEUED"))
    response = http.post("/workflows/scan:pcloud:1:/Camera:c1/cancel", json=_command())
    assert response.status_code == 200


@pytest.mark.parametrize("command", ["cancel", "resume"])
def test_commands_on_an_unknown_workflow_are_404(http: TestClient, command: str) -> None:
    """Nothing is created by a command on a workflow that does not exist."""
    response = http.post(f"/workflows/missing/{command}", json=_command())
    assert (response.status_code, response.json()["detail"]) == (404, "no workflow missing")


# Resume.


@pytest.mark.parametrize("state", ["CANCELLED", "MAX_RECOVERY_ATTEMPTS_EXCEEDED"])
def test_resume_requeues_a_stopped_workflow_once(
    http: TestClient, fake: FakeExecutors, state: str
) -> None:
    """The row is marked with the request first; the same request again changes nothing."""
    fake.workflows.add(workflow_status("w", status=state, attributes={"trace": "t"}))
    request_id = uuid4()
    first = http.post("/workflows/w/resume", json=_command(request_id, queue_name="scan"))
    assert (first.json()["changed"], first.json()["status"]) == (True, "ENQUEUED")
    assert fake.workflows.rows["w"].attributes == {"trace": "t", RESUMED_BY: str(request_id)}
    fake.workflows.rows["w"].status = "PENDING"
    again = http.post("/workflows/w/resume", json=_command(request_id))
    assert (again.status_code, again.json()["changed"]) == (200, False)
    assert fake.workflows.calls == [("resume", ("w", "scan"))]


def test_resume_keeps_the_rows_queue_when_none_is_named(
    http: TestClient, fake: FakeExecutors
) -> None:
    """A workflow out of recovery attempts still names its queue."""
    fake.workflows.add(
        workflow_status("w", status="MAX_RECOVERY_ATTEMPTS_EXCEEDED", queue_name="verify")
    )
    http.post("/workflows/w/resume", json=_command())
    assert fake.workflows.calls == [("resume", ("w", "verify"))]


def test_resuming_pending_work_is_refused(http: TestClient, fake: FakeExecutors) -> None:
    """DBOS would requeue it while an executor may still be running it."""
    fake.workflows.add(workflow_status("w", status="PENDING", executor_id="io-0"))
    response = http.post("/workflows/w/resume", json=_command())
    assert response.status_code == 409
    assert "recover" in response.json()["detail"]
    assert fake.workflows.calls == []


@pytest.mark.parametrize("state", ["SUCCESS", "ERROR", "ENQUEUED"])
def test_resuming_finished_or_queued_work_is_refused(
    http: TestClient, fake: FakeExecutors, state: str
) -> None:
    """Only a stopped workflow resumes."""
    fake.workflows.add(workflow_status("w", status=state))
    response = http.post("/workflows/w/resume", json=_command())
    assert response.status_code == 409
    assert state in response.json()["detail"]


# Fork.


def test_a_fork_replayed_with_its_request_id_returns_the_same_successor(
    http: TestClient, fake: FakeExecutors
) -> None:
    """The successor ID is derived from the request; the second call finds it."""
    fake.workflows.versions = [version("v2")]
    fake.workflows.add(
        workflow_status(
            "w", status="ERROR", app_version="v1", queue_name=None, queue_partition_key="p1"
        )
    )
    request_id = uuid4()
    body = _command(request_id, start_step=3, queue_name="transfer-small")
    first = http.post("/workflows/w/fork", json=body)
    second = http.post("/workflows/w/fork", json=body)
    successor = operator_fork_workflow_id(str(request_id))
    assert first.status_code == 200
    assert (first.json()["forked_workflow_id"], first.json()["changed"]) == (successor, True)
    assert first.json()["application_version"] == "v2"
    assert (second.json()["forked_workflow_id"], second.json()["changed"]) == (successor, False)
    assert [call for call in fake.workflows.calls if call[0] == "fork"] == [
        ("fork", ("w", 3, "v2", "transfer-small", "p1"))
    ]


def test_a_fork_racing_its_own_retry_accepts_the_committed_successor(
    fake: FakeExecutors,
) -> None:
    """The primary-key conflict is accepted when the row there is this fork."""
    fake.workflows.add(workflow_status("w", status="ERROR"))
    request = ForkRequest(request_id=uuid4(), start_step=1, application_version="v1")
    successor = operator_fork_workflow_id(str(request.request_id))
    original_status = fake._status
    reads = 0

    async def blind_once(workflow_id: str, *, output: bool = False) -> WorkflowStatus | None:
        """Miss the successor on the first read, as a concurrent retry would."""
        nonlocal reads
        if workflow_id == successor and reads == 0:
            reads += 1
            fake.workflows.add(workflow_status(successor, forked_from="w"))
            return None
        return await original_status(workflow_id, output=output)

    fake._status = blind_once  # type: ignore[method-assign]
    result = asyncio.run(fake.fork("w", request))
    assert (result.forked_workflow_id, result.changed) == (successor, False)


def test_a_fork_whose_successor_id_is_unrelated_work_is_refused(
    http: TestClient, fake: FakeExecutors
) -> None:
    """Never report someone else's workflow as this fork."""
    fake.workflows.add(workflow_status("w", status="ERROR"))
    request_id = uuid4()
    fake.workflows.add(workflow_status(operator_fork_workflow_id(str(request_id))))
    response = http.post("/workflows/w/fork", json=_command(request_id, start_step=1))
    assert response.status_code == 409
    assert "unrelated" in response.json()["detail"]


def test_a_fork_onto_a_partitioned_queue_needs_a_partition(
    http: TestClient, fake: FakeExecutors
) -> None:
    """A partitioned queue cannot dequeue a row without one."""
    fake.workflows.add(workflow_status("w", status="ERROR"))
    response = http.post(
        "/workflows/w/fork", json=_command(start_step=1, queue_name="transfer-large")
    )
    assert response.status_code == 422


def test_a_fork_onto_an_unpartitioned_queue_drops_the_partition(
    http: TestClient, fake: FakeExecutors
) -> None:
    """A partition key on an unpartitioned queue is meaningless."""
    fake.workflows.add(workflow_status("w", status="ERROR", queue_partition_key="p1"))
    http.post("/workflows/w/fork", json=_command(start_step=1, queue_name="maintenance"))
    assert [call for call in fake.workflows.calls if call[0] == "fork"] == [
        ("fork", ("w", 1, None, "maintenance", None))
    ]


def test_a_fork_needs_a_positive_start_step(http: TestClient, fake: FakeExecutors) -> None:
    """Step numbers start at one."""
    fake.workflows.add(workflow_status("w", status="ERROR"))
    assert http.post("/workflows/w/fork", json=_command(start_step=0)).status_code == 422
    assert http.post("/workflows/missing/fork", json=_command(start_step=1)).status_code == 404


# Trigger.


def test_a_trigger_repeated_with_one_request_id_enqueues_once(
    http: TestClient, fake: FakeExecutors
) -> None:
    """The run's ID is derived from the request, not the time it fired."""
    fake.workflows.versions = [version("v2")]
    fake.workflows.schedules["prune"] = {
        "schedule_id": "s",
        "schedule_name": "prune",
        "workflow_name": "prune_history",
        "workflow_class_name": None,
        "schedule": "0 5 * * *",
        "status": "ACTIVE",
        "context": {"k": "v"},
        "last_fired_at": None,
        "automatic_backfill": False,
        "cron_timezone": None,
        "queue_name": "maintenance",
        "application_name": "curate-d-local",
    }
    request_id = uuid4()
    first = http.post("/schedules/prune/trigger", json=_command(request_id))
    second = http.post("/schedules/prune/trigger", json=_command(request_id))
    workflow_id = operator_trigger_workflow_id("prune", str(request_id))
    assert (first.json()["workflow_id"], first.json()["changed"]) == (workflow_id, True)
    assert (second.json()["workflow_id"], second.json()["changed"]) == (workflow_id, False)
    enqueues = [call for call in fake.workflows.calls if call[0] == "enqueue"]
    assert len(enqueues) == 1
    options, args = fake.workflows.enqueued[0]
    assert options == {
        "workflow_name": "prune_history",
        "queue_name": "maintenance",
        "workflow_id": workflow_id,
        "app_version": "v2",
    }
    assert isinstance(args[0], datetime) and args[1] == {"k": "v"}


def test_triggering_an_unknown_schedule_is_404(http: TestClient) -> None:
    """Nothing is enqueued for a schedule that does not exist."""
    response = http.post("/schedules/missing/trigger", json=_command())
    assert (response.status_code, response.json()["detail"]) == (404, "no schedule missing")


# The operator's record.


def test_every_command_logs_its_operator_and_request(http: TestClient, fake: FakeExecutors) -> None:
    """The proxy's header and the request ID reach both log lines; no audit entry is made."""
    fake.workflows.add(workflow_status("w", status="ENQUEUED"))
    request_id = uuid4()
    with capture_logs() as events:
        http.post(
            "/workflows/w/cancel", json=_command(request_id), headers={OPERATOR_HEADER: "ops@x"}
        )
    lines = [event for event in events if event["event"].startswith("operator_command.")]
    assert [line["event"] for line in lines] == [
        "operator_command.issued",
        "operator_command.completed",
    ]
    assert {(line["operator"], line["request_id"], line["command"]) for line in lines} == {
        ("ops@x", str(request_id), "cancel")
    }


def test_a_refused_command_logs_the_refusal(http: TestClient, fake: FakeExecutors) -> None:
    """The completed line says why nothing changed."""
    fake.workflows.add(workflow_status("w", status="PENDING"))
    with capture_logs() as events:
        http.post("/workflows/w/resume", json=_command())
    completed = [event for event in events if event["event"] == "operator_command.completed"]
    assert completed and str(completed[0]["result"]).startswith("refused:")
    assert completed[0]["operator"] == "control-plane"
