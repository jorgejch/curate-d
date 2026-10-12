"""Migration 0020's frozen identity derivation and its choice of policy version.

The migration carries its own copy of the derivation, so that its result never changes
with the application; these tests hold the copy to the application's at this revision.
"""

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import pytest

from curate_d.domain import derive_destination_id, normalize_root

_MIGRATION = (
    Path(__file__).resolve().parents[3] / "migrations" / "versions" / "0020_destination_identity.py"
)
_T0 = datetime(2026, 10, 1, tzinfo=UTC)


@pytest.fixture(scope="module")
def migration() -> ModuleType:
    """Load the migration module by path; migrations are not a package."""
    spec = importlib.util.spec_from_file_location("destination_identity", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("provider", "account", "root"),
    [
        ("pcloud", "1", "/"),
        ("pcloud", "1", "/Library"),
        ("pcloud", "1", "/Library/"),
        ("pcloud", "someone@example.org", "/a/b/c"),
        ("pcloud", "acct:with:colons", "/Ünïcode/Fotos"),
    ],
)
def test_the_frozen_derivation_matches_the_domain(
    migration: ModuleType, provider: str, account: str, root: str
) -> None:
    """The backfill derives the identifier the application does."""
    assert migration.derive_destination_id(provider, account, root) == derive_destination_id(
        provider, account, root
    )
    assert migration.normalize_root(root) == normalize_root(root)


@pytest.mark.parametrize("root", ["relative", "/a//b", "/a/./b", "/a/../b"])
def test_the_frozen_normalization_refuses_what_the_domain_refuses(
    migration: ModuleType, root: str
) -> None:
    """A root the application would refuse is not backfilled either."""
    with pytest.raises(ValueError, match="root path"):
        migration.normalize_root(root)
    with pytest.raises(ValueError, match="root path"):
        normalize_root(root)


def test_the_version_in_force_at_the_start_decides(migration: ModuleType) -> None:
    """A start after a redefinition resolves to the new account, one before it to the old."""
    versions = [
        (_T0, {"lib": ("pcloud", "a", "/Library")}),
        (_T0 + timedelta(days=1), {"lib": ("pcloud", "b", "/Library")}),
    ]
    assert migration.resolve(versions, "lib", _T0 + timedelta(hours=1)) == (
        "pcloud",
        "a",
        "/Library",
    )
    assert migration.resolve(versions, "lib", _T0 + timedelta(days=2)) == (
        "pcloud",
        "b",
        "/Library",
    )


def test_a_name_dropped_by_the_version_in_force_falls_back_to_an_earlier_one(
    migration: ModuleType,
) -> None:
    """An attempt enqueued just before a change still resolves to where it was sent."""
    versions = [
        (_T0, {"lib": ("pcloud", "a", "/Library")}),
        (_T0 + timedelta(days=1), {"other": ("pcloud", "b", "/Other")}),
    ]
    assert migration.resolve(versions, "lib", _T0 + timedelta(days=2)) == (
        "pcloud",
        "a",
        "/Library",
    )


def test_a_start_before_any_version_or_an_unknown_name_is_unresolved(
    migration: ModuleType,
) -> None:
    """Nothing is guessed."""
    versions = [(_T0, {"lib": ("pcloud", "a", "/Library")})]
    assert migration.resolve(versions, "lib", _T0 - timedelta(seconds=1)) is None
    assert migration.resolve(versions, "nowhere", _T0 + timedelta(days=1)) is None
