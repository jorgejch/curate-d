"""What one deployment-time reconciliation writes, against an in-memory client.

The fence and the transactions run against PostgreSQL in
tests/integration/test_schedule_deploy.py.
"""

from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from dbos import ClientScheduleInput
from structlog.testing import CapturingLogger

from curate_d.config import Settings
from curate_d.runtime.schedule_fence import ScheduleFence, StaleScheduleDeploymentError
from curate_d.schedules import deployment
from curate_d.schedules.deployment import (
    Patience,
    QueuesNotDeclaredError,
    ScheduleReconciliation,
    StoredSchedule,
    VersionNotRegisteredError,
    VersionSupersededError,
    plan,
    promote,
    reconcile,
)
from curate_d.workflows.registry import declared_schedules
from tests.unit.schedules.fakes import APPLICATION, ClientFailureError, FakeScheduleClient

_VERSION = "1.0.0"


def _entry(name: str, cron: str = "0 0 * * *", queue: str = "maintenance") -> ClientScheduleInput:
    """Return a declaration of `name` on `queue`."""
    return {
        "schedule_name": name,
        "workflow_name": "wf",
        "schedule": cron,
        "context": None,
        "queue_name": queue,
    }


class _Clock:
    """A monotonic clock that advances only when the code under test sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps = 0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        """Advance the clock by `seconds`."""
        self.now += seconds
        self.sleeps += 1


@pytest.fixture
def clock() -> _Clock:
    """Return a clock the waits advance by sleeping."""
    return _Clock()


@pytest.fixture
def patience(clock: _Clock) -> Patience:
    """Return a ten-second wait on the fake clock."""
    return Patience(seconds=10, poll_seconds=1, clock=clock, sleep=clock.sleep)


@pytest.fixture
def fence() -> ScheduleFence:
    """Return a fence for this build's version, installed on no engine."""
    return ScheduleFence(application=APPLICATION, version=_VERSION)


@pytest.fixture
def client(fence: ScheduleFence) -> FakeScheduleClient:
    """Return a client whose only version is this build's and whose queue exists."""
    return FakeScheduleClient(fence)


@contextmanager
def _logs(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict[str, object]]]:
    """Record what the deployment module logs in the block, each with its event name."""
    sink = CapturingLogger()
    monkeypatch.setattr(deployment, "logger", sink)
    entries: list[dict[str, object]] = []
    yield entries
    entries.extend({"event": call.args[0], **call.kwargs} for call in sink.calls)


def _run(
    client: FakeScheduleClient,
    fence: ScheduleFence,
    declared: list[ClientScheduleInput],
    patience: Patience,
) -> ScheduleReconciliation:
    """Reconcile `declared` as this build."""
    return reconcile(
        client,
        fence,
        application=APPLICATION,
        version=_VERSION,
        declared=declared,
        patience=patience,
    )


