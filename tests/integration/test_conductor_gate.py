"""The Conductor gate against a local fake Conductor, real DBOS and the real database.

The unit suite pins the gate's classification and refusals over fake connections. What
needs the real pieces is the whole path: DBOS's own websocket thread connecting through
the gate, a read-only request reaching DBOS's dispatcher and coming back, every write
refused without DBOS's handlers ever running against the system tables or anything reaching
the ledger, and a worker that keeps executing workflows while Conductor or its proxy
refuses it.
"""

import base64
import gzip
import pickle
import threading
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Final, Protocol
from uuid import uuid4

import pytest
from dbos import DBOS, ScheduleInput, SetWorkflowID
from dbos._dbos import _get_dbos_instance
from dbos._utils import GlobalParams
from structlog.typing import EventDict

from curate_d.config import Settings
from curate_d.runtime import dbos_app
from curate_d.runtime.profiles import WorkerProfile
from tests.integration.conductor_harness import (
    ConnectProxy,
    FakeConductor,
    capture_gate_logs,
    command_entry_types,
    launch_worker,
    sql,
    stop_worker,
    wait_for_event,
    workflow_status,
)

pytestmark = pytest.mark.integration

KEY: Final = "test-conductor-key"
READ_ONLY: Final = "Conductor is read-only in curate.d"

# Released by the test that started the workflow; keyed by workflow ID.
_RELEASES: dict[str, threading.Event] = {}


@DBOS.workflow()
def conductor_gate_echo(value: str) -> str:
    """Return the value: proof that the worker executes workflows."""
    return value


@DBOS.workflow()
def conductor_gate_held() -> str:
    """Wait until the test releases this workflow."""
    _RELEASES[DBOS.workflow_id or ""].wait(timeout=60)
    return "released"


@DBOS.workflow()
def conductor_gate_scheduled(scheduled_at: datetime, context: object) -> None:
    """Do nothing; a firing of it would show a backfill got through."""


class _Canary:
    """Touches a file when unpickled: a stand-in for code an import payload could run."""

    def __init__(self, path: Path) -> None:
        """Touch `path` on unpickling."""
        self.path = path

    def __reduce__(self) -> tuple[Callable[[Path], None], tuple[Path]]:
        """Unpickle as a call to `Path.touch`."""
        return Path.touch, (self.path,)


class Launch(Protocol):
    """Start a worker connected to the fake Conductor and return its executor ID."""

    def __call__(
        self, profile: WorkerProfile = WorkerProfile.IO, *, proxy_url: str | None = None
    ) -> str:
        """Launch a worker of `profile`, through `proxy_url` when given."""
        ...


@pytest.fixture
def fake_conductor() -> Iterator[FakeConductor]:
    """Serve a fake Conductor for the test."""
    server = FakeConductor()
    yield server
    server.close()


@pytest.fixture
def logs(monkeypatch: pytest.MonkeyPatch) -> list[EventDict]:
    """Capture the gate's log events."""
    return capture_gate_logs(monkeypatch)


@pytest.fixture
def launch(monkeypatch: pytest.MonkeyPatch, fake_conductor: FakeConductor) -> Iterator[Launch]:
    """Start a worker connected to the fake Conductor; stop it after the test.

    Yields:
        A function taking the profile and an optional `proxy_url`, which sets the
        Conductor settings in the environment, launches the worker and returns its
        executor ID.
    """

    def start(profile: WorkerProfile = WorkerProfile.IO, *, proxy_url: str | None = None) -> str:
        """Launch a worker under a fresh, stable executor ID."""
        executor_id = f"conductor-gate-{profile.value}-{uuid4().hex[:8]}"
        monkeypatch.setenv("CURATE_D_CONDUCTOR_ENABLED", "true")
        monkeypatch.setenv("CURATE_D_CONDUCTOR_KEY", KEY)
        monkeypatch.setenv("CURATE_D_ENVIRONMENT", "local")
        monkeypatch.setenv("CURATE_D_CONDUCTOR_URL", fake_conductor.url)
        if proxy_url is None:
            monkeypatch.delenv("CURATE_D_CONDUCTOR_PROXY_URL", raising=False)
        else:
            monkeypatch.setenv("CURATE_D_CONDUCTOR_PROXY_URL", proxy_url)
        assert launch_worker(Settings(), profile, executor_id) is not None
        return executor_id

    yield start
    stop_worker()


