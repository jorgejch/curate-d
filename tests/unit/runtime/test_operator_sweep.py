"""The operator sweep's checks and exit codes, with DBOS, the registry and the lease in memory."""

import json
from collections.abc import Mapping
from datetime import datetime
from unittest.mock import AsyncMock, Mock

import pytest
from dbos import DBOS
from structlog.testing import capture_logs

from curate_d.config import Settings
from curate_d.runtime import dbos_app, operator_sweep, version_lease
from curate_d.runtime.lease import ExecutorLeaseUnavailableError
from curate_d.runtime.sweep import ClientRecoveryControl, SweepOutcome
from tests.unit.runtime.fake_recovery import registered

OWN = "v2"


class _Client:
    """The `DBOSClient` calls the operator sweep makes before handing over to the sweep."""

    def __init__(self, latest: str) -> None:
        self.latest = latest
        self.destroyed = False

    async def get_latest_application_version_async(self) -> dict[str, str]:
        return {"version_name": self.latest}

    def destroy(self) -> None:
        self.destroyed = True


class _Lease:
    """A standalone version lease that is taken, refused or lost on demand."""

    def __init__(self, *, refuse: bool = False, lose: bool = False) -> None:
        self.refuse = refuse
        self.lose = lose
        self.held = False
        self.released = False

    def acquire(self) -> None:
        if self.refuse:
            raise ExecutorLeaseUnavailableError("version 'v2' is held exclusively")
        self.held = True

    def monitor(self, on_lost: object) -> None:
        if self.lose:
            self.held = False

    def ensure_held(self) -> None:
        if not self.held:
            raise RuntimeError("lost")

    def release(self) -> None:
        self.released = True
        self.held = False


