"""The parallel shard runner: its plan, its executor and its interrupt handling."""

import io
import re
import shlex
import signal
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from types import FrameType
from typing import cast

import pytest
import yaml

from tests import parallel_integration as runner
from tests.parallel_integration import (
    ProcessRunner,
    ShardExecutor,
    ShardPlan,
    parse_splits,
    shard_plan,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
COMPOSE = REPOSITORY_ROOT / "compose.yaml"
LIFECYCLE_COMPOSE = REPOSITORY_ROOT / "compose.lifecycle.yaml"
WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "integration.yml"
MAKEFILE = REPOSITORY_ROOT / "Makefile"

RUN_DIR = Path(".integration-runs") / "20261031T000000Z"
ALL_SHARDS = range(runner.MIN_SPLITS, runner.MAX_SPLITS + 1)

_PORT_DEFAULT = re.compile(r"\$\{(CURATE_D_[A-Z_]+_PORT):-(\d+)\}")

type Document = dict[str, object]


def _plans(splits: int) -> list[ShardPlan]:
    """Return every shard of a run split `splits` ways."""
    return [shard_plan(index, splits, RUN_DIR) for index in range(1, splits + 1)]


def _published_port_defaults() -> set[int]:
    """Return every default host port the default and lifecycle Compose files publish."""
    text = COMPOSE.read_text(encoding="utf-8") + LIFECYCLE_COMPOSE.read_text(encoding="utf-8")
    return {int(port) for _, port in _PORT_DEFAULT.findall(text)}


def _port_variables(service: str) -> set[str]:
    """Return the variables that set a compose.yaml service's published host ports."""
    services = cast(Document, yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"])
    ports = cast(list[str], cast(Document, services[service]).get("ports", []))
    return {name for entry in ports for name, _ in _PORT_DEFAULT.findall(entry)}


def _ci_shard_command(index: int, splits: int, junit: Path) -> list[str]:
    """Return integration.yml's shard command with its expressions set for one shard."""
    workflow = cast(Document, yaml.safe_load(WORKFLOW.read_text(encoding="utf-8")))
    shard = cast(Document, cast(Document, workflow["jobs"])["shard"])
    steps = cast(list[Document], shard["steps"])
    command = next(
        cast(str, step["run"]) for step in steps if step.get("name") == "Integration suite"
    )
    command = (
        command.replace("integration-${{ matrix.group }}.xml", str(junit))
        .replace("${{ needs.plan.outputs.splits }}", str(splits))
        .replace("${{ matrix.group }}", str(index))
    )
    assert "${{" not in command, command
    return shlex.split(command)


def _test_stack_recipe() -> list[str]:
    """Return the `test-stack` target's recipe lines from the Makefile."""
    lines = MAKEFILE.read_text(encoding="utf-8").splitlines()
    start = lines.index(next(line for line in lines if line.startswith("test-stack:")))
    recipe = []
    for line in lines[start + 1 :]:
        if not line.startswith("\t"):
            break
        recipe.append(line.strip())
    return recipe


# --- SPLITS -------------------------------------------------------------------------------


@pytest.mark.parametrize(("value", "splits"), [("1", 1), ("4", 4), ("32", 32), ("04", 4)])
def test_a_shard_count_from_1_to_32_is_accepted(value: str, splits: int) -> None:
    """Every count integration.yml accepts parses, leading zeros included as CI reads them."""
    assert parse_splits(value) == splits


@pytest.mark.parametrize("value", ["0", "abc", "2.5", "33", "", "-1", "+4", " 4", "4 "])
def test_any_other_shard_count_is_refused_naming_splits_and_the_range(value: str) -> None:
    """A count outside 1 to 32, or not a plain integer, names SPLITS and the accepted range."""
    with pytest.raises(ValueError, match=r"SPLITS is .*from 1 to 32"):
        parse_splits(value)


def test_the_runners_range_is_the_one_integration_yml_accepts() -> None:
    """The runner refuses exactly what CI's shard-count guard refuses."""
    assert '-ge 1 ] && [ "$INTEGRATION_SHARDS" -le 32 ]' in WORKFLOW.read_text(encoding="utf-8")
    assert (runner.MIN_SPLITS, runner.MAX_SPLITS) == (1, 32)


# --- Projects and ports -------------------------------------------------------------------


def test_each_shard_has_its_own_project_and_output_directory() -> None:
    """Shard i is Compose project curate-d-it-<i>, writing under shard-<i>."""
    plan = shard_plan(3, 4, RUN_DIR)
    assert plan.project == "curate-d-it-3"
    assert plan.directory == RUN_DIR / "shard-3"
    assert (plan.stack_log.name, plan.pytest_log.name, plan.junit.name) == (
        "stack.log",
        "pytest.log",
        "junit.xml",
    )


def test_no_shard_port_collides_with_another_stack_or_shard() -> None:
    """For shards 1 to 32, no port is the default stack's, the lifecycle stack's or a shard's."""
    reserved = _published_port_defaults()
    # The default stack's database, RustFS API and console, rclone and API, and lifecycle's RustFS.
    assert {5433, 9100, 9101, 5572, 8000, 9110} <= reserved
    taken: list[int] = []
    for index in ALL_SHARDS:
        ports = runner.shard_ports(index)
        taken.extend((ports.postgres, ports.rustfs, ports.rustfs_console, ports.rclone))
    assert not set(taken) & reserved
    assert len(taken) == len(set(taken))
    assert all(1024 < port < 65536 for port in taken)


def test_a_shard_sets_every_port_its_services_publish() -> None:
    """The shard sets each port variable of postgres, rustfs and rclone, to its own block."""
    published = set().union(*(_port_variables(service) for service in runner.STACK_SERVICES))
    plan = shard_plan(2, 4, RUN_DIR)
    set_ports = {name: value for name, value in plan.environment.items() if name.endswith("_PORT")}
    assert set(set_ports) == published
    assert set_ports == {
        "CURATE_D_POSTGRES_PORT": str(plan.ports.postgres),
        "CURATE_D_RUSTFS_PORT": str(plan.ports.rustfs),
        "CURATE_D_RUSTFS_CONSOLE_PORT": str(plan.ports.rustfs_console),
        "CURATE_D_RCLONE_PORT": str(plan.ports.rclone),
    }


# --- Environment --------------------------------------------------------------------------


def test_the_suite_finds_the_shards_own_stack() -> None:
    """The DSN, RustFS endpoint and rclone URL point at the shard's ports."""
    plan = shard_plan(1, 4, RUN_DIR)
    assert plan.environment["CURATE_D_LEDGER_DATABASE_URL"].endswith(
        f"@localhost:{plan.ports.postgres}/curate_d"
    )
    assert (
        plan.environment["CURATE_D_TEST_RUSTFS_ENDPOINT"] == f"http://127.0.0.1:{plan.ports.rustfs}"
    )
    assert plan.environment["CURATE_D_TEST_RCLONE_URL"] == f"http://127.0.0.1:{plan.ports.rclone}"


def test_the_dsn_uses_compose_yamls_development_database() -> None:
    """The database, user and password are the ones the postgres service creates."""
    services = cast(Document, yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"])
    environment = cast(Document, cast(Document, services["postgres"])["environment"])
    assert environment["POSTGRES_DB"] == runner.DATABASE
    assert environment["POSTGRES_USER"] == runner.DATABASE_USER
    assert environment["POSTGRES_PASSWORD"] == runner.DATABASE_PASSWORD


def test_the_shards_settings_override_the_developers() -> None:
    """A DSN or port the developer exported for the default stack does not reach a shard."""
    plan = shard_plan(1, 4, RUN_DIR)
    env = plan.env({"CURATE_D_POSTGRES_PORT": "5433", "PATH": "/bin", "HOME": "/home/dev"})
    assert env["CURATE_D_POSTGRES_PORT"] == str(plan.ports.postgres)
    assert env["PATH"] == "/bin"


def test_the_encryption_key_falls_back_to_cis_public_dummy() -> None:
    """Without an exported key the shard gets the key integration.yml and the Makefile use."""
    env = shard_plan(1, 4, RUN_DIR).env({})
    workflow = cast(Document, yaml.safe_load(WORKFLOW.read_text(encoding="utf-8")))
    ci_key = cast(Document, workflow["env"])[runner.ENCRYPTION_KEY_VARIABLE]
    assert env[runner.ENCRYPTION_KEY_VARIABLE] == ci_key == runner.DUMMY_ENCRYPTION_KEY
    makefile = MAKEFILE.read_text(encoding="utf-8")
    assert f"{runner.ENCRYPTION_KEY_VARIABLE}:-{runner.DUMMY_ENCRYPTION_KEY}" in makefile


def test_an_exported_encryption_key_is_kept() -> None:
    """A developer's own key is not replaced, as TEST_STACK_COMPOSE keeps it."""
    env = shard_plan(1, 4, RUN_DIR).env({runner.ENCRYPTION_KEY_VARIABLE: "developer-key"})
    assert env[runner.ENCRYPTION_KEY_VARIABLE] == "developer-key"


def test_an_empty_exported_encryption_key_falls_back_to_the_dummy() -> None:
    """An empty exported key counts as unset, like `${VAR:-default}` in Compose."""
    env = shard_plan(1, 4, RUN_DIR).env({runner.ENCRYPTION_KEY_VARIABLE: ""})
    assert env[runner.ENCRYPTION_KEY_VARIABLE] == runner.DUMMY_ENCRYPTION_KEY


# --- Steps --------------------------------------------------------------------------------


def test_a_shard_resets_starts_provisions_migrates_then_tests() -> None:
    """The five steps run in order; teardown and logs are separate."""
    plan = shard_plan(2, 4, RUN_DIR)
    compose = ("docker", "compose", "-p", "curate-d-it-2")
    assert [step.name for step in plan.steps] == ["reset", "up", "rustfs-init", "migrate", "pytest"]
    assert plan.steps[0].argv == (*compose, "down", "-v")
    assert plan.steps[1].argv == (*compose, "up", "-d", "--wait", "postgres", "rustfs", "rclone")
    assert plan.steps[2].argv == (*compose, "run", "--rm", "rustfs-init")
    assert plan.teardown.argv == (*compose, "down", "-v")
    assert plan.logs.argv[:5] == (*compose, "logs")
    assert [step.log for step in plan.steps] == [runner.Log.STACK] * 4 + [runner.Log.PYTEST]


def test_a_shard_starts_and_migrates_what_test_stack_does() -> None:
    """Up, provisioning and migration are `make test-stack`'s, on the shard's project."""
    recipe = _test_stack_recipe()
    plan = shard_plan(1, 4, RUN_DIR)
    up = next(line for line in recipe if " up -d --wait " in line)
    assert up.endswith(" ".join(plan.steps[1].argv[-3:]))
    assert any(line.endswith("run --rm rustfs-init") for line in recipe)
    assert any(line.endswith("alembic upgrade head") for line in recipe)
    migrate = plan.steps[3].argv
    assert (Path(migrate[0]).name, *migrate[1:]) == ("alembic", "upgrade", "head")


def test_every_compose_command_names_the_shards_project_never_the_default() -> None:
    """No command of any shard can reach the default curate-d project."""
    for plan in _plans(runner.MAX_SPLITS):
        for step in (*plan.steps, plan.logs, plan.teardown):
            if step.argv[:2] == ("docker", "compose"):
                assert step.argv[2:4] == ("-p", plan.project), step
                assert plan.project != "curate-d"


@pytest.mark.parametrize(("index", "splits"), [(1, 1), (2, 4), (4, 4), (32, 32)])
def test_the_pytest_step_is_cis_shard_command(index: int, splits: int) -> None:
    """The pytest arguments equal integration.yml's shard step, set for the shard."""
    plan = shard_plan(index, splits, RUN_DIR)
    ci = _ci_shard_command(index, splits, plan.junit)
    assert ci[:4] == ["uv", "run", "--locked", "--no-build"]
    pytest_step = plan.steps[-1].argv
    assert [Path(pytest_step[0]).name, *pytest_step[1:]] == ci[4:]


def test_pytest_and_alembic_come_from_the_runners_environment() -> None:
    """Steps use the venv `uv run` gave the runner, not a nested `uv run` per shard."""
    plan = shard_plan(1, 4, RUN_DIR)
    for step in plan.steps[3:]:
        assert Path(step.argv[0]).parent == Path(sys.executable).parent


# --- Executor -----------------------------------------------------------------------------

# How long a test waits on another thread before it fails instead of hanging.
_WAIT = 5.0

type Outcome = int | OSError
type Hook = Callable[[int, str], int | None]


class FakeRunner:
    """Records each shard command and answers with a scripted exit code.

    A command is named by its shard and step, `logs` or `teardown`. `hook` runs first and
    may block or answer for the command itself.
    """

    def __init__(
        self,
        plans: Sequence[ShardPlan],
        outcomes: Mapping[tuple[int, str], Outcome] | None = None,
        hook: Hook | None = None,
    ) -> None:
        # Migrate and pytest share their command line across shards, so the shard is
        # recognized by the port its environment carries.
        self._shards = {str(plan.ports.postgres): plan.index for plan in plans}
        self._names: dict[tuple[int, tuple[str, ...], bool], str] = {}
        for plan in plans:
            for step in plan.steps:
                self._names[(plan.index, step.argv, False)] = step.name
            self._names[(plan.index, plan.logs.argv, True)] = "logs"
            self._names[(plan.index, plan.teardown.argv, True)] = "teardown"
        self._plans = {plan.index: plan for plan in plans}
        self._outcomes = dict(outcomes or {})
        self._hook = hook
        self._lock = threading.Lock()
        self.calls: list[tuple[int, str]] = []
        self.ports: dict[int, str] = {}
        self.logs: dict[tuple[int, str], Path] = {}
        self.terminations = 0
        self.terminated = threading.Event()

    def run(
        self, argv: Sequence[str], *, env: Mapping[str, str], log: Path, always: bool = False
    ) -> int:
        """Record the command, then return its scripted exit code or raise its error."""
        port = env["CURATE_D_POSTGRES_PORT"]
        index = self._shards[port]
        name = self._names[(index, tuple(argv), always)]
        with self._lock:
            self.calls.append((index, name))
            self.ports[index] = port
            self.logs[(index, name)] = log
        if self._hook is not None:
            answer = self._hook(index, name)
            if answer is not None:
                return answer
        outcome = self._outcomes.get((index, name), 0)
        if isinstance(outcome, OSError):
            raise outcome
        return outcome

    def terminate(self) -> None:
        """Record the termination and release every command waiting on it."""
        with self._lock:
            self.terminations += 1
        self.terminated.set()

    def of(self, index: int) -> list[str]:
        """Return the commands shard `index` ran, in order."""
        return [name for shard, name in self.calls if shard == index]


FULL_RUN = ["reset", "up", "rustfs-init", "migrate", "pytest", "teardown"]


def _execute(
    splits: int,
    outcomes: Mapping[tuple[int, str], Outcome] | None = None,
    hook: Hook | None = None,
) -> tuple[int, FakeRunner, str]:
    """Run a fake run of `splits` shards; return its exit code, the fake and the output."""
    plans = _plans(splits)
    fake = FakeRunner(plans, outcomes, hook)
    out = io.StringIO()
    code = ShardExecutor(plans, fake, base_env={}, out=out).run()
    return code, fake, out.getvalue()


def test_every_shard_passing_exits_0_and_removes_every_stack() -> None:
    """All shards run all steps, each is torn down once, and the run exits 0."""
    code, fake, output = _execute(4)
    assert code == 0
    for index in range(1, 5):
        assert fake.of(index) == FULL_RUN
        assert f"shard {index}/4 passed" in output
    assert "Failed shards" not in output


def test_a_failed_pytest_lets_the_other_shards_finish_and_names_its_reports() -> None:
    """Shard 2 fails its tests; 1, 3 and 4 still finish; the summary names 2's reports."""
    code, fake, output = _execute(4, {(2, "pytest"): 1})
    assert code == 1
    for index in (1, 3, 4):
        assert fake.of(index) == FULL_RUN
    assert fake.of(2) == ["reset", "up", "rustfs-init", "migrate", "pytest", "logs", "teardown"]
    assert "shard 2/4 failed at pytest" in output
    shard_2 = RUN_DIR / "shard-2"
    assert f"{shard_2 / 'pytest.log'}, {shard_2 / 'junit.xml'}" in output
    assert "Failed shards: 2." in output
    assert "make test-integration GROUP=<n> SPLITS=4" in output


def test_a_failed_up_skips_migrations_and_tests_and_names_the_step() -> None:
    """A stack that does not start stops its shard before migrate, naming up and stack.log."""
    code, fake, output = _execute(4, {(3, "up"): 1})
    assert code == 1
    assert fake.of(3) == ["reset", "up", "logs", "teardown"]
    for index in (1, 2, 4):
        assert fake.of(index) == FULL_RUN
    assert "shard 3/4 failed at up" in output
    row = next(line for line in output.splitlines() if line.strip().startswith("3 "))
    assert "FAILED" in row
    assert " up " in row
    assert str(RUN_DIR / "shard-3" / "stack.log") in row


def test_a_failed_teardown_fails_the_run_and_says_how_to_clean_up() -> None:
    """A stack left behind fails the run and names the command that removes it."""
    code, _, output = _execute(2, {(2, "teardown"): 1})
    assert code == 1
    assert "docker compose -p curate-d-it-2 down -v" in output
    assert "Failed shards: 2." in output


def test_a_command_that_cannot_start_fails_its_shard_with_the_reason() -> None:
    """Docker missing fails the shard at its first step and says why; others are unaffected."""
    missing = FileNotFoundError(2, "No such file or directory", "docker")
    outcomes: dict[tuple[int, str], Outcome] = {
        (1, "reset"): missing,
        (1, "logs"): missing,
        (1, "teardown"): missing,
    }
    code, fake, output = _execute(2, outcomes)
    assert code == 1
    # The logs failing to start does not stop the teardown from being tried.
    assert fake.of(1) == ["reset", "logs", "teardown"]
    assert fake.of(2) == FULL_RUN
    assert "reset could not start" in output
    assert "docker compose -p curate-d-it-1 down -v" in output


def test_every_shard_runs_at_once() -> None:
    """No shard waits for another: all four reach `up` together."""
    barrier = threading.Barrier(4, timeout=_WAIT)

    def meet(index: int, name: str) -> None:
        """Wait at `up` until every shard is there."""
        if name == "up":
            barrier.wait()

    code, _, _ = _execute(4, hook=meet)
    assert code == 0


def test_each_shard_runs_with_its_own_environment_and_logs() -> None:
    """Every command of a shard gets the shard's ports and writes into its directory."""
    _, fake, _ = _execute(3)
    for plan in _plans(3):
        assert fake.ports[plan.index] == str(plan.ports.postgres)
        assert fake.logs[(plan.index, "pytest")] == plan.pytest_log
        assert fake.logs[(plan.index, "up")] == plan.stack_log
        assert fake.logs[(plan.index, "teardown")] == plan.stack_log


@pytest.mark.parametrize(("seconds", "shown"), [(0.4, "0m00s"), (187, "3m07s"), (1038, "17m18s")])
def test_durations_read_as_minutes_and_seconds(seconds: float, shown: str) -> None:
    """A shard's time reads like the serial run's 17m18s."""
    assert runner._duration(seconds) == shown


# --- Interrupt ----------------------------------------------------------------------------


class Interruption:
    """A fake run whose shards block in pytest until a simulated signal terminates them."""

    def __init__(self, splits: int, blocking: Mapping[int, str] | None = None) -> None:
        self.plans = _plans(splits)
        # The step each shard blocks in, until terminated; pytest unless named here.
        self._blocking = {plan.index: "pytest" for plan in self.plans} | dict(blocking or {})
        self._waiting = threading.Semaphore(0)
        self.fake = FakeRunner(self.plans, hook=self._block)
        self.out = io.StringIO()
        self.executor = ShardExecutor(self.plans, self.fake, base_env={}, out=self.out)
        self.code: int | None = None
        self._thread = threading.Thread(target=self._run)

    def _block(self, index: int, name: str) -> int | None:
        """Hold the shard's blocking step until terminated: pytest dies, other steps finish."""
        if name != self._blocking[index]:
            return None
        self._waiting.release()
        assert self.fake.terminated.wait(_WAIT)
        return -signal.SIGTERM if name == "pytest" else 0

    def _run(self) -> None:
        """Run the executor, as the main thread would."""
        self.code = self.executor.run()

    def start(self) -> None:
        """Start the run and return once every shard is blocked."""
        self._thread.start()
        for _ in self.plans:
            assert self._waiting.acquire(timeout=_WAIT)

    def finish(self) -> int:
        """Wait for the run to end and return its exit code."""
        self._thread.join(_WAIT)
        assert not self._thread.is_alive()
        assert self.code is not None
        return self.code


@pytest.fixture
def installed(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, object]]:
    """Record the handlers the signal handler installs instead of installing them."""
    calls: list[tuple[int, object]] = []

    def install(signum: int, handler: object) -> None:
        """Record one installation."""
        calls.append((signum, handler))

    monkeypatch.setattr("tests.parallel_integration.signal.signal", install)
    return calls


def test_an_interrupt_stops_the_tests_and_removes_every_started_stack(
    installed: list[tuple[int, object]],
) -> None:
    """SIGINT while shards run pytest terminates them, tears every stack down and exits 130."""
    run = Interruption(4)
    run.start()
    handler: Callable[[int, FrameType | None], None] = runner.signal_handler(run.executor)
    handler(signal.SIGINT, None)
    assert run.finish() == 130
    assert run.fake.terminations == 1
    for index in range(1, 5):
        assert run.fake.of(index) == ["reset", "up", "rustfs-init", "migrate", "pytest", "teardown"]
    assert "SIGINT: stopping the shards and removing their stacks" in run.out.getvalue()
    assert "shard 1/4 interrupted" in run.out.getvalue()
    assert "Failed shards" not in run.out.getvalue()
    assert (signal.SIGINT, signal.SIG_IGN) in installed
    assert (signal.SIGTERM, signal.SIG_IGN) in installed


def test_a_second_interrupt_during_teardown_is_ignored(
    installed: list[tuple[int, object]],
) -> None:
    """Only the first signal acts: the second neither terminates again nor changes the code."""
    run = Interruption(2)
    run.start()
    handler = runner.signal_handler(run.executor)
    handler(signal.SIGINT, None)
    handler(signal.SIGTERM, None)
    assert run.finish() == 130
    assert run.fake.terminations == 1


def test_sigterm_exits_143(installed: list[tuple[int, object]]) -> None:
    """A SIGTERM stops the run the same way and exits 128 + 15."""
    run = Interruption(2)
    run.start()
    runner.signal_handler(run.executor)(signal.SIGTERM, None)
    assert run.finish() == 143


def test_a_shard_starts_no_step_after_an_interrupt() -> None:
    """A step that completes as the signal lands is the shard's last before teardown."""
    run = Interruption(2, blocking={2: "migrate"})
    run.start()
    run.executor.interrupt(signal.SIGINT)
    assert run.finish() == 130
    assert run.fake.of(2) == ["reset", "up", "rustfs-init", "migrate", "teardown"]


# --- Process runner -----------------------------------------------------------------------


def _python(code: str) -> list[str]:
    """Return a command that runs `code` in a fresh interpreter."""
    return [sys.executable, "-c", code]


def test_the_process_runner_logs_the_command_and_its_output(tmp_path: Path) -> None:
    """Output is appended to the log under the command line, and the exit code returned."""
    log = tmp_path / "shard-1" / "stack.log"
    code = ProcessRunner().run(
        _python("import sys; print('provisioned'); sys.exit(3)"), env={}, log=log
    )
    assert code == 3
    text = log.read_text()
    assert text.startswith(f"$ {sys.executable} -c ")
    assert "provisioned" in text


def test_terminate_stops_a_running_command_and_refuses_new_ones(tmp_path: Path) -> None:
    """A running child dies on terminate; later steps don't start, but teardown still does."""
    process_runner = ProcessRunner(grace_seconds=_WAIT)
    log = tmp_path / "pytest.log"
    codes: list[int] = []
    started = time.monotonic()
    command = _python("import time; print('ready', flush=True); time.sleep(60)")
    thread = threading.Thread(
        target=lambda: codes.append(process_runner.run(command, env={}, log=log))
    )
    thread.start()
    deadline = time.monotonic() + _WAIT
    while "ready" not in (log.read_text() if log.exists() else "") and time.monotonic() < deadline:
        time.sleep(0.02)
    process_runner.terminate()
    thread.join(_WAIT)
    assert codes == [-signal.SIGTERM]
    assert time.monotonic() - started < _WAIT

    assert process_runner.run(_python("pass"), env={}, log=log) == runner.NOT_STARTED
    assert "not started: the run was interrupted" in log.read_text()
    assert process_runner.run(_python("pass"), env={}, log=log, always=True) == 0


def test_terminate_lets_a_running_teardown_finish(tmp_path: Path) -> None:
    """A signal that lands while one shard tears down leaves that teardown to finish."""
    process_runner = ProcessRunner(grace_seconds=_WAIT)
    log = tmp_path / "stack.log"
    release = tmp_path / "release"
    codes: list[int] = []
    command = _python(
        "import pathlib, sys, time\n"
        "print('ready', flush=True)\n"
        f"while not pathlib.Path({str(release)!r}).exists(): time.sleep(0.02)\n"
        "sys.exit(0)"
    )
    thread = threading.Thread(
        target=lambda: codes.append(process_runner.run(command, env={}, log=log, always=True))
    )
    thread.start()
    deadline = time.monotonic() + _WAIT
    while "ready" not in (log.read_text() if log.exists() else "") and time.monotonic() < deadline:
        time.sleep(0.02)
    process_runner.terminate()
    release.touch()
    thread.join(_WAIT)
    assert codes == [0]


# --- Entry point --------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["0", "abc", "2.5", "33"])
def test_an_invalid_splits_exits_2_before_anything_starts(
    value: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The entry point names SPLITS and its range, and never builds a runner."""

    def refuse(*args: object, **kwargs: object) -> None:
        """Fail the test: nothing may start for an invalid count."""
        raise AssertionError("a stack would start")

    monkeypatch.setattr(runner, "ProcessRunner", refuse)
    monkeypatch.setattr("tests.parallel_integration.os.chdir", refuse)
    with pytest.raises(SystemExit) as exit_info:
        runner.main(["--splits", value])
    assert exit_info.value.code == 2
    assert "SPLITS" in capsys.readouterr().err