def _echo(value: str) -> str:
    """Run the echo workflow to completion under a fresh ID and return that ID."""
    workflow_id = f"conductor-gate-echo-{uuid4()}"
    with SetWorkflowID(workflow_id):
        assert conductor_gate_echo(value) == value
    return workflow_id


@pytest.mark.parametrize("profile", list(WorkerProfile))
def test_executor_info_reports_the_stable_identity_and_profile(
    launch: Launch, fake_conductor: FakeConductor, profile: WorkerProfile
) -> None:
    """Each profile answers EXECUTOR_INFO with its configured ID, version and profile."""
    executor_id = launch(profile)

    info = fake_conductor.executor_info()

    assert info["executor_id"] == executor_id
    assert info["executor_metadata"] == {"profile": profile.value, "executor_id": executor_id}
    assert info["application_version"] == dbos_app.APPLICATION_VERSION
    assert info["error_message"] is None
    assert fake_conductor.handshakes == [f"/websocket/curate-d-local/{KEY}"]


def test_a_workflow_listing_round_trips(launch: Launch, fake_conductor: FakeConductor) -> None:
    """LIST_WORKFLOWS reaches DBOS's dispatcher and returns the named workflow."""
    launch()
    workflow_id = _echo("listed")

    response = fake_conductor.request(
        "list_workflows",
        body={"workflow_uuids": [workflow_id], "load_input": False, "load_output": False},
    )

    assert response["error_message"] is None
    output = response["output"]
    assert isinstance(output, list)
    assert [(row["WorkflowUUID"], row["Status"]) for row in output] == [(workflow_id, "SUCCESS")]


def test_retention_is_refused_and_deletes_nothing(
    launch: Launch, fake_conductor: FakeConductor, settings: Settings
) -> None:
    """A completed workflow older than the requested cutoff survives a RETENTION request."""
    launch()
    old = f"conductor-gate-retention-{uuid4()}"
    # Ancient, and the cutoff just after it: a gate regression could delete only this row.
    sql(
        settings,
        "INSERT INTO dbos.workflow_status (workflow_uuid, status, name, application_name,"
        " application_version, created_at, updated_at, serialization)"
        " VALUES (%(id)s, 'SUCCESS', 'tests.conductor.retention', %(app)s,"
        " '0.0.0-conductor-retention', 1000, 1000, 'py_pickle')",
        {"id": old, "app": GlobalParams.app_name},
    )
    try:
        response = fake_conductor.request(
            "retention",
            body={
                "gc_cutoff_epoch_ms": 2000,
                "gc_rows_threshold": None,
                "gc_batch_size": None,
                "timeout_cutoff_epoch_ms": None,
            },
        )

        assert response["type"] == "retention"
        assert "'retention'" in str(response["error_message"])
        assert "success" not in response
        assert workflow_status(settings, old) == "SUCCESS"
    finally:
        sql(settings, "DELETE FROM dbos.workflow_status WHERE workflow_uuid = %(id)s", {"id": old})


def _hold(workflow_id: str) -> None:
    """Start the held workflow under `workflow_id`; the test releases it."""
    _RELEASES[workflow_id] = threading.Event()
    with SetWorkflowID(workflow_id):
        DBOS.start_workflow(conductor_gate_held)


def _requests_naming(settings: Settings, request_id: str) -> list[tuple[object, ...]]:
    """Return every workflow whose ID carries a request ID, as a deterministic enqueue would."""
    return sql(
        settings,
        "SELECT workflow_uuid FROM dbos.workflow_status WHERE strpos(workflow_uuid, %(id)s) > 0",
        {"id": request_id},
    )