class _Harness:
    """The collaborators `main` reaches, recorded."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.client = _Client(latest=OWN)
        self.lease = _Lease()
        self.registry: Mapping[str, datetime] = registered("v1", OWN)
        self.outcome = SweepOutcome()
        self.sweeps: list[tuple[object, str]] = []
        monkeypatch.setattr(dbos_app, "APPLICATION_VERSION", OWN)
        monkeypatch.setattr(dbos_app, "client", lambda settings: self.client)
        monkeypatch.setattr(operator_sweep, "get_settings", Settings)
        monkeypatch.setattr(operator_sweep, "configure_logging", lambda **kwargs: None)
        monkeypatch.setattr(version_lease, "registrations", self._registrations)
        monkeypatch.setattr(
            version_lease, "standalone_version_lease", lambda settings, version: self.lease
        )
        monkeypatch.setattr(operator_sweep, "run_recovery_sweep", self._sweep)

    async def _registrations(self, settings: Settings) -> Mapping[str, datetime]:
        return self.registry

    async def _sweep(
        self, settings: Settings, *, control: object, own_version: str
    ) -> SweepOutcome:
        self.sweeps.append((control, own_version))
        return self.outcome


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> _Harness:
    """Run `main` against in-memory collaborators; DBOS itself must never start."""

    def never(*args: object, **kwargs: object) -> None:
        raise AssertionError("the operator sweep constructed or launched DBOS")

    monkeypatch.setattr(DBOS, "launch", never)
    monkeypatch.setattr(DBOS, "__init__", never)
    return _Harness(monkeypatch)


@pytest.mark.parametrize("target", ["v1", "v3"])
def test_a_target_other_than_latest_and_own_is_refused(harness: _Harness, target: str) -> None:
    """A foreign or stale target writes nothing and exits 2."""
    assert operator_sweep.main(["--target-version", target]) == operator_sweep.EXIT_REFUSED
    assert harness.sweeps == []
    assert not harness.lease.held and not harness.lease.released
    assert harness.client.destroyed


def test_a_build_not_on_the_latest_version_is_refused(harness: _Harness) -> None:
    """The target must also be what this binary runs."""
    harness.client.latest = "v3"
    assert operator_sweep.main(["--target-version", "v3"]) == operator_sweep.EXIT_REFUSED
    assert harness.sweeps == []


def test_a_missing_target_is_a_usage_error(harness: _Harness) -> None:
    """argparse exits 2 before anything connects."""
    with pytest.raises(SystemExit) as raised:
        operator_sweep.main([])
    assert raised.value.code == 2
    assert harness.sweeps == []


def test_an_unregistered_target_is_refused(harness: _Harness) -> None:
    """A target that never held the version lease cannot anchor recovery."""
    harness.registry = registered("v1")
    assert operator_sweep.main(["--target-version", OWN]) == operator_sweep.EXIT_REFUSED
    assert harness.sweeps == []


def test_an_unavailable_version_lease_is_refused(harness: _Harness) -> None:
    """While another sweep holds the target exclusively, nothing runs."""
    harness.lease.refuse = True
    assert operator_sweep.main(["--target-version", OWN]) == operator_sweep.EXIT_REFUSED
    assert harness.sweeps == []


def test_a_matching_run_sweeps_through_the_client_under_the_lease(
    harness: _Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    """The sweep runs as this build, through the client, and every field is printed."""
    harness.outcome = SweepOutcome(forked=(("a", "b"),), left_running=2)
    assert operator_sweep.main(["--target-version", OWN]) == 0
    [(control, own_version)] = harness.sweeps
    assert isinstance(control, ClientRecoveryControl)
    assert own_version == OWN
    assert harness.lease.released
    printed = json.loads(capsys.readouterr().out)
    assert set(printed) == {
        "reenqueued",
        "forked",
        "retired",
        "left_running",
        "failed",
        "skipped",
        "unregistered",
        "newer_than_target",
        "version_live",
        "retired_ticks",
    }
    assert printed["forked"] == [["a", "b"]]


def test_failed_rows_exit_one(harness: _Harness) -> None:
    """An operator sees at a glance that something needs triage."""
    harness.outcome = SweepOutcome(failed=(("a", "fork failed"),))
    assert operator_sweep.main(["--target-version", OWN]) == operator_sweep.EXIT_FAILED_ROWS


def test_a_lost_lease_exits_non_zero(harness: _Harness) -> None:
    """The run reports the loss rather than success."""
    harness.lease.lose = True
    assert operator_sweep.main(["--target-version", OWN]) == operator_sweep.EXIT_LEASE_LOST
    assert harness.lease.released


@pytest.mark.parametrize("operation", ["monitor", "sweep"])
def test_a_failure_after_acquisition_releases_the_lease_and_client(
    harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    operation: str,
) -> None:
    """A failed run cannot leak the target lease or print a successful summary."""
    error = RuntimeError("sweep infrastructure failed")
    if operation == "monitor":
        monkeypatch.setattr(harness.lease, "monitor", Mock(side_effect=error))
    else:
        monkeypatch.setattr(operator_sweep, "run_recovery_sweep", AsyncMock(side_effect=error))
    with pytest.raises(RuntimeError) as raised:
        operator_sweep.main(["--target-version", OWN])
    assert raised.value is error
    assert harness.lease.released
    assert not harness.lease.held
    assert harness.client.destroyed
    assert capsys.readouterr().out == ""


def test_a_registry_read_failure_closes_the_client_before_leasing(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unknown registration state aborts before taking a lease or sweeping."""
    error = version_lease.VersionRegistryError("registry unavailable")
    monkeypatch.setattr(version_lease, "registrations", AsyncMock(side_effect=error))
    acquire = Mock(wraps=harness.lease.acquire)
    monkeypatch.setattr(harness.lease, "acquire", acquire)
    with pytest.raises(version_lease.VersionRegistryError) as raised:
        operator_sweep.main(["--target-version", OWN])
    assert raised.value is error
    acquire.assert_not_called()
    assert harness.sweeps == []
    assert harness.client.destroyed


def test_lease_loss_takes_precedence_over_failed_rows(
    harness: _Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    """The operator sees the lost-lease exit code while retaining per-row failures."""
    harness.lease.lose = True
    harness.outcome = SweepOutcome(failed=(("orphan", "write refused"),))
    with capture_logs() as events:
        assert operator_sweep.main(["--target-version", OWN]) == operator_sweep.EXIT_LEASE_LOST
    assert json.loads(capsys.readouterr().out)["failed"] == [["orphan", "write refused"]]
    assert [event["event"] for event in events] == ["operator_sweep.stopped_on_lease_loss"]
    assert harness.lease.released
    assert harness.client.destroyed
