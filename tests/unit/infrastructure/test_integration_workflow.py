"""The integration workflow's shape: its required check, trigger, suite and readiness gate."""

from pathlib import Path
from typing import cast

import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "integration.yml"
COMPOSE = REPOSITORY_ROOT / "compose.yaml"
API_APP = REPOSITORY_ROOT / "src" / "curate_d" / "api" / "app.py"
WORKER_MAIN = REPOSITORY_ROOT / "src" / "curate_d" / "worker" / "main.py"
MAKEFILE = REPOSITORY_ROOT / "Makefile"

type Document = dict[str, object]

# PyYAML parses the bare `on:` key as the boolean True, per YAML 1.1.
type RawDocument = dict[object, object]


def _workflow() -> RawDocument:
    """Parse the integration workflow YAML."""
    return cast(RawDocument, yaml.safe_load(WORKFLOW.read_text(encoding="utf-8")))


def _job() -> Document:
    """Return the workflow's integration job."""
    return cast(Document, cast(Document, _workflow()["jobs"])["integration"])


def _steps() -> list[Document]:
    """Return the integration job's steps."""
    return cast(list[Document], _job()["steps"])


def _index(name: str) -> int:
    """Return the position of the step called `name`."""
    return next(i for i, s in enumerate(_steps()) if s.get("name") == name)


def _suite_command() -> str:
    """Return the integration suite's pytest command."""
    return cast(str, _steps()[_index("Integration suite")]["run"])


def _test_stack_recipe() -> str:
    """Return the `test-stack` target's recipe lines from the Makefile."""
    lines = MAKEFILE.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("test-stack:"))
    recipe = []
    for line in lines[start + 1 :]:
        if not line.startswith("\t"):
            break
        recipe.append(line.strip())
    return "\n".join(recipe)


def _expanded(run: str) -> str:
    """Return a step's script, with `make test-stack` replaced by its Makefile recipe."""
    return run.replace("make test-stack", _test_stack_recipe())


def test_the_job_keeps_the_required_check_name() -> None:
    """The ruleset requires the check by this name; a rename leaves every PR waiting."""
    assert _job()["name"] == "Integration tests"


def test_only_pull_requests_to_dev_or_main_run_the_suite() -> None:
    """Feature pushes never run it; the PR run is the one that gates."""
    on = cast(Document, _workflow()[True])
    assert set(on) == {"pull_request"}
    assert cast(Document, on["pull_request"])["branches"] == ["dev", "main"]


def test_the_whole_suite_runs_with_nothing_deselected() -> None:
    """Every file under tests/integration gates the merge, not a chosen subset."""
    command = _suite_command()
    assert "pytest tests/integration " in command
    assert "-m integration" in command
    assert "--deselect" not in command
    assert " -k " not in command


def test_rustfs_is_the_only_staging_backend() -> None:
    """OVH needs credentials and days; it stays PENDING rather than failing the PR."""
    assert "--staging-backend=rustfs" in _suite_command()
    assert "ovh" not in _suite_command()


def test_integration_coverage_never_reaches_sonarqube() -> None:
    """SonarQube merges every report into one metric, and integration coverage does not count."""
    command = _suite_command()
    assert "--no-cov" in command
    assert "--cov-report" not in command


def test_a_ci_sized_lifecycle_budget_fails_before_the_suite() -> None:
    """A budget short enough for a job would record unobserved reclamation as verified."""
    guard = _steps()[_index("Refuse a CI-sized observation budget")]
    assert guard["if"] == "env.CURATE_D_TEST_STAGING_LIFECYCLE_TIMEOUT_SECONDS != ''"
    assert "exit 1" in cast(str, guard["run"])
    assert _index("Refuse a CI-sized observation budget") < _index("Integration suite")


def test_migrations_run_before_the_application_services_start() -> None:
    """DBOS must not launch against a schema Alembic is still changing."""
    migrations = "Start PostgreSQL, RustFS and rclone, and apply migrations"
    assert _index(migrations) < _index("Start the application services")