def test_every_former_write_command_is_refused_and_changes_nothing(
    launch: Launch, fake_conductor: FakeConductor, settings: Settings
) -> None:
    """The commands once audited are refused: no entry, no workflow, no change to a target."""
    launch()
    running, cancelled = (f"conductor-gate-target-{uuid4()}" for _ in range(2))
    active, paused = (f"conductor-gate-schedule-{uuid4().hex[:8]}" for _ in range(2))
    _hold(running)
    _hold(cancelled)
    DBOS.cancel_workflow(cancelled)
    # Fires on 29 February at midnight: never while the test runs.
    DBOS.apply_schedules(
        [
            ScheduleInput(
                schedule_name=name, workflow_fn=conductor_gate_scheduled, schedule="0 0 0 29 2 *"
            )
            for name in (active, paused)
        ]
    )
    DBOS.pause_schedule(paused)
    try:
        responses = [
            fake_conductor.request("cancel", workflow_id=running),
            fake_conductor.request("resume", workflow_id=cancelled),
            fake_conductor.request("pause_schedule", schedule_name=active),
            fake_conductor.request("resume_schedule", schedule_name=paused),
            fake_conductor.request("trigger_schedule", schedule_name=active),
        ]

        for response in responses:
            request_id = str(response["request_id"])
            assert response["error_message"] == (
                f"curate.d refuses Conductor command type {response['type']!r}: {READ_ONLY}"
            )
            assert command_entry_types(settings, request_id) == []
            assert _requests_naming(settings, request_id) == []
        assert [response["type"] for response in responses] == [
            "cancel",
            "resume",
            "pause_schedule",
            "resume_schedule",
            "trigger_schedule",
        ]

        assert (workflow_status(settings, running), workflow_status(settings, cancelled)) == (
            "PENDING",
            "CANCELLED",
        )
        schedules = [DBOS.get_schedule(name) for name in (active, paused)]
        assert [found["status"] if found else None for found in schedules] == ["ACTIVE", "PAUSED"]
        assert DBOS.list_workflows(schedule_name=active, load_input=False, load_output=False) == []
    finally:
        for name in (active, paused):
            DBOS.delete_schedule(name)
        for target in (running, cancelled):
            _RELEASES.pop(target).set()


@pytest.mark.parametrize(
    ("message_type", "fields"),
    [
        pytest.param("delete", {"delete_children": False}, id="delete"),
        pytest.param("fork_workflow", {"body": {"start_step": 1}}, id="fork"),
        pytest.param("rewind_workflow", {"body": {"start_step": 1}}, id="rewind"),
    ],
)
def test_an_unsafe_command_is_refused_and_changes_nothing(
    launch: Launch,
    fake_conductor: FakeConductor,
    settings: Settings,
    message_type: str,
    fields: dict[str, object],
) -> None:
    """A delete, fork or rewind of a finished workflow leaves it, and creates nothing."""
    launch()
    target = _echo(message_type)
    row = "SELECT status, updated_at FROM dbos.workflow_status WHERE workflow_uuid = %(id)s"
    before = sql(settings, row, {"id": target})
    body = fields.get("body")
    request = (
        {"body": {**body, "workflow_id": target}}
        if isinstance(body, dict)
        else {**fields, "workflow_id": target}
    )

    response = fake_conductor.request(message_type, **request)

    assert response["type"] == message_type
    assert response["error_message"] == (
        f"curate.d refuses Conductor command type {message_type!r}: {READ_ONLY}"
    )
    assert command_entry_types(settings, str(response["request_id"])) == []
    assert before[0][0] == "SUCCESS"
    assert sql(settings, row, {"id": target}) == before
    assert DBOS.list_workflows(forked_from=target, load_input=False, load_output=False) == []


def test_a_backfill_is_refused_and_fires_nothing(
    launch: Launch, fake_conductor: FakeConductor, settings: Settings
) -> None:
    """A backfill over a window holding a firing enqueues none; backfills belong to #6."""
    launch()
    name = f"conductor-gate-schedule-{uuid4().hex[:8]}"
    # Fires on 29 February at midnight: never on its own, and once in the window below.
    DBOS.apply_schedules(
        [
            ScheduleInput(
                schedule_name=name, workflow_fn=conductor_gate_scheduled, schedule="0 0 0 29 2 *"
            )
        ]
    )
    try:
        response = fake_conductor.request(
            "backfill_schedule",
            schedule_name=name,
            start="2028-02-28T00:00:00+00:00",
            end="2028-03-01T00:00:00+00:00",
        )

        assert response["type"] == "backfill_schedule"
        assert response["error_message"] == (
            f"curate.d refuses Conductor command type 'backfill_schedule': {READ_ONLY}; "
            "backfilling a schedule belongs to the deployment-time schedule tooling "
            "(issue #6), not to Conductor"
        )
        assert command_entry_types(settings, str(response["request_id"])) == []
        assert DBOS.list_workflows(schedule_name=name, load_input=False, load_output=False) == []
    finally:
        DBOS.delete_schedule(name)


