"""Schema shape decisions that no query expresses, pinned so they are not undone silently."""

from curate_d.ledger.tables import staged_objects


def test_the_staging_cache_index_carries_no_secondary_index() -> None:
    """Keep created_at unindexed: age-based eviction reads LastModified from S3 instead."""
    assert staged_objects.indexes == set()