def test_no_application_service_runs_beside_the_suite() -> None:
    """A worker's persisted schedules would tick in the test process, on the suite's version."""
    suite = _index("Integration suite")
    assert _index("Start the application services") > suite
    started = False
    for step in _steps()[:suite]:
        for line in _expanded(cast(str, step.get("run", ""))).splitlines():
            if "compose" in line.lower() and " up " in line:
                started = True
                assert "postgres rustfs rclone" in line, step["name"]
                assert " api" not in line and "worker" not in line, step["name"]
    assert started


def test_ci_starts_the_suites_services_with_the_developers_target() -> None:
    """CI and a local run start the same services, so neither can drift from the other."""
    suite = _index("Integration suite")
    assert any("make test-stack" in cast(str, s.get("run", "")) for s in _steps()[:suite])


def test_ci_keeps_its_image_and_lockfile_guarantees_through_the_target() -> None:
    """CI must build nothing and install only the locked dependencies."""
    step = _steps()[_index("Start PostgreSQL, RustFS and rclone, and apply migrations")]
    env = cast(Document, step["env"])
    assert env["TEST_STACK_UP_FLAGS"] == "--no-build"
    assert env["TEST_STACK_UV_FLAGS"] == "--locked --no-build"


def test_test_stack_resets_the_stack_before_it_starts_the_rest() -> None:
    """A stack-up's API, workers and persisted schedules would tick beside a local suite."""
    recipe = _test_stack_recipe().splitlines()
    assert "stack-reset" in recipe[0]
    assert "up -d --wait" in recipe[1]
    assert "rustfs-init" in recipe[2]
    assert "alembic upgrade head" in recipe[3]


def test_the_checkout_leaves_no_token_on_disk() -> None:
    """The suite runs PR code, which must not read the workflow token from the Git config."""
    checkouts = [s for s in _steps() if str(s.get("uses", "")).startswith("actions/checkout@")]
    assert checkouts
    for step in checkouts:
        assert cast(Document, step["with"]).get("persist-credentials") is False


def test_the_stack_is_removed_on_every_outcome() -> None:
    """A failed run must not leave containers or volumes behind on the runner."""
    teardown = _steps()[_index("Remove the stack")]
    assert teardown["if"] == "always()"
    assert "down --volumes" in cast(str, teardown["run"])


def test_the_api_health_check_probes_its_readiness_route() -> None:
    """`--wait` holds the API until /health/ready answers, not merely until it starts."""
    compose = cast(Document, yaml.safe_load(COMPOSE.read_text(encoding="utf-8")))
    api = cast(Document, cast(Document, compose["services"])["api"])
    probe = " ".join(cast(list[str], cast(Document, api["healthcheck"])["test"]))
    assert "http://127.0.0.1:8000/health/ready" in probe
    assert '"/health/ready"' in API_APP.read_text(encoding="utf-8")


def test_each_worker_must_log_ready_after_it_starts() -> None:
    """A worker has no health check, so the gate waits for the event its startup ends on."""
    assert _index("Start the application services") < _index("Wait for the workers to be ready")
    wait = cast(str, _steps()[_index("Wait for the workers to be ready")]["run"])
    assert "for service in worker-io worker-cpu" in wait
    assert "worker.ready" in wait
    assert 'logger.info("worker.ready"' in WORKER_MAIN.read_text(encoding="utf-8")


def test_the_gate_checks_the_workers_applied_their_schedules() -> None:
    """`worker.ready` follows a schedule apply that may have written nothing."""
    check = _index("Check the workers applied their schedules")
    assert _index("Wait for the workers to be ready") < check
    run = cast(str, _steps()[check]["run"])
    assert "declared_schedule_names(settings)" in run
    assert "schedules_for(profile, settings)" in run
    assert 'entry["schedule"]' in run
    assert "client.list_schedules()" in run
    assert "get_latest_application_version()" in run
    assert "dbos_app.APPLICATION_VERSION" in run
