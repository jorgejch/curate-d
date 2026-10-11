"""The persisted schedule that prunes workflow history."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from curate_d.config import Settings
from curate_d.runtime.profiles import WorkerProfile
from curate_d.runtime.retention import RetentionWindow
from curate_d.workflows import retention
from curate_d.workflows.registry import schedules_for

_TICK = datetime(2026, 10, 3, 5, 20, tzinfo=UTC)


def test_the_prune_runs_on_its_configured_schedule() -> None:
    """On the I/O profile's maintenance queue, with the configured cron."""
    settings = Settings(workflow_retention_schedule="0 1 * * *")
    entry = retention.schedule(settings)
    assert (entry["schedule"], entry["queue_name"]) == ("0 1 * * *", "maintenance")
    assert entry["workflow_fn"] is retention.prune_history
    assert entry in schedules_for(WorkerProfile.IO, settings)


def test_the_default_prune_runs_once_a_day() -> None:
    """One fixed minute and hour, every day: each run removes one day's newly aged records."""
    minute, hour, *rest = Settings().workflow_retention_schedule.split()
    assert minute.isdigit() and hour.isdigit()
    assert rest == ["*", "*", "*"]


def test_the_cutoff_is_measured_from_the_scheduled_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recovered or backfilled run prunes the boundary its tick would have."""
    settings = Settings(workflow_retention_days=7)
    seen: list[tuple[Settings, datetime | None]] = []

    async def prune(given: Settings, *, now: datetime | None = None) -> RetentionWindow:
        seen.append((given, now))
        assert now is not None
        return RetentionWindow(given.workflow_retention_days, now - timedelta(days=7))

    monkeypatch.setattr(retention, "prune_workflow_history", prune)
    monkeypatch.setattr(retention, "current", lambda: SimpleNamespace(settings=settings))

    cutoff = asyncio.run(retention._prune_history_step(_TICK))
    assert seen == [(settings, _TICK)]
    assert cutoff == (_TICK - timedelta(days=7)).isoformat()
