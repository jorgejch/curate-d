"""The integration workflow's shape: its jobs, required check, path gate, shards and boot."""

import json
import os
import posixpath
import re
import subprocess
from pathlib import Path
from typing import cast

import pytest
import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "integration.yml"
COMPOSE = REPOSITORY_ROOT / "compose.yaml"
DOCKERFILE = REPOSITORY_ROOT / "docker" / "Dockerfile"
INTEGRATION_TESTS = REPOSITORY_ROOT / "tests" / "integration"
API_APP = REPOSITORY_ROOT / "src" / "curate_d" / "api" / "app.py"
WORKER_MAIN = REPOSITORY_ROOT / "src" / "curate_d" / "worker" / "main.py"
MAKEFILE = REPOSITORY_ROOT / "Makefile"
DURATIONS = REPOSITORY_ROOT / ".test_durations"

# The paths whose change selects the suite, reviewed against design.md's table.
EXPECTED_DECLARED_PATHS = [
    "src/**",
    "tests/integration/**",
    "tests/stubs/**",
    "tests/__init__.py",
    "conftest.py",
    "tests/conftest.py",
    "pytest.toml",
    ".pytest.toml",
    "pytest.ini",
    ".pytest.ini",
    "tox.ini",
    "setup.cfg",
    "tests/pytest.toml",
    "tests/.pytest.toml",
    "tests/pytest.ini",
    "tests/.pytest.ini",
    "tests/tox.ini",
    "tests/setup.cfg",
    "tests/pyproject.toml",
    "infrastructure/postgres/**",
    "infrastructure/rustfs/**",
    "infrastructure/staging-compatibility.md",
    "migrations/**",
    "alembic.ini",
    "compose.yaml",
    "compose.override.yml",
    "compose.override.yaml",
    "docker-compose.override.yml",
    "docker-compose.override.yaml",
    "docker/Dockerfile",
    "docker/Dockerfile.dockerignore",
    ".dockerignore",
    "Makefile",
    "pyproject.toml",
    "uv.lock",
    ".python-version",
    ".env",
    ".gitattributes",
    "tests/.gitattributes",
    "infrastructure/.gitattributes",
    "docker/.gitattributes",
    ".github/workflows/integration.yml",
    ".test_durations",
]

# Files a tool picks up implicitly once they exist, so they select the suite before they do:
# the ancestor conftest.py files pytest loads for tests/integration, the configuration files
# pytest prefers in tests/ or the root, every override file name `docker compose` merges into
# compose.yaml, the .env file Compose and `Settings` read, the .gitattributes files that reach
# a declared input, and BuildKit's Dockerfile-specific ignore file.
IMPLICIT_FILES = {
    "conftest.py",
    "tests/conftest.py",
    "pytest.toml",
    ".pytest.toml",
    "pytest.ini",
    ".pytest.ini",
    "tox.ini",
    "setup.cfg",
    "tests/pytest.toml",
    "tests/.pytest.toml",
    "tests/pytest.ini",
    "tests/.pytest.ini",
    "tests/tox.ini",
    "tests/setup.cfg",
    "tests/pyproject.toml",
    ".env",
    ".gitattributes",
    "tests/.gitattributes",
    "infrastructure/.gitattributes",
    "docker/.gitattributes",
    "compose.override.yml",
    "compose.override.yaml",
    "docker-compose.override.yml",
    "docker-compose.override.yaml",
    "docker/Dockerfile.dockerignore",
}

# GitHub lists at most this many of a pull request's files.
PULL_REQUEST_FILE_LIMIT = 3000

# hatchling reads these for package metadata only; nothing the suite asserts depends on them.
PACKAGING_FILES = {"README.md", "LICENSE", "NOTICE"}

# The job set: the plan, the shard matrix, the application boot and the required gate.
JOBS = {"plan", "shard", "boot", "integration"}

