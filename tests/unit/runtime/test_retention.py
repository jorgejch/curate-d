"""Workflow history retention.

The boundary arithmetic and where the period comes from are the whole of the decision;
the deletion itself is DBOS's. Both halves matter: a period read from a constant cannot
be changed without a release, and a boundary computed wrong prunes either nothing or the
wrong month's records.
"""

from datetime import UTC, datetime, timedelta

import pytest

from curate_d.config import Settings
from curate_d.runtime.retention import (
    MILLISECONDS_PER_SECOND,
    RetentionWindow,
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
    """DBOS's collector takes milliseconds; seconds would prune a thousandfold too far back."""
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


def test_the_collector_binding_still_exists() -> None:
    """Pins a DBOS internal: an upgrade that moves it must fail here, not silently stop pruning."""
    from dbos._workflow_commands import garbage_collect

    assert callable(garbage_collect)
