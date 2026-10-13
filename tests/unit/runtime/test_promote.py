"""Promotion of an already-registered version, on an in-memory DBOS client."""

from datetime import UTC, datetime
from unittest.mock import Mock

import psycopg
import pytest
from dbos import error
from sqlalchemy import exc as sa_exc
from structlog.testing import capture_logs

from curate_d.config import Settings
from curate_d.runtime import dbos_app, promote, version_lease
from curate_d.runtime.dbos_app import APPLICATION_NAME
from curate_d.runtime.lease import promotion_fence_key
from tests.unit.runtime.fake_lease import FakeLockServer

FENCE = promotion_fence_key(APPLICATION_NAME)


class _Client:
    """The `DBOSClient` version calls, over an in-memory version table."""

    def __init__(self, versions: list[str], *, latest: str, read_back: str | None = None) -> None:
        self.versions = versions
        self.latest = latest
        self.read_back = read_back
        self.promoted: list[str] = []
        self.refuse: error.DBOSException | None = None
        self.destroyed = False

    def list_application_versions(self) -> list[dict[str, str]]:
        return [{"version_name": version} for version in self.versions]

    def get_latest_application_version(self) -> dict[str, str]:
        return {"version_name": self.latest}

    def set_latest_application_version(self, version: str) -> None:
        if self.refuse is not None:
            raise self.refuse
        self.promoted.append(version)
        self.latest = self.read_back or version

    def destroy(self) -> None:
        self.destroyed = True


@pytest.fixture
def fence_locks(monkeypatch: pytest.MonkeyPatch) -> FakeLockServer:
    """Serve the promotion fence's sessions from an in-memory lock server."""
    server = FakeLockServer()

    def hold(settings: Settings, *, timeout: float) -> object:
        """Hold the real fence over `server`."""
        return version_lease.hold_fence_exclusive(settings, timeout=timeout, connect=server.connect)

    monkeypatch.setattr(promote, "hold_fence_exclusive", hold)
    return server


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, fence_locks: FakeLockServer) -> _Client:
    """Serve promote's DBOS calls from memory, with the database clock in step."""
    fake = _Client(["v1", "v2"], latest="v2")
    monkeypatch.setattr(dbos_app, "client", lambda settings: fake)
    monkeypatch.setattr(promote, "get_settings", Settings)
    monkeypatch.setattr(promote, "configure_logging", lambda **kwargs: None)
    monkeypatch.setattr(promote, "_database_now", lambda settings: datetime.now(UTC))
    return fake


def test_a_rollback_promotes_the_older_version(client: _Client) -> None:
    """The restored build becomes latest, and the version it replaced is logged."""
    with capture_logs() as events:
        assert promote.main(["v1"]) == 0
    assert client.promoted == ["v1"]
    assert client.latest == "v1"
    assert client.destroyed
    [promoted] = [event for event in events if event["event"] == "promote.promoted"]
    assert promoted["previous_latest"] == "v2"


def test_an_unknown_version_is_refused_without_writing(client: _Client) -> None:
    """A typo or another environment's version never reaches DBOS."""
    with capture_logs() as events:
        assert promote.main(["v9"]) == promote.EXIT_REFUSED
    assert client.promoted == []
    assert [event["event"] for event in events] == ["promote.refused"]
    assert "not registered" in events[0]["reason"]


