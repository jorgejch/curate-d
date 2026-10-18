"""SonarQube's measured scope: coverage on src/ only, duplication on src/ and infrastructure/."""

import subprocess
import tomllib
from pathlib import Path
from typing import cast

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
PROPERTIES = REPOSITORY_ROOT / "sonar-project.properties"
PYPROJECT = REPOSITORY_ROOT / "pyproject.toml"

# `*` matches the files at the repository root.
_ROOT_FILES = "*"


def _properties() -> dict[str, str]:
    """Parse sonar-project.properties, joining backslash-continued lines."""
    properties: dict[str, str] = {}
    logical = ""
    for line in PROPERTIES.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not logical and (not stripped or stripped.startswith("#")):
            continue
        if stripped.endswith("\\"):
            logical += stripped.removesuffix("\\")
            continue
        key, _, value = (logical + stripped).partition("=")
        properties[key.strip()] = value.strip()
        logical = ""
    return properties


def _exclusions(key: str) -> set[str]:
    """Return the comma-separated patterns of the property called `key`."""
    return {pattern.strip() for pattern in _properties()[key].split(",") if pattern.strip()}


def _tracked_directories() -> set[str]:
    """Return the repository's tracked top-level directories; ignored ones never count."""
    listing = subprocess.run(
        ["git", "ls-files"], cwd=REPOSITORY_ROOT, capture_output=True, text=True, check=True
    )
    return {path.split("/")[0] for path in listing.stdout.splitlines() if "/" in path}


def _test_code_directories() -> set[str]:
    """Return the directories SonarQube takes as test code, from pytest's `testpaths`."""
    pyproject = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    pytest_options = cast(dict[str, object], pyproject["tool"]["pytest"]["ini_options"])
    return {str(path).rstrip("/") for path in cast(list[str], pytest_options["testpaths"])}


def _assert_measures_only(key: str, measured: set[str]) -> None:
    """Assert `key` excludes the root files and every tracked directory but `measured`."""
    expected = {_ROOT_FILES} | {f"{d}/**" for d in _tracked_directories() - measured}
    actual = _exclusions(key)
    assert not expected - actual, f"{key} misses {sorted(expected - actual)}"
    assert not actual - expected, f"{key} also lists {sorted(actual - expected)}"


def test_coverage_counts_src_only() -> None:
    """Every other top-level directory, tests/ included, is excluded from coverage."""
    _assert_measures_only("sonar.coverage.exclusions", {"src"})


def test_duplication_counts_src_and_infrastructure_only() -> None:
    """tests/ is test code, which SonarQube measures no duplication on; the rest is excluded."""
    test_code = _test_code_directories()
    assert test_code == {"tests"}
    _assert_measures_only("sonar.cpd.exclusions", {"src", "infrastructure"} | test_code)
