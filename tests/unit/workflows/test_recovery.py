"""The persisted schedule that runs the recovery sweep."""

import asyncio
from types import SimpleNamespace

import pytest
from dbos import DBOS

from curate_d.config import Settings
from curate_d.runtime.profiles import WorkerProfile
from curate_d.runtime.sweep import RECOVERY_SCHEDULE_NAME, DBOSRecoveryControl, SweepOutcome
from curate_d.workflows import recovery
from curate_d.workflows.registry import declared_schedule_names, schedules_for


def test_the_sweep_runs_on_its_configured_schedule() -> None:
    """On the maintenance queue, with the configured cron, under the sweep's own name."""
    settings = Settings(recovery_sweep_schedule="*/5 * * * *")
    entry = recovery.schedule(settings)
    assert entry["schedule_name"] == RECOVERY_SCHEDULE_NAME == "recovery-sweep"
    assert (entry["schedule"], entry["queue_name"]) == ("*/5 * * * *", "maintenance")
    assert entry["workflow_fn"] == recovery.recover_stranded_workflows


def test_only_the_io_profile_declares_the_sweep() -> None:
    """Applying it from both profiles would race to write identical rows."""
    settings = Settings()
    assert recovery.schedule(settings) in schedules_for(WorkerProfile.IO, settings)
    assert all(
        entry["schedule_name"] != RECOVERY_SCHEDULE_NAME
        for entry in schedules_for(WorkerProfile.CPU, settings)
    )
    assert RECOVERY_SCHEDULE_NAME in declared_schedule_names(settings)


def test_the_default_sweep_runs_every_ten_minutes() -> None:
    """Stranded work waits at most one interval."""
    assert Settings().recovery_sweep_schedule == "*/10 * * * *"


def test_the_step_sweeps_as_this_build_and_returns_plain_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The checkpoint holds a dict of plain values, not a class the follow-up may move."""
    settings = Settings()
    seen: list[tuple[Settings, object, str]] = []

    async def sweep(given: Settings, *, control: object, own_version: str) -> SweepOutcome:
        seen.append((given, control, own_version))
        return SweepOutcome(forked=(("a", "b"),), left_running=1, skipped=("c",))

    monkeypatch.setattr(recovery, "run_recovery_sweep", sweep)
    monkeypatch.setattr(recovery, "current", lambda: SimpleNamespace(settings=settings))
    monkeypatch.setattr(DBOS, "application_version", "v2")

    summary = asyncio.run(recovery._recovery_sweep_step())

    [(given, control, own_version)] = seen
    assert given is settings
    assert isinstance(control, DBOSRecoveryControl)
    assert own_version == "v2"
    assert summary["forked"] == (("a", "b"),)
    assert summary["left_running"] == 1
    assert summary["skipped"] == ("c",)
    assert set(summary) == {
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
