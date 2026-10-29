"""SonarQube's explicit source/test roots and production metric scope."""

from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
PROPERTIES = REPOSITORY_ROOT / "sonar-project.properties"

# Scanned for secrets only: outside sonar.sources, so no language rules, coverage or duplication.
SECRET_SCAN_ONLY = frozenset({"docs/**"})

# The one analysis filter: generated pages docs/** would otherwise add to secret scanning.
ANALYSIS_EXCLUSIONS = frozenset({"docs/diagrams/**"})


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


def test_automatic_configuration_cannot_override_explicit_scope() -> None:
    """Cloud automatic configuration cannot ignore the selected source and test paths."""
    assert _properties().get("sonar.scanner.autoconfig.enabled") == "false", (
        "Disable CLI automatic configuration explicitly; Cloud otherwise ignores sonar.sources "
        "and sonar.tests when it is enabled in project settings"
    )


def _exclusions(key: str) -> set[str]:
    """Return the comma-separated patterns of the property called `key`, none if it is unset."""
    return {pattern.strip() for pattern in _properties().get(key, "").split(",") if pattern.strip()}


def _assert_excludes_exactly(key: str, expected: set[str]) -> None:
    """Assert `key` lists exactly the patterns in `expected`."""
    actual = _exclusions(key)
    assert not expected - actual, f"{key} misses {sorted(expected - actual)}"
    assert not actual - expected, f"{key} also lists {sorted(actual - expected)}"


def test_analysis_uses_explicit_source_and_test_roots() -> None:
    """Application, operations, migrations, image and config are sources; tests/ is test code."""
    properties = _properties()
    expected_roots = {
        "sonar.sources": "src,ops,migrations,docker,pyproject.toml,alembic.ini,compose.yaml",
        "sonar.tests": "tests",
    }
    for key, expected in expected_roots.items():
        actual = properties.get(key)
        assert actual == expected, f"{key} must be {expected!r}, got {actual!r}"


def test_analysis_paths_exist() -> None:
    """Every explicitly selected source or test path exists in this repository."""
    properties = _properties()
    for key in ("sonar.sources", "sonar.tests"):
        for entry in properties[key].split(","):
            assert (REPOSITORY_ROOT / entry.strip()).exists(), f"{key} path {entry!r} is missing"


def test_source_and_test_roots_are_disjoint() -> None:
    """No configured source root contains or overlaps a test root."""
    properties = _properties()
    sources = properties.get("sonar.sources", "")
    tests = properties.get("sonar.tests", "")
    assert sources, "sonar.sources must explicitly declare source roots"
    assert tests, "sonar.tests must explicitly declare test roots"
    for source in sources.split(","):
        for test in tests.split(","):
            source_root = Path(source.strip())
            test_root = Path(test.strip())
            assert not (
                source_root.is_relative_to(test_root) or test_root.is_relative_to(source_root)
            ), f"sonar.sources root {source!r} overlaps sonar.tests root {test!r}"


def test_coverage_counts_src_only() -> None:
    """Non-application sources are analysed without lowering application coverage."""
    _assert_excludes_exactly(
        "sonar.coverage.exclusions",
        {"ops/**", "migrations/**", "docker/**", "pyproject.toml", "alembic.ini", "compose.yaml"},
    )


def test_duplication_counts_all_source_roots() -> None:
    """All supported sources remain eligible for duplication without exclusions."""
    assert "sonar.cpd.exclusions" not in _properties(), "Remove sonar.cpd.exclusions"


def test_analysis_needs_no_inclusions_or_exclusions() -> None:
    """Explicit roots replace every source and test analysis filter but one."""
    properties = _properties()
    for key in (
        "sonar.inclusions",
        "sonar.test.inclusions",
        "sonar.test.exclusions",
    ):
        assert key not in properties, f"Remove {key}; sonar.sources and sonar.tests define scope"


def test_only_generated_diagrams_are_excluded() -> None:
    """sonar.exclusions keeps docs/diagrams/ out of secret scanning, and excludes nothing else."""
    _assert_excludes_exactly("sonar.exclusions", set(ANALYSIS_EXCLUSIONS))


def test_no_out_of_scope_rule_suppression_remains() -> None:
    """Dockerfiles now in scope receive checks without blanket rule suppressions."""
    stale = sorted(
        key for key in _properties() if key.startswith("sonar.issue.ignore.multicriteria")
    )
    assert not stale, f"Remove blanket rule suppression settings: {stale}"


def test_scoped_configuration_files_receive_text_analysis() -> None:
    """Project, migration and Compose config receive secret detection without false languages."""
    properties = _properties()
    expected = {"sonar.text.inclusions.activate": "true"}
    for key, value in expected.items():
        assert properties.get(key) == value, f"{key} must be {value!r} for scoped text analysis"
    patterns = {entry.strip() for entry in properties.get("sonar.text.inclusions", "").split(",")}
    assert {"pyproject.toml", "alembic.ini", "compose.yaml"} <= patterns, (
        "sonar.text.inclusions misses config files"
    )
    assert "sonar.lang.patterns.py" not in properties, "Do not classify config files as Python"


def test_text_inclusions_do_not_expand_analysis_scope() -> None:
    """Secret scanning adds docs/ and no other unselected Git-tracked path, OpenSpec included."""
    properties = _properties()
    expected = {
        f"{entry.strip()}/**" if (REPOSITORY_ROOT / entry.strip()).is_dir() else entry.strip()
        for key in ("sonar.sources", "sonar.tests")
        for entry in properties[key].split(",")
    } | SECRET_SCAN_ONLY
    actual = {entry.strip() for entry in properties.get("sonar.text.inclusions", "").split(",")}
    assert actual == expected, (
        "sonar.text.inclusions must mirror source/test paths plus docs/**; it independently adds "
        f"Git-tracked files to secret scanning. Expected {sorted(expected)}, got {sorted(actual)}"
    )


def test_project_identity_python_and_coverage_report_are_preserved() -> None:
    """Scope changes preserve the Sonar project and unit coverage import settings."""
    properties = _properties()
    expected = {
        "sonar.projectKey": "jorgejch_curate-d",
        "sonar.organization": "jorge-haddad-oss",
        "sonar.python.version": "3.12",
        "sonar.python.coverage.reportPaths": "coverage.xml",
    }
    for key, value in expected.items():
        actual = properties.get(key)
        assert actual == value, f"{key} must remain {value!r}, got {actual!r}"