# A plan step's condition on the plan's own detection.
DETECTED = "steps.changes.outputs.suite == 'true'"
# A later job's or a gate step's condition on the plan's selection.
SELECTED = "needs.plan.outputs.suite == 'true'"
NOT_SELECTED = "needs.plan.outputs.suite == 'false'"

STACK_STEP = "Start PostgreSQL, RustFS and rclone, and apply migrations"
DETECTION_GUARD = "Refuse an undecided detection"
BUDGET_GUARD = "Refuse a CI-sized observation budget"
SHARD_COUNT_GUARD = "Refuse an invalid shard count"
BUDGET_VARIABLE = "CURATE_D_TEST_STAGING_LIFECYCLE_TIMEOUT_SECONDS"
SHARD_COUNT_DEFAULT = 4

# A repository path joined onto a test module's root: `_ROOT / "a" / "b"` reads `a/b`.
_ROOT_READ = re.compile(r"""\b(?:_ROOT|REPOSITORY_ROOT)((?:\s*/\s*(?:"[^"]*"|'[^']*'))+)""")

type Document = dict[str, object]

# PyYAML parses the bare `on:` key as the boolean True, per YAML 1.1.
type RawDocument = dict[object, object]


def _workflow() -> RawDocument:
    """Parse the integration workflow YAML."""
    return cast(RawDocument, yaml.safe_load(WORKFLOW.read_text(encoding="utf-8")))


def _jobs() -> dict[str, Document]:
    """Return the workflow's jobs by id."""
    return cast(dict[str, Document], _workflow()["jobs"])


def _plan() -> Document:
    """Return the plan job: detection, guards, collection and the shard split."""
    return _jobs()["plan"]


def _shard() -> Document:
    """Return the shard matrix job, which runs one group of the suite per runner."""
    return _jobs()["shard"]


def _boot() -> Document:
    """Return the job that boots the application image on its own stack."""
    return _jobs()["boot"]


def _gate() -> Document:
    """Return the required aggregate job, `Integration tests`."""
    return _jobs()["integration"]


def _steps(job: Document) -> list[Document]:
    """Return a job's steps."""
    return cast(list[Document], job["steps"])


def _index(job: Document, name: str) -> int:
    """Return the position of the step called `name` in `job`.

    Raises:
        AssertionError: The job has no step of that name.
    """
    for i, step in enumerate(_steps(job)):
        if step.get("name") == name:
            return i
    raise AssertionError(f"job {job.get('name')!r} has no step {name!r}")


def _step(job: Document, name: str) -> Document:
    """Return the step called `name` in `job`."""
    return _steps(job)[_index(job, name)]


def _needs(job: Document) -> list[str]:
    """Return a job's `needs` as a list, whether written as one id or several."""
    needs = job.get("needs", [])
    return [needs] if isinstance(needs, str) else cast(list[str], needs)


def _text(step: Document) -> str:
    """Return a step's script together with its environment, as one searchable string."""
    return f"{step.get('run', '')}\n{step.get('env', '')}"


def _fails_loudly(script: str) -> bool:
    """Return whether a step's script annotates an error and exits non-zero, in shell or Python."""
    return "::error::" in script and re.search(r"\bexit(?: |\()1\b", script) is not None


def _run_detection_guard(suite: str, changed_files: str | None) -> subprocess.CompletedProcess[str]:
    """Run the detection guard's script as GitHub Actions runs a `run` step.

    Args:
        suite: The `changes` step's `suite` output.
        changed_files: The pull request's changed-file count; `None` leaves it unset.
    """
    env = {"PATH": os.environ["PATH"], "SUITE": suite}
    if changed_files is not None:
        env["CHANGED_FILES"] = changed_files
    script = cast(str, _step(_plan(), DETECTION_GUARD)["run"])
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
        cwd=REPOSITORY_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _suite_command() -> str:
    """Return the shard's pytest command."""
    return cast(str, _step(_shard(), "Integration suite")["run"])


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


def _detection_index() -> int:
    """Return the position of the plan's change-detection step, `id: changes`."""
    return next(i for i, s in enumerate(_steps(_plan())) if s.get("id") == "changes")


