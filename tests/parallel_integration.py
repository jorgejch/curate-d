"""Run the integration suite as CI's shards, each against its own Compose stack, at once.

`make test-integration-parallel` runs it::

    uv run --locked python -m tests.parallel_integration --splits 4

Shard *i* runs in Compose project `curate-d-it-<i>` on its own loopback ports, which is the
isolation CI gets from one runner per shard. The default `curate-d` project is never named,
so a `make stack-up` stack keeps running. Inside a shard the tests stay serial: they share
one database's advisory locks and DBOS's latest application version, and two processes on
one database would interfere. A failed shard reproduces with
`make test-stack && make test-integration GROUP=<i> SPLITS=<n>`, so no stack is kept.

Standard library only, and nothing from `curate_d`: the runner must work, and tear its
stacks down, when the application does not import.
"""

import argparse
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from types import FrameType
from typing import Final, Protocol, TextIO

REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[1]
RUNS_DIRECTORY: Final = Path(".integration-runs")

# integration.yml's range for INTEGRATION_SHARDS, so every count CI can run reproduces here.
MIN_SPLITS: Final = 1
MAX_SPLITS: Final = 32

PROJECT_PREFIX: Final = "curate-d-it-"
# `make test-stack`'s services: only what the suite talks to, never the API or workers.
STACK_SERVICES: Final = ("postgres", "rustfs", "rclone")

ENCRYPTION_KEY_VARIABLE: Final = "CURATE_D_CREDENTIAL_ENCRYPTION_KEY"
# integration.yml's public dummy Fernet key, which TEST_STACK_COMPOSE also falls back to.
# Compose interpolates the key for every service; none of the shard's services reads it.
DUMMY_ENCRYPTION_KEY: Final = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="

# compose.yaml's development database, user and password for the postgres service: public
# values on a loopback-only, disposable stack, the same as the default stack's.
DATABASE: Final = "curate_d"
DATABASE_USER: Final = DATABASE
DATABASE_PASSWORD: Final = DATABASE

# A command the runner refused because the run was interrupted before it started.
NOT_STARTED: Final = -1

# How long a terminated child gets to exit before it is killed.
TERMINATE_GRACE_SECONDS: Final = 10.0

_SPLITS = re.compile(r"[0-9]+")


def parse_splits(value: str) -> int:
    """Return the shard count that `SPLITS` names.

    Raises:
        ValueError: `value` is not a decimal integer from 1 to 32.
    """
    if _SPLITS.fullmatch(value) and MIN_SPLITS <= int(value) <= MAX_SPLITS:
        return int(value)
    raise ValueError(
        f"SPLITS is {value!r}; it must be an integer from {MIN_SPLITS} to {MAX_SPLITS}, "
        "the range integration.yml accepts for INTEGRATION_SHARDS"
    )


@dataclass(frozen=True)
class Ports:
    """The host ports one shard's stack publishes, all on 127.0.0.1."""

    postgres: int
    rustfs: int
    rustfs_console: int
    rclone: int


def shard_ports(index: int) -> Ports:
    """Return shard `index`'s port block.

    For every index from 1 to 32 the block is clear of the default stack (5433, 9100, 9101,
    5572, 8000), the lifecycle stack (9110) and every other shard. Fixed rather than chosen
    by the OS, so a shard's addresses in its log are predictable.
    """
    return Ports(
        postgres=5440 + index,
        rustfs=9120 + 2 * index,
        rustfs_console=9121 + 2 * index,
        rclone=5580 + index,
    )


class Log(StrEnum):
    """The file in a shard's output directory that a command writes to."""

    STACK = "stack.log"
    PYTEST = "pytest.log"


@dataclass(frozen=True)
class Step:
    """One command of a shard, and the log its output goes to."""

    name: str
    argv: tuple[str, ...]
    log: Log


