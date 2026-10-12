"""Promotion of an already-registered version, on an in-memory DBOS client."""

from datetime import UTC, datetime

import pytest
from dbos._error import DBOSException
from structlog.testing import capture_logs

from curate_d.config import Settings
from curate_d.runtime import dbos_app, promote


class _Client:
    """The `DBOSClient` version calls, over an in-memory version table."""

    def __init__(self, versions: list[str], *, latest: str, read_back: str | None = None) -> None:
        self.versions = versions
        self.latest = latest
        self.read_back = read_back
        self.promoted: list[str] = []
        self.refuse: DBOSException | None = None
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
def client(monkeypatch: pytest.MonkeyPatch) -> _Client:
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
    client.refuse = DBOSException("Application version 'v1' is already registered by 'other'")
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