def _declared_paths() -> list[str]:
    """Return the patterns of the detection step's `suite` filter."""
    step = _steps(_plan())[_detection_index()]
    filters = cast(str, cast(Document, step["with"])["filters"])
    return cast(list[str], cast(Document, yaml.safe_load(filters))["suite"])


def _covered(path: str, patterns: list[str]) -> bool:
    """Return whether a `dir/**` prefix or an exact-file pattern covers `path`."""
    for pattern in patterns:
        if pattern.endswith("/**"):
            directory = pattern.removesuffix("/**")
            if path == directory or path.startswith(f"{directory}/"):
                return True
        elif path == pattern:
            return True
    return False


def _compose_mount_sources() -> set[str]:
    """Return the repository paths `compose.yaml` bind-mounts into a service."""
    compose = cast(Document, yaml.safe_load(COMPOSE.read_text(encoding="utf-8")))
    sources: set[str] = set()
    for service in cast(Document, compose["services"]).values():
        for volume in cast(list[object], cast(Document, service).get("volumes", [])):
            if isinstance(volume, str):
                source = volume.split(":")[0]
            else:
                source = str(cast(Document, volume).get("source", ""))
            if source.startswith("./"):
                sources.add(posixpath.normpath(source))
    return sources


def _dockerfile_copy_sources() -> set[str]:
    """Return the build-context paths `docker/Dockerfile` copies, less the packaging files."""
    text = DOCKERFILE.read_text(encoding="utf-8").replace("\\\n", " ")
    sources: set[str] = set()
    for line in text.splitlines():
        words = line.split()
        if not words or words[0].upper() != "COPY":
            continue
        if any(word.startswith("--from=") for word in words):
            continue
        operands = [word for word in words[1:] if not word.startswith("--")]
        sources.update(posixpath.normpath(source) for source in operands[:-1])
    return sources - PACKAGING_FILES


def _integration_test_reads() -> set[str]:
    """Return the repository paths the integration tests join onto their repository root."""
    reads: set[str] = set()
    for module in sorted(INTEGRATION_TESTS.rglob("*.py")):
        for match in _ROOT_READ.finditer(module.read_text(encoding="utf-8")):
            parts = re.findall(r""""([^"]*)"|'([^']*)'""", match.group(1))
            reads.add(posixpath.normpath("/".join(a or b for a, b in parts)))
    return reads


def _assert_durations_cover_the_suite(path: Path) -> None:
    """Assert `path` is a non-empty JSON object of integration node ids to positive seconds."""
    name = path.name
    assert path.is_file(), f"{name} is missing: run `make test-durations`"
    durations = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(durations, dict), f"{name} is not a JSON object"
    assert durations, f"{name} records no test"
    for node_id, seconds in durations.items():
        assert node_id.startswith("tests/integration/"), f"{name} records {node_id!r}"
        # bool is an int to isinstance, and `true` is no duration.
        positive = (
            isinstance(seconds, int | float) and not isinstance(seconds, bool) and seconds > 0
        )
        assert positive, f"{name} records {seconds!r} for {node_id!r}"


def test_the_job_keeps_the_required_check_name() -> None:
    """The ruleset requires the check by this name; a rename leaves every PR waiting."""
    assert _gate()["name"] == "Integration tests"
    for job_id, job in _jobs().items():
        if job_id != "integration":
            assert job.get("name") != "Integration tests", job_id


def test_the_run_is_a_plan_its_shards_a_boot_and_the_gate() -> None:
    """A job outside the four would neither gate the merge nor be gated by the plan."""
    assert set(_jobs()) == JOBS
    outputs = cast(Document, _plan()["outputs"])
    assert {"suite", "splits", "groups", "collected"} <= set(outputs)


def test_shards_and_boot_run_only_when_the_plan_selects_the_suite() -> None:
    """Without the job-level condition, a pull request touching no input would start them."""
    for job in (_shard(), _boot()):
        assert _needs(job) == ["plan"], job["name"]
        assert job["if"] == SELECTED, job["name"]


