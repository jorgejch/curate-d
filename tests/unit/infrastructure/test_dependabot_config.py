"""The Dependabot config's shape: ecosystems, target branch, schedule, groups and ignores."""

from pathlib import Path
from typing import cast

import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
CONFIG = REPOSITORY_ROOT / ".github" / "dependabot.yml"

type Document = dict[str, object]

_SCHEDULE: Document = {
    "interval": "weekly",
    "day": "friday",
    "time": "16:00",
    "timezone": "America/Los_Angeles",
}

# Each group by ecosystem: the update types it takes. Anything not listed opens on its own.
_GROUPS: dict[str, tuple[str, list[str]]] = {
    "uv": ("python-patches", ["patch"]),
    "github-actions": ("actions-patches-and-minors", ["patch", "minor"]),
    "docker": ("image-patches", ["patch"]),
}

_MINOR = "version-update:semver-minor"
_MAJOR = "version-update:semver-major"
_PATCH = "version-update:semver-patch"


def _entries() -> list[Document]:
    """Return the `updates` entries of the Dependabot config."""
    config = cast(Document, yaml.safe_load(CONFIG.read_text(encoding="utf-8")))
    return cast(list[Document], config["updates"])


def _entry(ecosystem: str) -> Document:
    """Return the single `updates` entry for `ecosystem`."""
    matches = [e for e in _entries() if e["package-ecosystem"] == ecosystem]
    assert len(matches) == 1, ecosystem
    return matches[0]


def _ignore(ecosystem: str, dependency: str) -> list[str]:
    """Return the ignored update types for `dependency` in the `ecosystem` entry."""
    ignores = cast(list[Document], _entry(ecosystem).get("ignore", []))
    matches = [i for i in ignores if i["dependency-name"] == dependency]
    assert len(matches) == 1, dependency
    return cast(list[str], matches[0]["update-types"])


def test_the_three_ecosystems_are_checked_and_compose_is_not() -> None:
    """uv and the workflows at the root, the Dockerfile under /docker; no Compose images."""
    found = {cast(str, e["package-ecosystem"]): e["directory"] for e in _entries()}
    assert found == {"uv": "/", "github-actions": "/", "docker": "/docker"}, found
    assert len(_entries()) == len(found)
    assert "docker-compose" not in found


def test_every_entry_targets_dev() -> None:
    """Without target-branch every pull request opens against main, not the active branch."""
    for entry in _entries():
        assert entry.get("target-branch") == "dev", entry["package-ecosystem"]


def test_every_entry_runs_weekly_on_friday_afternoon_pacific() -> None:
    """One run a week, Friday 16:00 America/Los_Angeles, on every ecosystem."""
    for entry in _entries():
        assert entry.get("schedule") == _SCHEDULE, entry["package-ecosystem"]


def test_uv_raises_the_declared_ranges() -> None:
    """`increase` raises the pyproject.toml range with the lock, not the lock alone."""
    assert _entry("uv").get("versioning-strategy") == "increase"


def test_each_ecosystem_groups_only_its_allowed_update_types() -> None:
    """Patches group everywhere, action minors too; every major opens on its own."""
    for ecosystem, (name, update_types) in _GROUPS.items():
        groups = cast(Document, _entry(ecosystem).get("groups", {}))
        assert list(groups) == [name], ecosystem
        group = cast(Document, groups[name])
        assert group.get("applies-to") == "version-updates", name
        assert group.get("patterns") == ["*"], name
        assert group.get("update-types") == update_types, name


def test_aioboto3_ignores_minors_only() -> None:
    """A new minor waits for RustFS and OVH re-verification; patches still flow."""
    assert _ignore("uv", "aioboto3") == [_MINOR], "aioboto3"


def test_the_python_image_ignores_minors_and_majors() -> None:
    """The image follows the Python minor the repository declares, patches included."""
    assert sorted(_ignore("docker", "python")) == [_MAJOR, _MINOR], "python"


def test_no_ignore_holds_back_patches() -> None:
    """A patch is never ignored, so every pinned dependency keeps getting fixes."""
    for entry in _entries():
        for ignore in cast(list[Document], entry.get("ignore", [])):
            name = ignore["dependency-name"]
            # Without update-types, or with a versions range, an ignore can cover patches.
            update_types = cast(list[str], ignore.get("update-types", []))
            assert update_types, name
            assert _PATCH not in update_types, name
            assert "versions" not in ignore, name