@dataclass(frozen=True)
class ShardPlan:
    """Everything one shard runs: its project, ports, environment, steps and teardown.

    `environment` holds only the shard's own settings; `env` lays them over the runner's.
    """

    index: int
    splits: int
    project: str
    ports: Ports
    directory: Path
    environment: Mapping[str, str] = field(hash=False)
    steps: tuple[Step, ...]
    logs: Step
    teardown: Step

    @property
    def stack_log(self) -> Path:
        """Return the path of the log the Compose steps, the migrations and the logs go to."""
        return self.directory / Log.STACK

    @property
    def pytest_log(self) -> Path:
        """Return the path of the log pytest writes to."""
        return self.directory / Log.PYTEST

    @property
    def junit(self) -> Path:
        """Return the path of the shard's JUnit report."""
        return self.directory / "junit.xml"

    def env(self, base: Mapping[str, str]) -> dict[str, str]:
        """Return the shard's process environment: `base` with the shard's settings over it.

        An empty exported key counts as unset; a non-empty key is kept, as TEST_STACK_COMPOSE
        keeps it.
        """
        key = base.get(ENCRYPTION_KEY_VARIABLE) or DUMMY_ENCRYPTION_KEY
        return {**base, ENCRYPTION_KEY_VARIABLE: key, **self.environment}


def _tool(name: str) -> str:
    """Return the path of a console script in the environment `uv run` gave the runner.

    A nested `uv run` per shard would resolve and lock the environment again, concurrently.
    """
    return str(Path(sys.executable).with_name(name))


def pytest_arguments(index: int, splits: int, junit: Path) -> tuple[str, ...]:
    """Return integration.yml's shard command after `pytest`, for one shard.

    `--splits`, `--group` and `--junitxml` are set for the shard; the rest is CI's.
    """
    return (
        "tests/integration",
        "--no-cov",
        "-m",
        "integration",
        "--staging-backend=rustfs",
        "--splits",
        str(splits),
        "--group",
        str(index),
        "--splitting-algorithm",
        "least_duration",
        f"--junitxml={junit}",
        "-o",
        "junit_family=legacy",
    )


def shard_plan(index: int, splits: int, run_dir: Path) -> ShardPlan:
    """Return shard `index` of `splits`, writing its output under `run_dir`."""
    project = f"{PROJECT_PREFIX}{index}"
    ports = shard_ports(index)
    directory = run_dir / f"shard-{index}"
    compose = ("docker", "compose", "-p", project)
    down = (*compose, "down", "-v")
    environment = {
        "CURATE_D_LEDGER_DATABASE_URL": (
            f"postgresql+psycopg://{DATABASE_USER}:{DATABASE_PASSWORD}"
            f"@localhost:{ports.postgres}/{DATABASE}"
        ),
        "CURATE_D_TEST_RUSTFS_ENDPOINT": f"http://127.0.0.1:{ports.rustfs}",
        "CURATE_D_TEST_RCLONE_URL": f"http://127.0.0.1:{ports.rclone}",
        "CURATE_D_POSTGRES_PORT": str(ports.postgres),
        "CURATE_D_RUSTFS_PORT": str(ports.rustfs),
        "CURATE_D_RUSTFS_CONSOLE_PORT": str(ports.rustfs_console),
        "CURATE_D_RCLONE_PORT": str(ports.rclone),
    }
    steps = (
        # The residue of an earlier run that was killed; the per-shard `stack-reset`.
        Step("reset", down, Log.STACK),
        Step("up", (*compose, "up", "-d", "--wait", *STACK_SERVICES), Log.STACK),
        Step("rustfs-init", (*compose, "run", "--rm", "rustfs-init"), Log.STACK),
        Step("migrate", (_tool("alembic"), "upgrade", "head"), Log.STACK),
        Step(
            "pytest",
            (_tool("pytest"), *pytest_arguments(index, splits, directory / "junit.xml")),
            Log.PYTEST,
        ),
    )
    return ShardPlan(
        index=index,
        splits=splits,
        project=project,
        ports=ports,
        directory=directory,
        environment=environment,
        steps=steps,
        logs=Step("logs", (*compose, "logs", "--no-color", "--timestamps"), Log.STACK),
        teardown=Step("teardown", down, Log.STACK),
    )