def test_the_gate_runs_on_every_outcome_of_the_jobs_it_needs() -> None:
    """A skipped required check passes, so the gate must run whatever its dependencies did."""
    gate = _gate()
    assert sorted(_needs(gate)) == ["boot", "plan", "shard"]
    assert gate["if"] == "always()"


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


def test_each_shard_runs_its_group_of_the_plans_split() -> None:
    """Shards split the plan's count by recorded duration, serially, each with its own report."""
    shard = _shard()
    strategy = cast(Document, shard["strategy"])
    assert strategy["fail-fast"] is False
    assert strategy["matrix"] == {"group": "${{ fromJSON(needs.plan.outputs.groups) }}"}

    command = _suite_command()
    # `strategy.job-total` would double with a second matrix dimension and drop half the suite.
    assert "--splits ${{ needs.plan.outputs.splits }}" in command
    assert "--group ${{ matrix.group }}" in command
    assert "--splitting-algorithm least_duration" in command
    assert "--junitxml=integration-${{ matrix.group }}.xml" in command
    # One database, one serial process: no xdist inside a shard.
    assert re.search(r"(?:^|\s)(?:-n\b|--numprocesses|--dist\b)", command) is None

    suite = _index(shard, "Integration suite")
    upload = _steps(shard)[suite + 1]
    assert upload["name"] == "Upload the shard report"
    assert upload["if"] == "always()"
    assert str(upload["uses"]).startswith("actions/upload-artifact@")
    assert "integration-report-${{ matrix.group }}" in str(cast(Document, upload["with"])["name"])

    for step in _steps(shard):
        uses = str(step.get("uses", ""))
        assert not uses.startswith(("docker/build-push-action@", "docker/setup-buildx-action@")), (
            step["name"]
        )


def test_a_ci_sized_lifecycle_budget_fails_before_the_suite() -> None:
    """A budget short enough for a job would record unobserved reclamation as verified."""
    guard = _step(_plan(), BUDGET_GUARD)
    assert guard["if"] == f"env.{BUDGET_VARIABLE} != ''"
    assert "exit 1" in cast(str, guard["run"])
    assert _index(_plan(), BUDGET_GUARD) < _detection_index()
    # The guard reads the plan's environment; a budget set on a later job would evade it.
    assert BUDGET_VARIABLE not in cast(Document, _workflow().get("env", {}))
    for job_id, job in _jobs().items():
        assert BUDGET_VARIABLE not in cast(Document, job.get("env", {})), job_id


def test_migrations_run_before_the_application_services_start() -> None:
    """DBOS must not launch against a schema Alembic is still changing."""
    boot = _boot()
    assert _index(boot, STACK_STEP) < _index(boot, "Start the application services")
    assert _index(boot, "Build the application image") < _index(
        boot, "Start the application services"
    )


def test_the_boot_runs_no_test() -> None:
    """The booted workers' schedules would tick in a test process on the same database."""
    for step in _steps(_boot()):
        assert "pytest" not in cast(str, step.get("run", "")), step["name"]


def test_no_application_service_runs_beside_the_suite() -> None:
    """A worker's persisted schedules would tick in the test process, on the suite's version."""
    started = False
    for step in _steps(_shard()):
        assert step.get("name") != "Start the application services"
        for line in _expanded(cast(str, step.get("run", ""))).splitlines():
            if "compose" in line.lower() and " up " in line:
                started = True
                assert "postgres rustfs rclone" in line, step["name"]
                assert " api" not in line and "worker" not in line, step["name"]
    assert started


def test_ci_starts_the_suites_services_with_the_developers_target() -> None:
    """CI and a local run start the same services, so neither can drift from the other."""
    shard, boot = _shard(), _boot()
    for job, first_user in ((shard, "Integration suite"), (boot, "Start the application services")):
        stack = _index(job, STACK_STEP)
        assert stack < _index(job, first_user), job["name"]
        assert "make test-stack" in cast(str, _steps(job)[stack]["run"]), job["name"]


