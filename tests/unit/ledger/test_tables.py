"""Schema shape decisions that no query expresses, pinned so they are not undone silently."""

from sqlalchemy import Table, UniqueConstraint

from curate_d.ledger.tables import destination_checks, dry_run_items, placements, staged_objects


def _unique_column_sets(table: Table) -> set[tuple[str, ...]]:
    """Return the column names of each unique constraint on `table`."""
    return {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }


def test_the_staging_cache_index_carries_no_secondary_index() -> None:
    """Keep created_at unindexed: age-based eviction reads LastModified from S3 instead."""
    assert staged_objects.indexes == set()


def test_placement_uniqueness_is_keyed_on_the_destination_identity() -> None:
    """One placement per content per identity and one content per locator per identity."""
    assert _unique_column_sets(placements) == {
        ("content_object_id", "destination_id"),
        ("destination_id", "locator"),
    }


def test_a_placements_destination_is_a_required_reference() -> None:
    """Every placement points at a recorded destination identity."""
    column = placements.c.destination_id
    assert not column.nullable
    assert {key.target_fullname for key in column.foreign_keys} == {"destinations.id"}


def test_destination_checks_are_keyed_on_the_identity() -> None:
    """The latest check is per identity; the name is a label."""
    assert [column.name for column in destination_checks.primary_key] == ["destination_id"]


def test_dry_run_locators_are_indexed_per_identity() -> None:
    """A dry run looks up proposed locators by identity, not by name."""
    assert {tuple(column.name for column in index.columns) for index in dry_run_items.indexes} >= {
        ("run_id", "destination_id", "locator")
    }
    assert dry_run_items.c.destination_id.nullable
