"""The Dependabot config's shape: ecosystems, target branch, schedule, groups and ignores."""

import re
from pathlib import Path
from typing import cast

import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
CONFIG = REPOSITORY_ROOT / ".github" / "dependabot.yml"
APPROVE_CRITERIA = REPOSITORY_ROOT / ".gitar" / "config" / "approve.md"

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

# The words approve.md's ignored list uses for each of dependabot.yml's update types.
_UPDATE_TYPE_WORDS = {"patch": _PATCH, "minor": _MINOR, "major": _MAJOR}
_NOT_IGNORED = re.compile(
    r"\*\*Not ignored\.\*\*(?P<body>.*?)\*\*Version references only\.\*\*", re.S
)
_IGNORED_ITEM = re.compile(
    r"^\s+- `(?P<ecosystem>[a-z-]+)` `(?P<dependency>[^`]+)`: (?P<types>[a-z ,]+)\.$", re.M
)


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


def _approve_criteria_ignores() -> set[tuple[str, str, str]]:
    """Return the (ecosystem, dependency, update type) triples approve.md lists as ignored."""
    match = _NOT_IGNORED.search(APPROVE_CRITERIA.read_text(encoding="utf-8"))
    assert match, "approve.md has no **Not ignored.** condition before **Version references only.**"
    listed: set[tuple[str, str, str]] = set()
    for item in _IGNORED_ITEM.finditer(match["body"]):
        for word in re.split(r",\s*|\s+and\s+", item["types"].strip()):
            listed.add((item["ecosystem"], item["dependency"], _UPDATE_TYPE_WORDS[word]))
    return listed


def _config_ignores() -> set[tuple[str, str, str]]:
    """Return the (ecosystem, dependency, update type) triples dependabot.yml ignores."""
    ignored: set[tuple[str, str, str]] = set()
    for entry in _entries():
        for ignore in cast(list[Document], entry.get("ignore", [])):
            for update_type in cast(list[str], ignore.get("update-types", [])):
                ignored.add(
                    (
                        cast(str, entry["package-ecosystem"]),
                        cast(str, ignore["dependency-name"]),
                        update_type,
                    )
                )
    return ignored


def test_approve_criteria_list_exactly_the_ignored_updates() -> None:
    """Gitar's review can't read dependabot.yml, so approve.md copies its ignore rules."""
    assert _approve_criteria_ignores() == _config_ignores()


def test_action_updates_wait_a_week_and_nothing_else_sets_a_cooldown() -> None:
    """A new action release waits a week before Dependabot proposes it; no other entry delays."""
    cooldown = cast(Document, _entry("github-actions").get("cooldown", {}))
    assert cast(int, cooldown.get("default-days", 0)) >= 7, cooldown
    # A per-update-type key overrides default-days for that type, so none may shorten the wait.
    for key in ("semver-major-days", "semver-minor-days", "semver-patch-days"):
        if key in cooldown:
            assert cast(int, cooldown[key]) >= 7, (key, cooldown)
    for entry in _entries():
        if entry["package-ecosystem"] != "github-actions":
            assert "cooldown" not in entry, entry["package-ecosystem"]