def test_ci_keeps_its_image_and_lockfile_guarantees_through_the_target() -> None:
    """CI must build nothing and install only the locked dependencies."""
    for job in (_shard(), _boot()):
        env = cast(Document, _step(job, STACK_STEP)["env"])
        assert env["TEST_STACK_UP_FLAGS"] == "--no-build", job["name"]
        assert env["TEST_STACK_UV_FLAGS"] == "--locked --no-build", job["name"]


def test_test_stack_resets_the_stack_before_it_starts_the_rest() -> None:
    """A stack-up's API, workers and persisted schedules would tick beside a local suite."""
    recipe = _test_stack_recipe().splitlines()
    assert "stack-reset" in recipe[0]
    assert "up -d --wait" in recipe[1]
    assert "rustfs-init" in recipe[2]
    assert "alembic upgrade head" in recipe[3]


def test_the_checkout_leaves_no_token_on_disk() -> None:
    """Every job runs or reads PR code, which must not find the workflow token in Git config."""
    checkouts = [
        (job_id, step)
        for job_id, job in _jobs().items()
        for step in _steps(job)
        if str(step.get("uses", "")).startswith("actions/checkout@")
    ]
    assert {job_id for job_id, _ in checkouts} >= {"plan", "shard", "boot"}
    for job_id, step in checkouts:
        assert cast(Document, step["with"]).get("persist-credentials") is False, job_id


def test_the_stack_is_removed_on_every_outcome() -> None:
    """A failed run must not leave containers or volumes behind on the runner."""
    for job in (_shard(), _boot()):
        teardown = _step(job, "Remove the stack")
        assert teardown["if"] == "always()", job["name"]
        assert "down --volumes" in cast(str, teardown["run"]), job["name"]


def test_the_api_health_check_probes_its_readiness_route() -> None:
    """`--wait` holds the API until /health/ready answers, not merely until it starts."""
    compose = cast(Document, yaml.safe_load(COMPOSE.read_text(encoding="utf-8")))
    api = cast(Document, cast(Document, compose["services"])["api"])
    probe = " ".join(cast(list[str], cast(Document, api["healthcheck"])["test"]))
    assert "http://127.0.0.1:8000/health/ready" in probe
    assert '"/health/ready"' in API_APP.read_text(encoding="utf-8")


def test_each_worker_must_log_ready_after_it_starts() -> None:
    """A worker has no health check, so the boot waits for the event its startup ends on."""
    boot = _boot()
    wait = "Wait for the workers to be ready"
    assert _index(boot, "Start the application services") < _index(boot, wait)
    script = cast(str, _step(boot, wait)["run"])
    assert "for service in worker-io worker-cpu" in script
    assert "worker.ready" in script
    assert 'logger.info("worker.ready"' in WORKER_MAIN.read_text(encoding="utf-8")


def test_the_gate_checks_the_workers_applied_their_schedules() -> None:
    """`worker.ready` follows a schedule apply that may have written nothing."""
    boot = _boot()
    check = _index(boot, "Check the workers applied their schedules")
    assert _index(boot, "Wait for the workers to be ready") < check
    step = _steps(boot)[check]
    assert step["run"] == "uv run --locked --no-build python -m curate_d.worker.schedule_check"
    assert "continue-on-error" not in step
    # The job is gated on the selection; a step condition could only skip the check.
    assert "if" not in step


