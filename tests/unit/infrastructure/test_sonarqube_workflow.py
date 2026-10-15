"""The SonarQube workflow's shape: the required check, its triggers and its report."""

from pathlib import Path
from typing import cast

import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "sonarqube.yml"

type Document = dict[str, object]


def _workflow() -> dict[object, object]:
    """Parse the SonarQube workflow YAML; PyYAML keys the bare `on:` as True."""
    return cast(dict[object, object], yaml.safe_load(WORKFLOW.read_text(encoding="utf-8")))


def _job() -> Document:
    """Return the workflow's unit-test job."""
    return cast(Document, cast(Document, _workflow()["jobs"])["unit-tests"])


def _step(name: str) -> Document:
    """Return the unit-test job's step called `name`."""
    steps = cast(list[Document], _job()["steps"])
    return next(s for s in steps if s.get("name") == name)


def test_the_job_keeps_the_required_check_name() -> None:
    """The ruleset requires the check by this name; a rename leaves every PR waiting."""
    assert _job()["name"] == "Unit tests and SonarQube analysis"


def test_every_branch_push_and_every_pr_to_dev_or_main_is_analysed() -> None:
    """Feature branches, dev and main pushes, and PRs to the long-lived branches."""
    on = cast(Document, _workflow()[True])
    assert cast(Document, on["push"])["branches"] == ["**"]
    assert cast(Document, on["pull_request"])["branches"] == ["dev", "main"]


def test_only_the_unit_suite_reports_coverage() -> None:
    """SonarQube merges every report into one metric, and integration coverage does not count."""
    run = cast(str, _step("Unit tests")["run"])
    assert run == "uv run pytest -n auto --cov-report=xml"


def test_the_scan_skips_only_fork_pull_requests() -> None:
    """A fork's PR has no SONAR_TOKEN; any other run without one must fail, not skip."""
    condition = cast(str, _step("SonarQube scan")["if"])
    assert "github.event.pull_request.head.repo.full_name == github.repository" in condition