class Runner(Protocol):
    """Runs a shard's commands and stops the running ones on an interrupt."""

    def run(
        self, argv: Sequence[str], *, env: Mapping[str, str], log: Path, always: bool = False
    ) -> int:
        """Run `argv` to completion with its output appended to `log`; return its exit code.

        After `terminate`, a command that is not `always` is not started and returns
        NOT_STARTED. Teardown and logs pass `always`, and `terminate` lets an `always`
        command that is already running finish.
        """
        ...

    def terminate(self) -> None:
        """Stop every running command that is not `always`, and refuse any later one."""
        ...


def _signal_group(child: "subprocess.Popen[bytes]", signum: int) -> None:
    """Send `signum` to the child's process group, which it leads."""
    try:
        os.killpg(child.pid, signum)
    except ProcessLookupError:
        # The child exited between the snapshot and the signal: nothing left to stop.
        return


class ProcessRunner:
    """Runs each command as a child process in its own session.

    A terminal's Ctrl-C reaches only the runner's process group, so the children never see
    it: the runner terminates them itself and then tears the stacks down, which a child
    killed alongside it could not be relied on to allow.
    """

    def __init__(self, grace_seconds: float = TERMINATE_GRACE_SECONDS) -> None:
        """Create a runner that gives a terminated child `grace_seconds` before SIGKILL."""
        self._grace_seconds = grace_seconds
        self._lock = threading.Lock()
        # The children `terminate` stops: a teardown or logs command is never among them.
        self._children: set[subprocess.Popen[bytes]] = set()
        self._stopping = False

    def run(
        self, argv: Sequence[str], *, env: Mapping[str, str], log: Path, always: bool = False
    ) -> int:
        """Run `argv` with its output appended to `log`; return its exit code.

        Raises:
            OSError: The command cannot be started, for example `docker` is not installed.
        """
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("ab") as output:
            output.write(f"$ {shlex.join(argv)}\n".encode())
            output.flush()
            with self._lock:
                if self._stopping and not always:
                    output.write(b"not started: the run was interrupted\n")
                    return NOT_STARTED
                child = subprocess.Popen(
                    list(argv),
                    env=dict(env),
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                if not always:
                    self._children.add(child)
            try:
                return child.wait()
            finally:
                with self._lock:
                    self._children.discard(child)

    def terminate(self) -> None:
        """Terminate each running child that is not `always`; kill those alive after the grace."""
        with self._lock:
            self._stopping = True
            children = list(self._children)
        for child in children:
            _signal_group(child, signal.SIGTERM)
        deadline = time.monotonic() + self._grace_seconds
        for child in children:
            try:
                child.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                _signal_group(child, signal.SIGKILL)


@dataclass(frozen=True)
class ShardResult:
    """How one shard ended."""

    plan: ShardPlan
    failed_step: str | None
    torn_down: bool
    interrupted: bool
    seconds: float
    error: str | None = None

    @property
    def passed(self) -> bool:
        """Return whether every step passed and the stack was removed."""
        return self.failed_step is None and self.torn_down and not self.interrupted

    @property
    def outputs(self) -> tuple[Path, ...]:
        """Return the files that show why the shard ended as it did."""
        if self.interrupted:
            return (self.plan.directory,)
        if self.failed_step in (None, "pytest"):
            return (self.plan.pytest_log, self.plan.junit)
        return (self.plan.stack_log,)


def _duration(seconds: float) -> str:
    """Return `seconds` as minutes and seconds, for example `3m07s`."""
    minutes, rest = divmod(round(seconds), 60)
    return f"{minutes}m{rest:02d}s"


class ShardExecutor:
    """Runs every shard at once, one thread each, and reports the outcome.

    Each shard runs its steps in order and stops at the first that fails. A failed shard
    gets its `docker compose logs` in `stack.log`. Every shard that ran a step has its stack
    removed, whatever happened.
    """

    def __init__(
        self,
        plans: Sequence[ShardPlan],
        runner: Runner,
        *,
        base_env: Mapping[str, str],
        out: TextIO,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Create an executor for `plans` that runs commands through `runner`.

        Args:
            plans: The shards, run at once.
            runner: Runs each step, logs and teardown command.
            base_env: The environment each shard's settings are laid over.
            out: Where progress and the summary are written.
            clock: The time source for each shard's duration.
        """
        self._plans = tuple(plans)
        self._runner = runner
        self._base_env = dict(base_env)
        self._out = out
        self._clock = clock
        self._stop = threading.Event()
        self._signal: int | None = None
        self._say_lock = threading.Lock()
        self._results: dict[int, ShardResult] = {}

    def run(self) -> int:
        """Run every shard to completion; return 0 when all passed, 1, or 128 + the signal."""
        threads = [
            threading.Thread(target=self._shard, args=(plan,), name=plan.project)
            for plan in self._plans
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            # A bounded join keeps the main thread returning to the interpreter, where
            # signal handlers run.
            while thread.is_alive():
                thread.join(0.2)
        results = [self._result(plan) for plan in self._plans]
        self._summarize(results)
        if self._signal is not None:
            return 128 + self._signal
        return 0 if all(result.passed for result in results) else 1

    def interrupt(self, signum: int) -> None:
        """Stop the run: terminate the running steps and start no further one.

        Each shard still removes its stack. Only the first call acts.
        """
        if self._stop.is_set():
            return
        self._signal = signum
        self._stop.set()
        self._say(f"{signal.Signals(signum).name}: stopping the shards and removing their stacks")
        self._runner.terminate()

    def _result(self, plan: ShardPlan) -> ShardResult:
        """Return a shard's result, or a failure when its thread ended without one."""
        return self._results.get(
            plan.index,
            ShardResult(
                plan=plan,
                failed_step="runner",
                torn_down=False,
                interrupted=False,
                seconds=0.0,
                error="the shard's thread raised; see its traceback on stderr",
            ),
        )

    def _shard(self, plan: ShardPlan) -> None:
        """Run one shard's steps, then its logs on failure and its teardown."""
        started = self._clock()
        env = plan.env(self._base_env)
        failed: str | None = None
        error: str | None = None
        ran = False
        # False until the steps end in a known way: any other exception still fails the
        # shard and removes its stack before the thread reports it.
        settled = False
        self._say(f"{self._name(plan)} started: {plan.project}, output in {plan.directory}")
        try:
            for step in plan.steps:
                if self._stop.is_set():
                    failed = step.name
                    break
                ran = True
                if self._runner.run(step.argv, env=env, log=plan.directory / step.log) != 0:
                    failed = step.name
                    break
            settled = True
        except OSError as exc:
            failed = step.name
            error = f"{step.name} could not start: {exc}"
            settled = True
        finally:
            if not settled:
                failed = failed or "runner"
                error = "the runner raised; see its traceback on stderr"
            interrupted = self._stop.is_set()
            torn_down = True
            if ran:
                torn_down = self._finish(plan, env, failed is not None and not interrupted)
            result = ShardResult(
                plan=plan,
                failed_step=failed,
                torn_down=torn_down,
                interrupted=interrupted,
                seconds=self._clock() - started,
                error=error,
            )
            self._results[plan.index] = result
            self._say(self._finished(result))

    def _finish(self, plan: ShardPlan, env: Mapping[str, str], failed: bool) -> bool:
        """Save a failed shard's container logs, then remove its stack; return whether it went.

        An interrupted shard skips the logs: the developer is waiting on the teardown.
        """
        log = plan.directory / Log.STACK
        if failed:
            try:
                self._runner.run(plan.logs.argv, env=env, log=log, always=True)
            except OSError as exc:
                # The logs only explain the failure; the teardown must still run.
                self._say(f"{self._name(plan)}: saving {plan.project}'s logs failed: {exc}")
        try:
            return self._runner.run(plan.teardown.argv, env=env, log=log, always=True) == 0
        except OSError as exc:
            self._say(f"{self._name(plan)}: removing {plan.project} failed: {exc}")
            return False

    def _name(self, plan: ShardPlan) -> str:
        """Return how the terminal names a shard, for example `shard 2/4`."""
        return f"shard {plan.index}/{plan.splits}"

    def _finished(self, result: ShardResult) -> str:
        """Return the line printed when a shard ends."""
        name, took = self._name(result.plan), _duration(result.seconds)
        if result.interrupted:
            return f"{name} interrupted after {took}"
        if result.failed_step is not None:
            return f"{name} failed at {result.failed_step} after {took}"
        if not result.torn_down:
            return f"{name} passed in {took}, but its stack was not removed"
        return f"{name} passed in {took}"

    def _summarize(self, results: Sequence[ShardResult]) -> None:
        """Print one row per shard, then what to do about the failed ones."""
        rows = [("shard", "result", "step", "time", "output")]
        for result in results:
            if result.passed:
                outcome = "passed"
            elif result.interrupted:
                outcome = "interrupted"
            else:
                outcome = "FAILED"
            step = result.failed_step or ("teardown" if not result.torn_down else "-")
            outputs = ", ".join(str(path) for path in result.outputs)
            rows.append((str(result.plan.index), outcome, step, _duration(result.seconds), outputs))
        widths = [max(len(row[column]) for row in rows) for column in range(4)]
        lines = ["", "Summary"]
        for row in rows:
            cells = [cell.ljust(width) for cell, width in zip(row[:4], widths, strict=True)]
            lines.append("  " + "  ".join([*cells, row[4]]))
        for result in results:
            if result.error is not None:
                lines.append(f"shard {result.plan.index}: {result.error}")
            if not result.torn_down:
                lines.append(
                    f"shard {result.plan.index}: its stack is still up; remove it with "
                    f"`docker compose -p {result.plan.project} down -v`"
                )
        failed = [result for result in results if not result.passed and not result.interrupted]
        if failed:
            splits = results[0].plan.splits
            lines.append(
                "Failed shards: "
                + ", ".join(str(result.plan.index) for result in failed)
                + f". Rerun one: make test-stack && make test-integration GROUP=<n> SPLITS={splits}"
            )
        self._say("\n".join(lines))

    def _say(self, text: str) -> None:
        """Print a line to the terminal, whole, whichever thread prints it."""
        with self._say_lock:
            print(text, file=self._out, flush=True)


def signal_handler(executor: ShardExecutor) -> Callable[[int, FrameType | None], None]:
    """Return the SIGINT and SIGTERM handler that interrupts `executor`.

    The first signal ignores both from then on: a second Ctrl-C during teardown would
    otherwise leave the stacks behind. SIGKILL still stops the runner; the next run's reset
    step removes what it leaves.
    """

    def handle(signum: int, frame: FrameType | None) -> None:
        """Ignore further interrupts and stop the shards."""
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        executor.interrupt(signum)

    return handle


def main(argv: Sequence[str] | None = None) -> int:
    """Run the shards `--splits` names; return the process exit code.

    Raises:
        SystemExit: The arguments or `--splits` are invalid (exit code 2).
    """
    parser = argparse.ArgumentParser(
        prog="python -m tests.parallel_integration",
        description="Run the integration suite as CI's shards, each on its own stack, at once.",
    )
    parser.add_argument("--splits", required=True, help="shard count, 1 to 32 (SPLITS)")
    arguments = parser.parse_args(argv)
    try:
        splits = parse_splits(arguments.splits)
    except ValueError as exc:
        # Exits 2 with the usage and the message, before any stack starts.
        parser.error(str(exc))
    os.chdir(REPOSITORY_ROOT)
    run_dir = RUNS_DIRECTORY / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    plans = [shard_plan(index, splits, run_dir) for index in range(1, splits + 1)]
    executor = ShardExecutor(plans, ProcessRunner(), base_env=os.environ, out=sys.stdout)
    handler = signal_handler(executor)
    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)
    return executor.run()


if __name__ == "__main__":
    sys.exit(main())