def test_the_plan_detects_changes_before_installing_anything() -> None:
    """A detection after an install, or a plan step it does not gate, costs or opens the merge."""
    steps = _steps(_plan())
    detection = _detection_index()
    assert re.fullmatch(r"dorny/paths-filter@[0-9a-f]{40}", str(steps[detection]["uses"]))
    assert _index(_plan(), BUDGET_GUARD) < detection
    for step in steps[:detection]:
        assert str(step.get("uses", "actions/checkout@")).startswith("actions/checkout@"), step[
            "name"
        ]

    guard = steps[detection + 1]
    run = cast(str, guard["run"])
    assert "steps.changes.outputs.suite" in _text(guard), guard["name"]
    assert "::error::" in run and "exit 1" in run, guard["name"]
    assert "if" not in guard, guard["name"]
    # A truncated file list may have missed a declared path, whatever the detection answered.
    env = cast(Document, guard["env"])
    assert env["CHANGED_FILES"] == "${{ github.event.pull_request.changed_files }}"
    assert f"-ge {PULL_REQUEST_FILE_LIMIT}" in run, guard["name"]
    # The image copies the packaging files, which select nothing: a skip needs them present.
    for file in sorted(PACKAGING_FILES):
        assert file in run, guard["name"]

    # An unbounded count would start that many runners.
    count = steps[detection + 2]
    assert count["name"] == SHARD_COUNT_GUARD
    assert "if" not in count, count["name"]
    count_env = cast(Document, count["env"])
    assert any(
        f"vars.INTEGRATION_SHARDS || {SHARD_COUNT_DEFAULT}" in str(v) for v in count_env.values()
    )
    count_run = cast(str, count["run"])
    assert re.search(r"\b1\b", count_run) and re.search(r"\b32\b", count_run), count["name"]
    assert _fails_loudly(count_run), count["name"]

    later = steps[detection + 3 :]
    assert {"Install uv", "Install dependencies", "Collect the suite"} <= {s["name"] for s in later}
    for step in later:
        condition = cast(str, step.get("if", ""))
        # An `||` would let the step run whatever the detection reported.
        assert "||" not in condition, step["name"]
        assert DETECTED in condition, step["name"]


@pytest.mark.parametrize("suite", ["true", "false"])
@pytest.mark.parametrize("changed_files", [str(PULL_REQUEST_FILE_LIMIT), "4500", "", None])
def test_the_detection_guard_fails_a_possibly_truncated_list_whatever_it_selected(
    suite: str, changed_files: str | None
) -> None:
    """A file count at GitHub's listing limit, or a missing one, fails the plan on either result."""
    result = _run_detection_guard(suite, changed_files)
    assert result.returncode == 1, result.stdout + result.stderr
    assert f"changes {changed_files or ''} files" in result.stdout
    assert "Split the pull request." in result.stdout


@pytest.mark.parametrize("suite", ["", "maybe"])
def test_the_detection_guard_refuses_a_result_neither_true_nor_false(suite: str) -> None:
    """A detection result other than true or false fails the plan, naming the detection."""
    result = _run_detection_guard(suite, "10")
    assert result.returncode == 1, result.stdout + result.stderr
    assert f"step 'changes') reported suite='{suite}'" in result.stdout


@pytest.mark.parametrize("suite", ["true", "false"])
def test_the_detection_guard_passes_a_complete_list(suite: str) -> None:
    """A pull request under GitHub's listing limit passes the guard on either result."""
    result = _run_detection_guard(suite, str(PULL_REQUEST_FILE_LIMIT - 1))
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_gate_fails_closed() -> None:
    """The gate passes only on a successful plan and all-skipped or all-successful jobs."""
    steps = _steps(_gate())
    first = steps[0]
    assert first["name"] == "Refuse an incomplete run"
    assert "if" not in first
    text = _text(first)
    for expression in (
        "needs.plan.result",
        "needs.plan.outputs.suite",
        "needs.shard.result",
        "needs.boot.result",
    ):
        assert expression in text, expression
    run = cast(str, first["run"])
    assert _fails_loudly(run)
    assert "success" in run and "skipped" in run

    skips = []
    for step in steps[1:]:
        condition = cast(str, step.get("if", ""))
        assert "||" not in condition, step["name"]
        if NOT_SELECTED in condition:
            skips.append(step)
        else:
            assert SELECTED in condition, step["name"]
    assert [s["name"] for s in skips] == ["Suite not selected"]
    assert skips[0]["if"] == NOT_SELECTED
    assert "GITHUB_STEP_SUMMARY" in cast(str, skips[0]["run"])
    assert _step(_gate(), "Record staging evidence")["if"] == f"always() && {SELECTED}"


