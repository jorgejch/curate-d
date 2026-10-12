"""Schema shape decisions that no query expresses, pinned so they are not undone silently."""

from sqlalchemy import CheckConstraint

from curate_d.domain import ReviewReason
from curate_d.ledger.tables import review_items, staged_objects


def test_the_staging_cache_index_carries_no_secondary_index() -> None:
    """Keep created_at unindexed: age-based eviction reads LastModified from S3 instead."""
    assert staged_objects.indexes == set()


def test_every_review_reason_is_allowed_by_the_reason_check() -> None:
    """A reason the enum gains is one the table accepts, resource_limit included."""
    [check] = [
        constraint
        for constraint in review_items.constraints
        if isinstance(constraint, CheckConstraint) and str(constraint.name).endswith("reason_known")
    ]
    sql = str(check.sqltext)
    assert all(f"'{reason.value}'" in sql for reason in ReviewReason)
    assert "'resource_limit'" in sql
