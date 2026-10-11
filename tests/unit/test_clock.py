"""The default wall clock."""

from curate_d.clock import utc_now


def test_the_default_clock_is_timezone_aware() -> None:
    """Stored timestamps are aware, so a naive now would fail every comparison with them."""
    assert utc_now().tzinfo is not None
