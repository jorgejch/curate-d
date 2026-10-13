"""Workflow history retention.

The boundary arithmetic and where the period comes from are the whole of the decision;
the deletion itself is DBOS's. Both halves matter: a period read from a constant cannot
be changed without a release, and a boundary computed wrong prunes either nothing or the
wrong month's records.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from curate_d.config import Settings
from curate_d.runtime import retention
from curate_d.runtime.retention import (
    MILLISECONDS_PER_SECOND,
    TERMINAL_STATUSES,
    RetentionWindow,
    prune_completed_workflows,
    retention_window,
)

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)


def test_the_default_retention_period_is_thirty_days() -> None:
    assert Settings().workflow_retention_days == 30


def test_the_period_is_read_from_configuration_not_a_constant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Changing how long history is kept must not require a release."""
    monkeypatch.setenv("CURATE_D_WORKFLOW_RETENTION_DAYS", "7")
    assert retention_window(Settings(), now=NOW).retention_days == 7


def test_the_cutoff_is_the_configured_period_before_now(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CURATE_D_WORKFLOW_RETENTION_DAYS", "7")
    assert retention_window(Settings(), now=NOW).cutoff == NOW - timedelta(days=7)


def test_the_default_cutoff_is_thirty_days_back() -> None:
    assert retention_window(Settings(), now=NOW).cutoff == NOW - timedelta(days=30)


def test_the_cutoff_is_expressed_in_epoch_milliseconds() -> None:
    """Checkpoints are stamped in milliseconds; seconds would prune a thousandfold too far back."""
    window = retention_window(Settings(), now=NOW)
    assert window.cutoff_epoch_ms == int(window.cutoff.timestamp() * MILLISECONDS_PER_SECOND)
    assert window.cutoff_epoch_ms == int((NOW - timedelta(days=30)).timestamp() * 1000)


def test_a_zero_day_period_is_rejected_by_configuration() -> None:
    """It would delete records the moment they complete, leaving nothing to inspect."""
    with pytest.raises(ValueError, match="workflow_retention_days"):
        Settings(workflow_retention_days=0)


def test_a_negative_period_is_rejected_by_configuration() -> None:
    with pytest.raises(ValueError, match="workflow_retention_days"):
        Settings(workflow_retention_days=-1)


def test_a_hand_built_window_with_a_bad_period_still_fails_loudly() -> None:
    """Settings validation is the first gate, not the only one."""

    class _Unvalidated:
        workflow_retention_days = 0

    with pytest.raises(ValueError, match="at least one day"):
        retention_window(_Unvalidated())  # type: ignore[arg-type]


def test_a_window_built_directly_reports_its_own_boundary() -> None:
    window = RetentionWindow(retention_days=1, cutoff=NOW)
    assert window.cutoff_epoch_ms == int(NOW.timestamp() * 1000)


@dataclass
class _Status:
    """The one field of DBOS's `WorkflowStatus` the prune reads."""

    workflow_id: str


@dataclass
class _FakeDBOS:
    """Serve listed workflow IDs in batches and record every list and delete call."""

    rows: list[str]
    listed: list[dict[str, object]] = field(default_factory=list)
    deleted: list[list[str]] = field(default_factory=list)

    def list_workflows(
        self,
        *,
        status: list[str],
        completed_before: str,
        load_input: bool,
        load_output: bool,
        limit: int,
    ) -> list[_Status]:
        """Return the first `limit` rows not yet deleted."""
        self.listed.append(
            {
                "status": status,
                "completed_before": completed_before,
                "load_input": load_input,
                "load_output": load_output,
                "limit": limit,
            }
        )
        return [_Status(workflow_id) for workflow_id in self.rows[:limit]]

    def delete_workflows(self, workflow_ids: list[str]) -> None:
        """Drop the given rows, as DBOS's delete does."""
        self.deleted.append(workflow_ids)
        self.rows = [row for row in self.rows if row not in workflow_ids]


def test_the_prune_deletes_listed_batches_until_one_comes_back_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Five eligible rows in batches of two take three deletes and a final empty list."""
    fake = _FakeDBOS(rows=[f"wf-{index}" for index in range(5)])
    monkeypatch.setattr(retention, "DBOS", fake)

    deleted = prune_completed_workflows(NOW, batch_size=2)

    assert deleted == 5
    assert fake.deleted == [["wf-0", "wf-1"], ["wf-2", "wf-3"], ["wf-4"]]
    assert len(fake.listed) == 4


def test_the_prune_lists_only_terminal_rows_completed_by_the_cutoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In-flight rows are never listed, and no input or output is loaded to list them."""
    fake = _FakeDBOS(rows=[])
    monkeypatch.setattr(retention, "DBOS", fake)

    assert prune_completed_workflows(NOW, batch_size=7) == 0

    assert fake.listed == [
        {
            "status": list(TERMINAL_STATUSES),
            "completed_before": NOW.isoformat(),
            "load_input": False,
            "load_output": False,
            "limit": 7,
        }
    ]
    assert set(TERMINAL_STATUSES) == {
        "SUCCESS",
        "ERROR",
        "CANCELLED",
        "MAX_RECOVERY_ATTEMPTS_EXCEEDED",
    }
    assert fake.deleted == []
