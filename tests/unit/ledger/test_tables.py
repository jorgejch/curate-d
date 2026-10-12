"""Schema shape decisions that no query expresses, pinned so they are not undone silently."""

from sqlalchemy import DateTime, DefaultClause

from curate_d.ledger.tables import lease_versions, staged_objects


def test_the_staging_cache_index_carries_no_secondary_index() -> None:
    """Keep created_at unindexed: age-based eviction reads LastModified from S3 instead."""
    assert staged_objects.indexes == set()


def test_a_version_has_one_nonnullable_registry_entry() -> None:
    """A version's primary key prevents duplicate deploy-order entries on worker restarts."""
    assert list(lease_versions.primary_key.columns.keys()) == ["version"]
    assert not lease_versions.c.version.nullable
    assert not lease_versions.c.first_registered.nullable


def test_version_registration_time_comes_from_the_database_with_timezone() -> None:
    """Workers cannot stamp deploy order using an absent default or a naive timestamp."""
    column = lease_versions.c.first_registered
    assert isinstance(column.type, DateTime)
    assert column.type.timezone
    assert column.default is None
    assert isinstance(column.server_default, DefaultClause)
    assert str(column.server_default.arg) == "now()"
