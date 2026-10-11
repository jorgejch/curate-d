"""Staging keys and their containment within the root."""

from pathlib import Path
from uuid import UUID

import pytest

from curate_d.domain.ids import SourceObjectId, derive_source_object_id
from curate_d.staging.errors import StagingKeyError, StagingPathEscapeError
from curate_d.staging.keys import derive_staging_key, entry_path, parse_staging_key


def test_key_derives_from_the_source_object_identifier() -> None:
    """Keyed by source object because the fingerprint is not known until after the read."""
    source_id = derive_source_object_id("pcloud", "acct", "12345")
    assert parse_staging_key(derive_staging_key(source_id)) == source_id.hex


@pytest.mark.parametrize(
    "candidate",
    [
        "../../etc/passwd",
        "..",
        "a/b",
        "",
        "not-hex",
        # A formatted UUID names the same source object as its dashless form; accepting
        # both would stage one file twice under two keys.
        "00000000-0000-0000-0000-000000000001",
        "0000000000000000000000000000000A",
    ],
)
def test_a_key_that_is_not_a_derived_identifier_is_rejected(candidate: str) -> None:
    with pytest.raises(StagingKeyError):
        parse_staging_key(candidate)


def test_a_traversing_key_cannot_be_constructed(staging_root: Path) -> None:
    """Traversal is unrepresentable: the key alphabet has no separator and no dot."""
    with pytest.raises(StagingKeyError):
        entry_path(staging_root, parse_staging_key("../../../etc/passwd"))


def test_a_symlink_planted_inside_the_root_is_refused(staging_root: Path, tmp_path: Path) -> None:
    """What key validation cannot see: an escape built on the volume itself."""
    outside = tmp_path / "outside"
    outside.write_text("not ours")
    key = derive_staging_key(SourceObjectId(UUID(int=7)))

    planted = staging_root / key[:2] / key
    planted.parent.mkdir(parents=True)
    planted.symlink_to(outside)

    with pytest.raises(StagingPathEscapeError, match="outside the staging root"):
        entry_path(staging_root, key)


def test_entries_are_sharded_under_the_root(staging_root: Path) -> None:
    key = derive_staging_key(SourceObjectId(UUID(int=1)))
    path = entry_path(staging_root, key)
    assert path.parent.name == key[:2]
    assert path.is_relative_to(staging_root.resolve())
