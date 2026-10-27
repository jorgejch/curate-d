"""SonarQube's explicit source/test roots and production metric scope."""

from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
PROPERTIES = REPOSITORY_ROOT / "sonar-project.properties"


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
    """Return the comma-separated patterns of the property called `key`, none if it is unset."""
    return {pattern.strip() for pattern in _properties().get(key, "").split(",") if pattern.strip()}


def _assert_excludes_exactly(key: str, expected: set[str]) -> None:
    """Assert `key` lists exactly the patterns in `expected`."""
    actual = _exclusions(key)
    assert not expected - actual, f"{key} misses {sorted(expected - actual)}"
    assert not actual - expected, f"{key} also lists {sorted(actual - expected)}"


def test_analysis_uses_explicit_source_and_test_roots() -> None:
    """Only src/ and infrastructure/ are sources; tests/ is explicitly test code."""
    properties = _properties()
    for key, expected in {"sonar.sources": "src,infrastructure", "sonar.tests": "tests"}.items():
        actual = properties.get(key)
        assert actual == expected, f"{key} must be {expected!r}, got {actual!r}"


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
    """Only infrastructure/ needs a coverage exclusion within the selected source roots."""
    _assert_excludes_exactly("sonar.coverage.exclusions", {"infrastructure/**"})


def test_duplication_counts_src_and_infrastructure_only() -> None:
    """Both source roots count toward duplication without exclusions."""
    assert "sonar.cpd.exclusions" not in _properties(), "Remove sonar.cpd.exclusions"


def test_analysis_needs_no_inclusions_or_exclusions() -> None:
    """Explicit roots replace all source and test analysis filters."""
    properties = _properties()
    for key in (
        "sonar.inclusions",
        "sonar.exclusions",
        "sonar.test.inclusions",
        "sonar.test.exclusions",
    ):
        assert key not in properties, f"Remove {key}; sonar.sources and sonar.tests define scope"


def test_no_out_of_scope_rule_suppression_remains() -> None:
    """Dockerfiles outside the analysis roots need no rule suppression."""
    stale = sorted(
        key for key in _properties() if key.startswith("sonar.issue.ignore.multicriteria")
    )
    assert not stale, f"Remove out-of-scope Docker rule suppression settings: {stale}"


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