def test_recovery_is_refused_and_reenqueues_nothing(
    launch: Launch, fake_conductor: FakeConductor, settings: Settings
) -> None:
    """RECOVERY is refused, so a named executor's PENDING row stays where it is."""
    launch()
    executor = f"conductor-gate-gone-{uuid4().hex[:8]}"
    # On the worker's own version, which DBOS's handler would re-enqueue if it were reached.
    stranded = f"conductor-gate-stranded-{uuid4()}"
    sql(
        settings,
        "INSERT INTO dbos.workflow_status (workflow_uuid, status, name, executor_id,"
        " application_name, application_version, queue_name, serialization)"
        " VALUES (%(id)s, 'PENDING', 'tests.conductor.stranded', %(executor)s, %(app)s,"
        " %(version)s, 'conductor-gate-unconsumed', 'py_pickle')",
        {
            "id": stranded,
            "executor": executor,
            "app": GlobalParams.app_name,
            "version": dbos_app.APPLICATION_VERSION,
        },
    )
    try:
        response = fake_conductor.request("recovery", executor_ids=[executor])

        assert response["type"] == "recovery"
        assert response["error_message"] == (
            f"curate.d refuses Conductor command type 'recovery': {READ_ONLY}; curate.d "
            "recovers its executors itself: each restarted worker recovers its own rows at "
            "startup under its stable identity"
        )
        rows = sql(
            settings,
            "SELECT status, executor_id FROM dbos.workflow_status WHERE workflow_uuid = %(id)s",
            {"id": stranded},
        )
        assert rows == [("PENDING", executor)]
    finally:
        sql(
            settings,
            "DELETE FROM dbos.workflow_status WHERE workflow_uuid = %(id)s",
            {"id": stranded},
        )


def test_an_import_is_refused_without_decoding_its_payload(
    launch: Launch, fake_conductor: FakeConductor, settings: Settings, tmp_path: Path
) -> None:
    """IMPORT_WORKFLOW with a payload that runs code when unpickled creates no workflow."""
    launch()
    [exported] = _get_dbos_instance()._sys_db.export_workflow(
        _echo("exported"), export_children=False
    )
    imported = f"conductor-gate-imported-{uuid4()}"
    canary = tmp_path / "unpickled"
    exported["workflow_status"] |= {"workflow_uuid": imported, "canary": _Canary(canary)}
    payload = base64.b64encode(gzip.compress(pickle.dumps([exported]))).decode()

    response = fake_conductor.request("import_workflow", serialized_workflow=payload)

    assert response["type"] == "import_workflow"
    assert "'import_workflow'" in str(response["error_message"])
    assert not canary.exists()
    assert workflow_status(settings, imported) is None
    # The payload is as harmful as claimed: loading it runs the canary.
    pickle.loads(gzip.decompress(base64.b64decode(payload)))
    assert canary.exists()


def test_a_rejected_key_leaves_the_worker_executing(
    launch: Launch, fake_conductor: FakeConductor, settings: Settings, logs: list[EventDict]
) -> None:
    """A 401 handshake is logged as `conductor.rejected` while workflows still complete."""
    fake_conductor.reject = True

    launch()

    rejected = wait_for_event(logs, "conductor.rejected")
    assert rejected["status"] == 401
    assert workflow_status(settings, _echo("while rejected")) == "SUCCESS"
    assert fake_conductor.handshakes
    assert fake_conductor.connections == 0


def test_a_proxied_connection_reaches_conductor(
    launch: Launch, fake_conductor: FakeConductor, logs: list[EventDict]
) -> None:
    """With the proxy setting, the connection is a CONNECT through the proxy."""
    proxy = ConnectProxy()
    try:
        executor_id = launch(proxy_url=proxy.url)

        assert fake_conductor.executor_info()["executor_id"] == executor_id
        assert proxy.targets == [f"127.0.0.1:{fake_conductor.port}"]
        assert wait_for_event(logs, "conductor.connected")["proxied"] is True
    finally:
        stop_worker()
        proxy.stop()


def test_a_stopped_proxy_leaves_the_worker_executing(
    launch: Launch, fake_conductor: FakeConductor, settings: Settings, logs: list[EventDict]
) -> None:
    """With the proxy down, `conductor.connect_failed` is logged and workflows complete."""
    proxy = ConnectProxy()
    proxy.stop()

    launch(proxy_url=proxy.url)

    failed = wait_for_event(logs, "conductor.connect_failed")
    assert KEY not in str(failed["reason"])
    assert workflow_status(settings, _echo("while the proxy is down")) == "SUCCESS"
    assert fake_conductor.handshakes == []