def test_applies_once_then_deletes_only_this_applications_undeclared_rows(
    client: FakeScheduleClient,
    fence: ScheduleFence,
    patience: Patience,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Applied in one write; retired rows go one write each; other owners are untouched."""
    client.store("kept", cron="5 5 * * *")
    client.store("retired", workflow="old_pass")
    client.store("another-apps", owner="someone-else")
    client.store("unclaimed", owner=None)
    with _logs(monkeypatch) as logs:
        outcome = _run(client, fence, [_entry("kept"), _entry("added")], patience)

    assert client.writes == [("apply", ["kept", "added"]), ("delete", "retired")]
    assert outcome == ScheduleReconciliation(
        applied=("kept", "added"),
        deleted=(StoredSchedule("retired", "old_pass"),),
        unclaimed_left=(StoredSchedule("unclaimed", "wf"),),
    )
    assert set(client.rows) == {"kept", "added", "another-apps", "unclaimed"}
    assert client.rows["kept"]["schedule"] == "0 0 * * *"
    assert [log["event"] for log in logs] == [
        "schedules.applied",
        "schedules.deleted",
        "schedules.unclaimed_left",
    ]
    assert logs[1]["schedule"] == "retired" and logs[1]["workflow"] == "old_pass"


def test_an_unclaimed_row_a_declaration_names_is_claimed_not_deleted(
    client: FakeScheduleClient, fence: ScheduleFence, patience: Patience
) -> None:
    """The apply takes ownership of it, keeping its history."""
    stored = client.store("kept", owner=None, last_fired_at="2026-10-01T00:00:00Z")
    _run(client, fence, [_entry("kept")], patience)
    assert client.rows["kept"]["application_name"] == APPLICATION
    assert client.rows["kept"]["schedule_id"] == stored["schedule_id"]
    assert client.rows["kept"]["last_fired_at"] == "2026-10-01T00:00:00Z"


def test_a_repeated_run_changes_nothing_further(
    client: FakeScheduleClient, fence: ScheduleFence, patience: Patience
) -> None:
    """The deploy job may run the command twice; the second finds nothing to retire."""
    client.store("retired")
    paused = client.store("kept", status="PAUSED", last_fired_at="2026-10-01T00:00:00Z")
    declared = [_entry("kept"), _entry("added")]
    _run(client, fence, declared, patience)
    rows = {name: dict(row) for name, row in client.rows.items()}
    client.writes.clear()

    outcome = _run(client, fence, declared, patience)

    assert outcome.deleted == ()
    assert client.writes == [("apply", ["kept", "added"])]
    assert {name: dict(row) for name, row in client.rows.items()} == rows
    assert client.rows["kept"]["schedule_id"] == paused["schedule_id"]
    assert client.rows["kept"]["status"] == "PAUSED"
    assert not plan(client, application=APPLICATION, declared=declared).changes


def test_a_failure_part_way_through_the_deletions_converges_on_retry(
    client: FakeScheduleClient, fence: ScheduleFence, patience: Patience
) -> None:
    """Apply and each delete commit alone; a re-run finishes what the first left."""
    for name in ("retired-a", "retired-b", "retired-c"):
        client.store(name)
    client.fail_deletes = {"retired-b"}
    with pytest.raises(ClientFailureError):
        _run(client, fence, [_entry("kept")], patience)
    assert set(client.rows) == {"kept", "retired-b", "retired-c"}

    outcome = _run(client, fence, [_entry("kept")], patience)

    assert [stored.name for stored in outcome.deleted] == ["retired-b", "retired-c"]
    assert set(client.rows) == {"kept"}


def test_a_superseded_version_writes_nothing(
    client: FakeScheduleClient, fence: ScheduleFence, patience: Patience
) -> None:
    """A newer deployment registered first: this one's declarations do not count."""
    client.versions = ["1.1.0", _VERSION]
    client.store("retired")
    with pytest.raises(VersionSupersededError, match="promote"):
        _run(client, fence, [_entry("kept")], patience)
    assert client.writes == []
    assert set(client.rows) == {"retired"}


def test_a_version_superseded_part_way_stops_before_the_next_write(
    client: FakeScheduleClient, fence: ScheduleFence, patience: Patience
) -> None:
    """The fence fails the first write after a newer version registers."""
    client.store("retired")
    client.before_write = [_VERSION, "1.1.0"]  # the second write sees the newer version
    with pytest.raises(StaleScheduleDeploymentError):
        _run(client, fence, [_entry("kept")], patience)
    assert client.writes == [("apply", ["kept"])]
    assert "retired" in client.rows


def test_waits_for_the_version_to_register(
    client: FakeScheduleClient, fence: ScheduleFence, patience: Patience, clock: _Clock
) -> None:
    """The command may start before the rollout's first worker has launched."""
    client.versions = ["0.9.0"]
    client.on_list_versions = [("0.9.0",), ("0.9.0",), (_VERSION, "0.9.0")]
    outcome = _run(client, fence, [_entry("kept")], patience)
    assert outcome.applied == ("kept",)
    assert clock.sleeps == 2


def test_a_version_that_never_registers_times_out_without_writing(
    client: FakeScheduleClient, fence: ScheduleFence, patience: Patience, clock: _Clock
) -> None:
    """Workers that never come up fail the deploy job instead of hanging it."""
    client.versions = ["0.9.0"]
    with pytest.raises(VersionNotRegisteredError, match=r"'0\.9\.0'"):
        _run(client, fence, [_entry("kept")], patience)
    assert client.writes == []
    assert clock.now >= patience.seconds


def test_waits_for_the_queues_its_schedules_name(
    client: FakeScheduleClient, fence: ScheduleFence, patience: Patience
) -> None:
    """Workers register queues just after launch; the client does not check them."""
    client.queues = set()
    client.on_list_queues = [(), ("maintenance",)]
    outcome = _run(client, fence, [_entry("kept")], patience)
    assert outcome.applied == ("kept",)


def test_a_queue_that_never_registers_writes_nothing(
    client: FakeScheduleClient, fence: ScheduleFence, patience: Patience
) -> None:
    """A schedule on a missing queue would fire into nothing."""
    with pytest.raises(QueuesNotDeclaredError, match="no-such-queue"):
        _run(client, fence, [_entry("kept", queue="no-such-queue")], patience)
    assert client.writes == []


def test_promote_makes_a_rolled_back_version_latest_and_reconciles(
    client: FakeScheduleClient, fence: ScheduleFence, patience: Patience
) -> None:
    """A relaunched older version keeps its old timestamp until promoted."""
    client.versions = ["1.1.0", _VERSION]
    client.store("added-by-1.1.0")
    outcome = promote(
        client,
        fence,
        application=APPLICATION,
        version=_VERSION,
        declared=[_entry("kept")],
        patience=patience,
    )
    assert client.versions[0] == _VERSION
    assert client.writes[0] == ("promote", _VERSION)
    assert [stored.name for stored in outcome.deleted] == ["added-by-1.1.0"]


def test_promote_refuses_a_version_never_registered(
    client: FakeScheduleClient, fence: ScheduleFence, patience: Patience
) -> None:
    """Promoting a name DBOS has never seen would change nothing and mislead."""
    client.versions = ["1.1.0"]
    with pytest.raises(VersionNotRegisteredError):
        promote(
            client,
            fence,
            application=APPLICATION,
            version=_VERSION,
            declared=[_entry("kept")],
            patience=patience,
        )
    assert client.writes == []


def test_the_plan_reports_every_kind_of_change_without_writing(
    client: FakeScheduleClient,
) -> None:
    """The dry run lists creations, updates, retirements and unclaimed rows."""
    client.store("same", cron="0 0 * * *")
    client.rows["same"]["queue_name"] = "maintenance"
    client.store("changed", cron="1 1 * * *")
    client.store("retired")
    client.store("unclaimed", owner=None)
    client.store("another-apps", owner="someone-else")
    outcome = plan(
        client,
        application=APPLICATION,
        declared=[_entry("same"), _entry("changed"), _entry("new")],
    )
    assert client.writes == []
    assert (outcome.created, outcome.updated, outcome.unchanged) == (
        ("new",),
        ("changed",),
        ("same",),
    )
    assert outcome.retired == (StoredSchedule("retired", "wf"),)
    assert outcome.unclaimed == (StoredSchedule("unclaimed", "wf"),)
    assert outcome.latest_version == _VERSION
    assert outcome.changes


def test_the_real_declarations_reconcile_against_an_empty_store(
    client: FakeScheduleClient, fence: ScheduleFence, patience: Patience
) -> None:
    """Every declared schedule applies and names a queue the workers register."""
    declared = declared_schedules(Settings())
    outcome = _run(client, fence, declared, patience)
    assert set(outcome.applied) == set(client.rows)
    assert not plan(client, application=APPLICATION, declared=declared).changes
