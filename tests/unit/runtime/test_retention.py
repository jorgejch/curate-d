"""Workflow history retention.

The boundary arithmetic and where the period comes from are the whole of the decision;
the deletion itself is DBOS's. Both halves matter: a period read from a constant cannot
be changed without a release, and a boundary computed wrong prunes either nothing or the
wrong month's records.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from structlog.testing import capture_logs

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
    """Serve listed workflow IDs in batches and record every list, re-read and delete call.

    A row in `revived` is listed as eligible but drops out at the re-read, as a row resumed
    between the two calls does, and is no longer eligible afterwards.
    """

    rows: list[str]
    revived: set[str] = field(default_factory=set)
    listed: list[dict[str, object]] = field(default_factory=list)
    reread: list[dict[str, object]] = field(default_factory=list)
    deleted: list[list[str]] = field(default_factory=list)

    def list_workflows(
        self,
        *,
        status: list[str],
        completed_before: str,
        load_input: bool,
        load_output: bool,
        limit: int | None = None,
        workflow_ids: list[str] | None = None,
    ) -> list[_Status]:
        """Return the first `limit` eligible rows, or the eligible ones of `workflow_ids`."""
        call: dict[str, object] = {
            "status": status,
            "completed_before": completed_before,
            "load_input": load_input,
            "load_output": load_output,
        }
        if workflow_ids is None:
            self.listed.append({**call, "limit": limit})
            return [_Status(workflow_id) for workflow_id in self.rows[:limit]]
        self.reread.append({**call, "workflow_ids": workflow_ids, "limit": limit})
        self.rows = [row for row in self.rows if row not in self.revived]
        return [_Status(workflow_id) for workflow_id in workflow_ids if workflow_id in self.rows]

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


def test_a_row_revived_between_the_listing_and_the_delete_is_left_in_place(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeDBOS(rows=["wf-0", "wf-1", "wf-2"], revived={"wf-1"})
    monkeypatch.setattr(retention, "DBOS", fake)

    deleted = prune_completed_workflows(NOW, batch_size=3)

    assert deleted == 2
    assert fake.deleted == [["wf-0", "wf-2"]]


def test_the_re_read_binds_the_batchs_ids_under_the_original_terminal_and_cutoff_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeDBOS(rows=["wf-0", "wf-1", "wf-2"])
    monkeypatch.setattr(retention, "DBOS", fake)

    prune_completed_workflows(NOW, batch_size=2)

    assert fake.reread == [
        {
            "status": list(TERMINAL_STATUSES),
            "completed_before": NOW.isoformat(),
            "load_input": False,
            "load_output": False,
            "workflow_ids": batch,
            "limit": None,
        }
        for batch in (["wf-0", "wf-1"], ["wf-2"])
    ]


def test_a_revived_row_is_logged_by_id_under_workflow_history_revived_during_prune(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeDBOS(rows=["wf-0", "wf-1", "wf-2"], revived={"wf-0", "wf-2"})
    monkeypatch.setattr(retention, "DBOS", fake)

    with capture_logs() as events:
        prune_completed_workflows(NOW, batch_size=3)

    revived = [
        event for event in events if event["event"] == "workflow_history.revived_during_prune"
    ]
    assert len(revived) == 1
    assert revived[0]["log_level"] == "error"
    assert revived[0]["workflow_ids"] == ["wf-0", "wf-2"]


def test_a_batch_whose_every_row_was_revived_deletes_nothing_and_the_walk_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeDBOS(rows=["wf-0", "wf-1", "wf-2", "wf-3"], revived={"wf-0", "wf-1"})
    monkeypatch.setattr(retention, "DBOS", fake)

    deleted = prune_completed_workflows(NOW, batch_size=2)

    assert deleted == 2
    assert fake.deleted == [["wf-2", "wf-3"]]
    assert len(fake.listed) == 3
