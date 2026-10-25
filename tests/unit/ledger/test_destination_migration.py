"""Migration 0023's frozen identity derivation, policy check and choice of evidence.

The migration carries its own copy of the derivation and of the destination check, so
that its result never changes with the application; these tests hold the copies to the
application's at this revision.
"""

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import pytest

from curate_d.domain import (
    DestinationConfig,
    check_destinations,
    derive_destination_id,
    normalize_root,
)

_MIGRATION = (
    Path(__file__).resolve().parents[3] / "migrations" / "versions" / "0023_destination_identity.py"
)
_T0 = datetime(2026, 10, 1, tzinfo=UTC)
_AT_A = ("pcloud", "a", "/Library")
_AT_B = ("pcloud", "b", "/Library")


@pytest.fixture(scope="module")
def migration() -> ModuleType:
    """Load the migration module by path; migrations are not a package."""
    spec = importlib.util.spec_from_file_location("destination_identity", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _versions(
    migration: ModuleType, *destinations: dict[str, tuple[str, str, str]]
) -> list[object]:
    """Return policy versions 1, 2, ... activated a day apart from `_T0`."""
    return [
        migration.PolicyVersion(index + 1, _T0 + timedelta(days=index), by_name)
        for index, by_name in enumerate(destinations)
    ]


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


@pytest.mark.parametrize(
    ("root", "canonical"), [("/", "/"), ("///", "/"), ("/a/", "/a"), ("/a/b//", "/a/b")]
)
def test_the_frozen_normalization_drops_trailing_slashes(
    migration: ModuleType, root: str, canonical: str
) -> None:
    """Two spellings of one root backfill to one identity."""
    assert migration.normalize_root(root) == canonical


@pytest.mark.parametrize("root", ["relative", "/a//b", "/a/./b", "/a/../b"])
def test_the_frozen_normalization_refuses_what_the_domain_refuses(
    migration: ModuleType, root: str
) -> None:
    """A root the application would refuse is not backfilled either."""
    with pytest.raises(ValueError, match="root path"):
        migration.normalize_root(root)
    with pytest.raises(ValueError, match="root path"):
        normalize_root(root)


def test_the_recorded_location_wins_over_every_policy_version(migration: ModuleType) -> None:
    """A start that named its location needs no policy history."""
    versions = _versions(migration, {"lib": _AT_A})
    details = {
        "destination_provider": "pcloud",
        "destination_account": "b",
        "destination_root": "/Library/",
        "policy_version": 1,
    }
    assert migration.resolve(versions, "lib", _T0 + timedelta(hours=1), details) == _AT_B


def test_the_named_version_wins_over_the_one_in_force(migration: ModuleType) -> None:
    """An attempt enqueued under version 1 and started after version 2 went to version 1's place."""
    versions = _versions(migration, {"lib": _AT_A}, {"lib": _AT_B})
    started = _T0 + timedelta(days=2)
    assert migration.resolve(versions, "lib", started, {"policy_version": 1}) == _AT_A
    assert migration.resolve(versions, "lib", started, {}) == _AT_B


def test_the_version_in_force_at_the_start_decides(migration: ModuleType) -> None:
    """A start after a redefinition resolves to the new account, one before it to the old."""
    versions = _versions(migration, {"lib": _AT_A}, {"lib": _AT_B})
    assert migration.resolve(versions, "lib", _T0 + timedelta(hours=1), {}) == _AT_A
    assert migration.resolve(versions, "lib", _T0 + timedelta(days=2), {}) == _AT_B


@pytest.mark.parametrize("details", [{}, {"policy_version": 2}])
def test_a_name_missing_from_the_version_found_falls_back_to_an_earlier_one(
    migration: ModuleType, details: dict[str, object]
) -> None:
    """An attempt enqueued just before a change still resolves to where it was sent."""
    versions = _versions(migration, {"lib": _AT_A}, {"other": ("pcloud", "b", "/Other")})
    assert migration.resolve(versions, "lib", _T0 + timedelta(days=2), details) == _AT_A


def test_a_start_before_any_version_or_an_unknown_name_is_unresolved(
    migration: ModuleType,
) -> None:
    """Nothing is guessed."""
    versions = _versions(migration, {"lib": _AT_A})
    assert migration.resolve(versions, "lib", _T0 - timedelta(seconds=1), {}) is None
    assert migration.resolve(versions, "nowhere", _T0 + timedelta(days=1), {}) is None


def test_an_incomplete_recorded_location_is_not_used(migration: ModuleType) -> None:
    """A start missing one location part falls back to the policy, never to a partial guess."""
    versions = _versions(migration, {"lib": _AT_A})
    details = {"destination_provider": "pcloud", "destination_account": "b"}
    assert migration.resolve(versions, "lib", _T0 + timedelta(hours=1), details) == _AT_A


@pytest.mark.parametrize(
    "specs",
    [
        (("a", "1", "/Library"), ("b", "2", "/Library")),
        (("a", "1", "/Library"), ("b", "1", "/Archive")),
        (("a", "1", "/Library"), ("b", "1", "/Library2")),
        (("a", "1", "/Library"), ("b", "1", "/Library/")),
        (("a", "1", "/Library"), ("b", "1", "/Library/Photos")),
        (("a", "1", "/"), ("b", "1", "/Anything")),
        (("a", "1", "/Library"), ("b", "2", "/"), ("c", "2", "/Library")),
    ],
)
def test_the_frozen_policy_check_refuses_what_the_domain_refuses(
    migration: ModuleType, specs: tuple[tuple[str, str, str], ...]
) -> None:
    """The upgrade refuses an active policy exactly when the application would not load it."""
    documents = [
        {"name": name, "provider": "pcloud", "account": account, "root_path": root}
        for name, account, root in specs
    ]
    configs = [DestinationConfig.model_validate(d, strict=False) for d in documents]
    try:
        check_destinations(configs)
    except ValueError:
        refused = True
    else:
        refused = False
    assert bool(migration.policy_conflicts(documents)) is refused


@pytest.mark.parametrize(
    ("second_root", "conflict"),
    [("/Library/", "share files"), ("/Library/Photos", "share files"), ("/a//b", "root path")],
)
def test_policy_conflicts_name_the_destinations(
    migration: ModuleType, second_root: str, conflict: str
) -> None:
    """Shared and nested roots name both destinations; a bad root names its own."""
    [reported] = migration.policy_conflicts(
        [
            {"name": "a", "provider": "pcloud", "account": "1", "root_path": "/Library"},
            {"name": "b", "provider": "pcloud", "account": "1", "root_path": second_root},
        ]
    )
    assert conflict in reported
    assert "'b'" in reported