def test_clock_skew_is_refused_without_writing(
    client: _Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DBOS stamps the promotion with this clock; a slow one may not promote at all."""
    monkeypatch.setattr(promote, "_database_now", lambda settings: datetime(2020, 1, 1, tzinfo=UTC))
    with capture_logs() as events:
        assert promote.main(["v1"]) == promote.EXIT_REFUSED
    assert client.promoted == []
    assert "clock" in events[0]["reason"]


def test_a_mismatched_read_back_fails(client: _Client) -> None:
    """A promotion that does not read back is not reported as done."""
    client.read_back = "v2"
    with capture_logs() as events:
        assert promote.main(["v1"]) == promote.EXIT_REFUSED
    assert [event["event"] for event in events] == ["promote.failed"]
    assert "reads back as 'v2'" in events[0]["error"]


def test_another_applications_version_fails(client: _Client) -> None:
    """DBOS refuses to retime a peer application's version; the command exits non-zero."""
    client.refuse = error.DBOSException("Application version 'v1' is already registered by 'other'")
    with capture_logs() as events:
        assert promote.main(["v1"]) == promote.EXIT_REFUSED
    assert [event["event"] for event in events] == ["promote.failed"]
    assert client.destroyed


def test_promoting_the_current_latest_is_harmless(client: _Client) -> None:
    """It stays latest and the command succeeds."""
    assert promote.main(["v2"]) == 0
    assert client.latest == "v2"


def test_the_clock_check_tolerates_a_second(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skew up to the limit passes; the midpoint of the round trip is compared."""
    now = datetime.now(UTC)
    monkeypatch.setattr(promote, "_database_now", lambda settings: now)
    assert promote._check_clock(Settings()) <= promote.MAX_CLOCK_SKEW_SECONDS


def test_a_version_is_required() -> None:
    """argparse refuses a missing version before anything connects."""
    with pytest.raises(SystemExit) as raised:
        promote.main([])
    assert raised.value.code == 2


@pytest.mark.parametrize("offset", [-1.0, 0.0, 1.0])
def test_clock_skew_accepts_the_exact_boundary_using_the_round_trip_midpoint(
    monkeypatch: pytest.MonkeyPatch, offset: float
) -> None:
    """Both clock directions accept one second, measured at the request midpoint."""
    readings = iter([1000.0, 1004.0])
    monkeypatch.setattr(
        promote, "_database_now", lambda settings: datetime.fromtimestamp(1002.0 + offset, UTC)
    )
    assert promote._check_clock(Settings(), clock=lambda: next(readings)) == abs(offset)


@pytest.mark.parametrize("offset", [-1.001, 1.001])
def test_clock_skew_just_outside_the_boundary_is_refused(
    monkeypatch: pytest.MonkeyPatch, offset: float
) -> None:
    """Skew beyond one second fails for both a fast and a slow database clock."""
    monkeypatch.setattr(
        promote, "_database_now", lambda settings: datetime.fromtimestamp(1000.0 + offset, UTC)
    )
    with pytest.raises(promote.PromotionRefusedError, match="more than 1s"):
        promote._check_clock(Settings(), clock=lambda: 1000.0)


@pytest.mark.parametrize(
    "operation", ["list_application_versions", "get_latest_application_version"]
)
def test_a_version_read_failure_closes_the_client_without_promoting(
    client: _Client, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    """A database outage before the promotion cannot be reported as success."""

    def fail() -> None:
        """Simulate a failed version lookup, as DBOSClient raises it."""
        raise sa_exc.OperationalError("SELECT", {}, Exception("version lookup failed"))

    monkeypatch.setattr(client, operation, fail)
    assert promote.main(["v1"]) == promote.EXIT_REFUSED
    assert client.promoted == []
    assert client.destroyed


@pytest.mark.parametrize("row", [None, (None,), ("not a timestamp",)])
def test_an_invalid_database_clock_closes_its_connection(
    monkeypatch: pytest.MonkeyPatch, row: tuple[object, ...] | None
) -> None:
    """Absent or malformed clock data fails closed and releases its dedicated session."""
    connection = Mock()
    connection.execute.return_value.fetchone.return_value = row
    monkeypatch.setattr(promote, "connect_dedicated", lambda *args, **kwargs: connection)
    with pytest.raises(promote.PromotionRefusedError, match="database returned no time"):
        promote._database_now(Settings())
    connection.close.assert_called_once_with()


def test_a_failed_database_clock_read_closes_its_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clock query failure releases its connection and preserves the database error."""
    failure = psycopg.OperationalError("clock unavailable")
    connection = Mock()
    connection.execute.side_effect = failure
    monkeypatch.setattr(promote, "connect_dedicated", lambda *args, **kwargs: connection)
    with pytest.raises(psycopg.OperationalError) as raised:
        promote._database_now(Settings())
    assert raised.value is failure
    connection.close.assert_called_once_with()


def test_a_post_promotion_read_failure_reports_failure_without_undoing_the_write(
    client: _Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed read-back exits non-zero and leaves the written promotion in place."""
    read = Mock(
        side_effect=[
            {"version_name": "v2"},
            sa_exc.OperationalError("SELECT", {}, Exception("read back failed")),
        ]
    )
    monkeypatch.setattr(client, "get_latest_application_version", read)

    with capture_logs() as events:
        assert promote.main(["v1"]) == promote.EXIT_REFUSED
    assert client.promoted == ["v1"]
    assert client.latest == "v1"
    assert client.destroyed
    assert [event["event"] for event in events] == ["promote.failed"]
    assert "read back failed" in events[0]["error"]


def test_a_clock_read_failure_refuses_promotion_and_closes_the_client(
    client: _Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreadable database clock never permits the operator to write a promotion."""
    monkeypatch.setattr(
        promote, "_database_now", Mock(side_effect=psycopg.OperationalError("clock unavailable"))
    )

    assert promote.main(["v1"]) == promote.EXIT_REFUSED
    assert client.promoted == []
    assert client.latest == "v2"
    assert client.destroyed


def test_the_change_runs_under_the_exclusive_fence(
    client: _Client, fence_locks: FakeLockServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No sweep can fork onto the replaced version while latest moves; the fence is freed after."""
    modes: list[str | None] = []
    set_latest = client.set_latest_application_version

    def observe(version: str) -> None:
        """Record the fence's mode at the moment of the change."""
        modes.append(fence_locks.mode(FENCE))
        set_latest(version)

    monkeypatch.setattr(client, "set_latest_application_version", observe)
    assert promote.main(["v1"]) == 0
    assert modes == ["exclusive"]
    assert fence_locks.waits == [promote.FENCE_TIMEOUT_SECONDS]
    assert fence_locks.mode(FENCE) is None


def test_the_fence_timeout_is_configurable(client: _Client, fence_locks: FakeLockServer) -> None:
    """--fence-timeout bounds the wait for a running sweep."""
    assert promote.main(["--fence-timeout", "5", "v1"]) == 0
    assert fence_locks.waits == [5.0]


def test_a_sweep_holding_the_fence_times_out_without_promoting(
    client: _Client, fence_locks: FakeLockServer
) -> None:
    """A sweep mid-batch keeps latest where it is; the command exits with its own code."""
    sweep = fence_locks.connect()
    assert sweep.try_lock_shared(FENCE)
    with capture_logs() as events:
        assert promote.main(["v1"]) == promote.EXIT_FENCED
    assert client.promoted == []
    assert client.latest == "v2"
    assert client.destroyed
    assert [event["event"] for event in events] == ["promote.fenced"]
    assert "promotion fence" in events[0]["reason"]
    assert fence_locks.mode(FENCE) == "shared"


def test_a_refused_version_never_takes_the_fence(
    client: _Client, fence_locks: FakeLockServer
) -> None:
    """Pre-write refusals come before the fence, so they never wait on a sweep."""
    assert promote.main(["v9"]) == promote.EXIT_REFUSED
    assert fence_locks.sessions == []


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "soon"])
def test_a_fence_timeout_must_be_a_positive_number(value: str) -> None:
    """argparse refuses a non-positive or non-numeric timeout before anything connects."""
    with pytest.raises(SystemExit) as raised:
        promote.main(["--fence-timeout", value, "v1"])
    assert raised.value.code == 2