def test_the_gate_checks_the_shards_ran_the_whole_suite() -> None:
    """A shard that never reached pytest, or collected differently, fails the gate."""
    gate = _gate()
    download = _index(gate, "Download the shard reports")
    step = _steps(gate)[download]
    assert str(step["uses"]).startswith("actions/download-artifact@")
    options = cast(Document, step["with"])
    assert options["pattern"] == "integration-report-*"
    assert options["merge-multiple"] is True

    check_index = _index(gate, "Check the shards ran the whole suite")
    assert download < check_index
    check = _steps(gate)[check_index]
    # One report per group, and their test counts summing to the plan's collection.
    assert "needs.plan.outputs.splits" in _text(check)
    assert "needs.plan.outputs.collected" in _text(check)
    run = cast(str, check["run"])
    assert _fails_loudly(run)


def test_the_shard_count_defaults_agree() -> None:
    """A local `GROUP=<n>` rerun reproduces CI's split only with the same shard count."""
    makefile = re.search(r"^SPLITS \?= (\d+)$", MAKEFILE.read_text(encoding="utf-8"), re.M)
    assert makefile, "Makefile sets no `SPLITS ?= <n>`"
    env = cast(Document, _step(_plan(), SHARD_COUNT_GUARD)["env"])
    workflow = re.search(r"vars\.INTEGRATION_SHARDS \|\| (\d+)", " ".join(map(str, env.values())))
    assert workflow, "integration.yml sets no `vars.INTEGRATION_SHARDS || <n>`"
    makefile_count, workflow_count = int(makefile.group(1)), int(workflow.group(1))
    assert makefile_count == workflow_count, (
        f"Makefile SPLITS ?= {makefile_count}, integration.yml || {workflow_count}"
    )
    assert workflow_count == SHARD_COUNT_DEFAULT


def test_the_durations_file_covers_the_integration_suite() -> None:
    """Without recorded durations pytest-split falls back to equal counts and skews the shards."""
    _assert_durations_cover_the_suite(DURATIONS)


def test_declared_paths_are_prefixes_or_exact_files() -> None:
    """The coverage pin matches by prefix, so a pattern must be `dir/**` or an exact file."""
    for pattern in _declared_paths():
        path = pattern.removesuffix("/**")
        assert path and not set("*?[]{}!") & set(path), pattern
        assert not path.startswith(("/", "./")) and not path.endswith("/"), pattern
        if pattern.endswith("/**"):
            assert (REPOSITORY_ROOT / path).is_dir(), pattern
        elif pattern in IMPLICIT_FILES:
            # Untracked and local, so it may be absent, or not a regular file: a 1Password
            # mount makes .env a named pipe. Only a directory would break prefix matching.
            assert not (REPOSITORY_ROOT / path).is_dir(), pattern
        else:
            assert (REPOSITORY_ROOT / path).is_file(), pattern


def test_the_declared_paths_are_the_expected_set() -> None:
    """A widened or narrowed filter shows as a diff against the reviewed list."""
    assert sorted(_declared_paths()) == sorted(EXPECTED_DECLARED_PATHS)


def test_every_stack_mount_copy_and_test_read_is_declared() -> None:
    """A path the stack mounts, the image copies or a test reads must select the suite."""
    declared = _declared_paths()
    readers = {
        "compose.yaml bind mount": _compose_mount_sources(),
        "docker/Dockerfile COPY": _dockerfile_copy_sources(),
        "integration test read": _integration_test_reads(),
    }
    for reader, paths in readers.items():
        assert paths, f"found no {reader}"
        for path in sorted(paths):
            assert _covered(path, declared), f"{reader} {path} is under no declared path"
