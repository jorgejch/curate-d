"""The wall clock read wherever no other clock is injected."""

from datetime import UTC, datetime


def utc_now() -> datetime:
    """Return the current instant, timezone-aware so it compares with stored timestamps."""
    return datetime.now(UTC)
