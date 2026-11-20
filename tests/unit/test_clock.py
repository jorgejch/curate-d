"""The default wall clock."""

from datetime import datetime, timedelta

from curate_d.clock import age_in_days, utc_now


def test_the_default_clock_is_timezone_aware() -> None:
    """Stored timestamps are aware, so a naive now would fail every comparison with them."""
    assert utc_now().tzinfo is not None


def test_age_in_days_counts_whole_days() -> None:
    """An instant three days back is three days old."""
    assert age_in_days(datetime.now() - timedelta(days=3)) >= 3
