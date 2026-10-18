"""The CI gate that the workers applied this build's schedules, on an in-memory DBOS client."""

import pytest

from curate_d.config import Settings
from curate_d.runtime import dbos_app
from curate_d.worker import schedule_check
from curate_d.worker.schedule_check import declared_crons, schedule_failures
from curate_d.workflows.registry import declared_schedule_names

VERSION = dbos_app.APPLICATION_VERSION


class _Client:
    """The `DBOSClient` calls the gate makes, over an in-memory latest version and schedules."""

    def __init__(self, *, latest: str, stored: dict[str, str]) -> None:
        self.latest = latest
        self.stored = stored
        self.destroyed = False

    def get_latest_application_version(self) -> dict[str, str]:
        return {"version_name": self.latest}

    def list_schedules(self) -> list[dict[str, str]]:
        return [{"schedule_name": name, "schedule": cron} for name, cron in self.stored.items()]

    def destroy(self) -> None:
        self.destroyed = True


@pytest.fixture
def declared() -> dict[str, str]:
    """Return the schedules this build declares, by name, with their crons."""
    return declared_crons(Settings())


def _run(
    monkeypatch: pytest.MonkeyPatch, *, latest: str, stored: dict[str, str]
) -> tuple[int, _Client]:
    """Run the gate's entry point against an in-memory client and return its status and client."""
    fake = _Client(latest=latest, stored=stored)
    monkeypatch.setattr(dbos_app, "client", lambda settings, **kwargs: fake)
    monkeypatch.setattr(schedule_check, "get_settings", Settings)
    return schedule_check.main(), fake


def test_the_declared_crons_cover_every_declared_schedule(declared: dict[str, str]) -> None:
    """The gate compares against the same names the reconciler keeps."""
    assert declared.keys() == declared_schedule_names(Settings())
    assert declared


def test_applied_schedules_pass(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    declared: dict[str, str],
) -> None:
    """The build's version as latest and its exact schedules exit 0 with no annotation."""
    status, client = _run(monkeypatch, latest=VERSION, stored=dict(declared))
    assert status == 0
    assert client.destroyed
    assert capsys.readouterr().out == ""


def test_a_stale_latest_version_fails(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    declared: dict[str, str],
) -> None:
    """Workers not on latest applied nothing, however right the stored rows look."""
    status, client = _run(monkeypatch, latest="stale", stored=dict(declared))
    assert status == 1
    assert client.destroyed
    assert capsys.readouterr().out == (
        f"::error::schedules not applied: latest version is 'stale', not the image's {VERSION!r}\n"
    )


def test_a_missing_schedule_fails(declared: dict[str, str]) -> None:
    """A declared schedule with no stored row is named as missing."""
    name = sorted(declared)[0]
    stored = {key: cron for key, cron in declared.items() if key != name}
    assert schedule_failures(latest=VERSION, version=VERSION, stored=stored, declared=declared) == [
        f"missing {[name]}, undeclared []"
    ]


def test_an_undeclared_schedule_fails(declared: dict[str, str]) -> None:
    """A stored row no profile declares is named as undeclared."""
    stored = {**declared, "retired-pass": "0 0 * * *"}
    assert schedule_failures(latest=VERSION, version=VERSION, stored=stored, declared=declared) == [
        "missing [], undeclared ['retired-pass']"
    ]


def test_a_mismatched_cron_fails(declared: dict[str, str]) -> None:
    """A stored row whose cron differs from its declaration is named as stale."""
    name = sorted(declared)[0]
    stored = {**declared, name: declared[name] + " "}
    assert schedule_failures(latest=VERSION, version=VERSION, stored=stored, declared=declared) == [
        f"stored cron differs from the declared one for {[name]}"
    ]


def test_every_failure_is_reported(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    declared: dict[str, str],
) -> None:
    """One run annotates each failure, so a CI log shows them all at once."""
    name = sorted(declared)[0]
    stored = {**declared, name: declared[name] + " ", "retired-pass": "0 0 * * *"}
    status, _ = _run(monkeypatch, latest="stale", stored=stored)
    assert status == 1
    assert capsys.readouterr().out.splitlines() == [
        f"::error::schedules not applied: latest version is 'stale', not the image's {VERSION!r}",
        "::error::schedules not applied: missing [], undeclared ['retired-pass']",
        f"::error::schedules not applied: stored cron differs from the declared one for {[name]}",
    ]


def test_the_client_is_destroyed_when_a_read_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed read propagates, failing the step, and still releases the client."""
    fake = _Client(latest=VERSION, stored={})

    def fail() -> list[dict[str, str]]:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(fake, "list_schedules", fail)
    monkeypatch.setattr(dbos_app, "client", lambda settings, **kwargs: fake)
    monkeypatch.setattr(schedule_check, "get_settings", Settings)
    with pytest.raises(RuntimeError, match="database unavailable"):
        schedule_check.main()
    assert fake.destroyed
