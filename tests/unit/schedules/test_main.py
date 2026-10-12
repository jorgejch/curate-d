"""The deployment command's subcommands and exit statuses."""

import io
from collections.abc import Callable, Iterator
from contextlib import contextmanager

import pytest

from curate_d.config import Settings
from curate_d.runtime import dbos_app
from curate_d.runtime.schedule_fence import ScheduleAuthorityUnavailableError, ScheduleFence
from curate_d.schedules import __main__ as command
from curate_d.schedules.deployment import QueuesNotDeclaredError, VersionNotRegisteredError
from curate_d.workflows.registry import declared_schedule_names
from tests.unit.schedules.fakes import FakeScheduleClient

_VERSION = "1.0.0"


class _Harness:
    """The command run against one fake client, recording the fence it was given."""

    def __init__(self) -> None:
        self.client: FakeScheduleClient | None = None
        self.setup: Callable[[FakeScheduleClient], None] = lambda _client: None
        self.fail: BaseException | None = None

    @contextmanager
    def open(self, settings: Settings, fence: ScheduleFence) -> Iterator[FakeScheduleClient]:
        """Open the fake client behind `fence`, prepared by `setup`."""
        if self.fail is not None:
            raise self.fail
        self.client = FakeScheduleClient(fence)
        self.setup(self.client)
        yield self.client

    def run(self, *argv: str) -> tuple[int, str]:
        """Run the command with `argv` and return its status and printed output."""
        out = io.StringIO()
        status = command.run(
            [*argv],
            settings=Settings(),
            open_client=self.open,
            out=out,
        )
        return status, out.getvalue()


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> _Harness:
    """Return a harness running as this build's version."""
    monkeypatch.setattr(dbos_app, "APPLICATION_VERSION", _VERSION)
    return _Harness()


def test_reconcile_applies_every_declared_schedule(harness: _Harness) -> None:
    """Exit 0 with the build's declarations stored and the retired row gone."""

    def prepare(client: FakeScheduleClient) -> None:
        client.store("retired-pass")

    harness.setup = prepare
    status, _ = harness.run("reconcile", "--wait-seconds", "0")
    assert status == 0
    assert harness.client is not None
    assert set(harness.client.rows) == declared_schedule_names(Settings())


def test_the_fence_carries_the_build_version_and_lock_timeout(harness: _Harness) -> None:
    """The fence checks this build's version under the environment's application."""
    harness.run("reconcile", "--wait-seconds", "0", "--lock-timeout-seconds", "7")
    assert harness.client is not None
    fence = harness.client.fence
    assert (fence.application, fence.version, fence.timeout_seconds) == (
        "curate-d-local",
        _VERSION,
        7.0,
    )


@pytest.mark.parametrize(
    ("versions", "queues", "status"),
    [
        (("0.9.0",), ("maintenance",), 3),
        (("1.1.0", _VERSION), ("maintenance",), 4),
        ((_VERSION,), (), 5),
    ],
    ids=["not-registered", "superseded", "queue-missing"],
)
def test_a_refused_reconcile_exits_with_its_reason_and_writes_nothing(
    harness: _Harness, versions: tuple[str, ...], queues: tuple[str, ...], status: int
) -> None:
    """The deploy job tells the three apart by status."""

    def prepare(client: FakeScheduleClient) -> None:
        client.versions, client.queues = list(versions), set(queues)

    harness.setup = prepare
    assert harness.run("reconcile", "--wait-seconds", "0")[0] == status
    assert harness.client is not None and harness.client.writes == []


def test_a_version_superseded_mid_run_exits_as_superseded(harness: _Harness) -> None:
    """The fence's refusal is the same outcome as finding a newer version up front."""

    def prepare(client: FakeScheduleClient) -> None:
        client.before_write = ["1.1.0"]

    harness.setup = prepare
    assert harness.run("reconcile", "--wait-seconds", "0")[0] == 4


@pytest.mark.parametrize("mid_run", [False, True], ids=["up-front", "mid-run"])
def test_allow_superseded_exits_0_when_a_newer_rollout_owns_the_schedules(
    harness: _Harness, mid_run: bool
) -> None:
    """A deploy job overtaken by a newer rollout can succeed without writing further."""

    def prepare(client: FakeScheduleClient) -> None:
        if mid_run:
            client.before_write = ["1.1.0"]
        else:
            client.versions = ["1.1.0", _VERSION]

    harness.setup = prepare
    assert harness.run("reconcile", "--wait-seconds", "0", "--allow-superseded")[0] == 0
    assert harness.client is not None and harness.client.writes == []


def test_a_held_authority_exits_6(harness: _Harness) -> None:
    """Another writer stuck holding the lock fails the job, distinctly."""
    harness.fail = ScheduleAuthorityUnavailableError("pid 1 holds it")
    assert harness.run("reconcile")[0] == 6


def test_a_usage_error_is_told_apart_from_every_status_the_command_returns(
    harness: _Harness,
) -> None:
    """argparse's 2 means a broken command line, which no retry fixes."""
    with pytest.raises(SystemExit) as raised:
        harness.run("reconcile", "--no-such-option")
    returned = {
        command.EXIT_AUTHORITY_UNAVAILABLE,
        command.EXIT_SUPERSEDED,
        VersionNotRegisteredError.exit_code,
        QueuesNotDeclaredError.exit_code,
    }
    assert raised.value.code == 2
    assert 2 not in returned
    assert harness.client is None


def test_promote_makes_the_version_latest_and_reconciles(harness: _Harness) -> None:
    """The rollback path: exit 0, version promoted, the build's schedules stored."""

    def prepare(client: FakeScheduleClient) -> None:
        client.versions = ["1.1.0", _VERSION]

    harness.setup = prepare
    status, _ = harness.run("promote", "--wait-seconds", "0")
    assert status == 0
    assert harness.client is not None
    assert harness.client.versions[0] == _VERSION
    assert set(harness.client.rows) == declared_schedule_names(Settings())


def test_show_prints_the_plan_and_writes_nothing(harness: _Harness) -> None:
    """A dry run for an operator: one line per schedule, prefixed by its change."""

    def prepare(client: FakeScheduleClient) -> None:
        client.store("retired-pass")
        client.store("unclaimed-pass", owner=None)

    harness.setup = prepare
    status, printed = harness.run("show")
    assert status == 0
    assert harness.client is not None and harness.client.writes == []
    lines = printed.splitlines()
    assert lines[0] == f"version {_VERSION}; latest {_VERSION}"
    assert {line for line in lines if line.startswith("+ ")} == {
        f"+ {name}" for name in declared_schedule_names(Settings())
    }
    assert "- retired-pass" in lines
    assert "? unclaimed-pass" in lines


def test_a_subcommand_is_required() -> None:
    """Running the module bare is a usage error, not a reconcile."""
    with pytest.raises(SystemExit) as raised:
        command.build_parser().parse_args([])
    assert raised.value.code == 2
