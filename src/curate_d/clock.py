"""The wall clock read wherever no other clock is injected."""

from datetime import UTC, datetime


def utc_now() -> datetime:
    """Return the current instant, timezone-aware so it compares with stored timestamps."""
    return datetime.now(UTC)


def age_in_days(since: datetime) -> int:
    """Return how many whole days have passed since an instant."""
    now = datetime.now()
    return (now - since).days + 1
