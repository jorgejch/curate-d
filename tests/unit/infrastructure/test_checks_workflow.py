"""The checks workflow's shape: the required check, its triggers and its coverage report."""

from pathlib import Path
from typing import cast

import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "checks.yml"

type Document = dict[str, object]

# PyYAML parses the bare `on:` key as the boolean True, per YAML 1.1.
type RawDocument = dict[object, object]

_PROTECTED_PUSH = (
    "github.event_name == 'push' &&\n"
    "(github.ref == 'refs/heads/dev' || github.ref == 'refs/heads/main')"
)


def _workflow() -> RawDocument:
    """Parse the checks workflow YAML."""
    return cast(RawDocument, yaml.safe_load(WORKFLOW.read_text(encoding="utf-8")))


def _job(job_id: str) -> Document:
    """Return the job called `job_id`."""
    return cast(Document, cast(Document, _workflow()["jobs"])[job_id])


def _steps(job_id: str) -> list[Document]:
    """Return the steps of the job called `job_id`."""
    return cast(list[Document], _job(job_id)["steps"])


def _step(job_id: str, name: str) -> Document:
    """Return the step called `name` in the job called `job_id`."""
    return next(s for s in _steps(job_id) if s.get("name") == name)


def _condition(job_id: str) -> str:
    """Return a job's `if:` with the `${{ }}` wrapper and line folding removed."""
    raw = cast(str, _job(job_id)["if"]).strip()
    return raw.removeprefix("${{").removesuffix("}}").strip()


def test_the_checks_job_keeps_the_required_check_name() -> None:
    """The ruleset requires the check by this name; a rename leaves every PR waiting."""
    assert _job("checks")["name"] == "Checks"


def test_every_branch_push_and_every_pr_to_dev_or_main_is_checked() -> None:
    """Feature, dev and main pushes, and PRs to the long-lived branches; no tags."""
    on = cast(Document, _workflow()[True])
    assert cast(Document, on["push"])["branches"] == ["**"]
    assert "tags" not in cast(Document, on["push"])
    assert cast(Document, on["pull_request"])["branches"] == ["dev", "main"]


def test_the_two_jobs_split_on_a_protected_branch_push() -> None:
    """`checks` runs everywhere but a dev/main push, `coverage-report` only there."""
    protected = " ".join(_PROTECTED_PUSH.split())
    assert " ".join(_condition("coverage-report").split()) == protected
    assert " ".join(_condition("checks").split()) == f"!({protected})"


def test_both_jobs_write_the_unit_coverage_report() -> None:
    """SonarQube reads coverage.xml; only the unit suite writes it."""
    command = "uv run --locked --no-build pytest -n auto --cov-report=xml"
    assert _step("checks", "Unit tests")["run"] == command
    assert _step("coverage-report", "Unit tests")["run"] == command


def test_the_checks_job_lints_and_type_checks() -> None:
    """The required check is the whole of `make check`, not the unit suite alone."""
    runs = " ".join(str(s.get("run", "")) for s in _steps("checks"))
    assert "ruff check ." in runs
    assert "ruff format --check ." in runs
    assert "curate_d.runtime.workflow_audit" in runs
    assert "mypy" in runs


def test_the_coverage_report_neither_lints_nor_type_checks() -> None:
    """The merged PR already ran them; this job only refreshes the branch's coverage."""
    runs = " ".join(str(s.get("run", "")) for s in _steps("coverage-report"))
    assert "ruff" not in runs
    assert "mypy" not in runs


def test_the_scan_skips_only_fork_pull_requests() -> None:
    """A fork's PR has no SONAR_TOKEN; any other run without one must fail, not skip."""
    condition = cast(str, _step("checks", "SonarQube scan")["if"])
    assert "github.event.pull_request.head.repo.full_name == github.repository" in condition
