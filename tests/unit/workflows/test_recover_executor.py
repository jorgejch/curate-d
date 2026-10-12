"""The operator's recovery workflow: one sweep step, its outcome returned as plain data."""

import asyncio
from types import SimpleNamespace

import pytest

from curate_d.config import Settings
from curate_d.runtime.sweep import SweepOutcome
from curate_d.workflows import executors


def test_the_step_runs_a_sweep_scoped_to_the_executor(monkeypatch: pytest.MonkeyPatch) -> None:
    """The outcome crosses the checkpoint as a dict the API reads back without the class."""
    settings = Settings()
    seen: list[tuple[Settings, str | None]] = []

    async def sweep(given: Settings, *, executor_id: str | None = None) -> SweepOutcome:
        seen.append((given, executor_id))
        return SweepOutcome(reenqueued=("a",), skipped=(("b", "version 'v0' is running"),))

    monkeypatch.setattr(executors, "run_recovery_sweep", sweep)
    monkeypatch.setattr(executors, "current", lambda: SimpleNamespace(settings=settings))

    report = asyncio.run(executors._recover_step("gone-0"))
    assert seen == [(settings, "gone-0")]
    assert report["reenqueued"] == ("a",)
    assert report["skipped"] == (("b", "version 'v0' is running"),)
    assert report["refused"] is None
    assert set(report) == {
        "reenqueued",
        "forked",
        "retired",
        "left_running",
        "failed",
        "skipped",
        "running_versions",
        "refused",
    }


@pytest.mark.parametrize(
    ("report", "count"),
    [({"forked": (("a", "b"),)}, 1), ({"forked": []}, 0), ({}, 0), ({"forked": None}, 0)],
)
def test_report_items_are_counted_whatever_their_shape(
    report: dict[str, object], count: int
) -> None:
    """The finishing log line never fails on a report it does not expect."""
    assert len(executors._items(report, "forked")) == count


def test_the_workflow_is_registered_under_the_name_the_api_enqueues() -> None:
    """The API enqueues by name; renaming the function must not strand its requests."""
    assert executors.WORKFLOW_NAME == "recover_executor"
