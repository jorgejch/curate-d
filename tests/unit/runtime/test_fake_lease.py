"""The test doubles model the locks and DBOS behaviour the recovery sweep relies on."""

import pytest
from sqlalchemy.exc import IntegrityError

from curate_d.runtime.sweep import ENQUEUED, RecoveryControl
from curate_d.runtime.version_lease import LockMode
from tests.unit.runtime.fake_lease import FakeLockServer
from tests.unit.runtime.fake_recovery import (
    FakePromotionFence,
    FakeRecoveryControl,
    FakeRegistrations,
    FakeVersionHolds,
    registered,
    workflow_status,
)

KEY = 7


def test_many_sessions_hold_a_key_shared() -> None:
    """Shared holders coexist."""
    server = FakeLockServer()
    first, second = server.connect(), server.connect()
    assert first.try_lock_shared(KEY)
    assert second.try_lock_shared(KEY)
    assert first.holds(KEY) and second.holds(KEY)
    assert server.mode(KEY) == "shared"


def test_a_shared_holder_refuses_an_exclusive_lock() -> None:
    """An exclusive lock waits for every shared holder to leave."""
    server = FakeLockServer()
    shared, exclusive = server.connect(), server.connect()
    assert shared.try_lock_shared(KEY)
    assert not exclusive.try_lock(KEY)
    shared.unlock_shared(KEY)
    assert exclusive.try_lock(KEY)


def test_an_exclusive_holder_refuses_a_shared_lock() -> None:
    """A shared lock waits for an exclusive holder to leave."""
    server = FakeLockServer()
    exclusive, shared = server.connect(), server.connect()
    assert exclusive.try_lock(KEY)
    assert not shared.try_lock_shared(KEY)
    assert server.mode(KEY) == "exclusive"
    exclusive.unlock(KEY)
    assert shared.try_lock_shared(KEY)


def test_closing_a_session_frees_its_shared_locks() -> None:
    """A backend's exit releases both modes."""
    server = FakeLockServer()
    session = server.connect()
    session.try_lock(1)
    session.try_lock_shared(KEY)
    session.close()
    assert server.mode(1) is None
    assert server.mode(KEY) is None


def test_version_lock_events_are_told_apart_from_lease_events() -> None:
    """Shared-lock events are not recorded as lease events."""
    server = FakeLockServer()
    session = server.connect()
    session.try_lock(1)
    session.try_lock_shared(KEY)
    session.unlock_shared(KEY)
    assert server.events == [
        "lease-acquired",
        "version-shared-acquired",
        "version-shared-released",
    ]


async def test_the_fake_control_forks_cancels_and_reenqueues_like_dbos() -> None:
    """Fork links the successor, cancel clears the queue, re-enqueue keeps it."""
    control = FakeRecoveryControl(
        [workflow_status("old", version="v1"), workflow_status("dead", version="v2")]
    )
    checked: RecoveryControl = control  # The fake must satisfy the protocol mypy checks.
    assert checked is control

    await control.fork(
        "old", "new", 1, application_version="v2", queue_name="stage", queue_partition_key="p"
    )
    await control.cancel("old")
    assert await control.reenqueue("dead", "retired")

    successor = control.rows["new"]
    assert (successor.forked_from, successor.app_version, successor.status) == (
        "old",
        "v2",
        ENQUEUED,
    )
    assert control.rows["old"].status == "CANCELLED"
    assert control.rows["old"].queue_name is None
    assert control.rows["dead"].queue_name == "stage"
    assert [call[0] for call in control.writes()] == ["fork", "cancel", "reenqueue"]


async def test_a_second_fork_under_one_successor_conflicts() -> None:
    """The successor's primary key arbitrates concurrent sweeps."""
    control = FakeRecoveryControl([workflow_status("old")])
    await control.fork(
        "old", "new", 1, application_version="v2", queue_name="stage", queue_partition_key=None
    )
    with pytest.raises(IntegrityError):
        await control.fork(
            "old", "new", 1, application_version="v2", queue_name="stage", queue_partition_key=None
        )


async def test_the_fake_control_scripts_latest_and_errors() -> None:
    """Latest answers advance and repeat; registered errors are raised."""
    control = FakeRecoveryControl(latest=["v2", "v3"])
    control.errors[("cancel", "*")] = RuntimeError("boom")
    assert [await control.latest_version() for _ in range(3)] == ["v2", "v3", "v3"]
    with pytest.raises(RuntimeError, match="boom"):
        await control.cancel("any")


async def test_registrations_answer_or_fail() -> None:
    """The registry fake returns its entries in order, or its error."""
    entries = registered("v1", "v2")
    assert entries["v1"] < entries["v2"]
    assert await FakeRegistrations(entries)() == entries
    with pytest.raises(OSError):
        await FakeRegistrations(error=OSError("down"))()


async def test_version_holds_release_on_every_exit() -> None:
    """A taken hold is released even when its block raises."""
    holds = FakeVersionHolds(held_elsewhere={"v2": LockMode.SHARED})
    with pytest.raises(RuntimeError):
        async with holds("v1") as hold:
            assert hold.held
            raise RuntimeError("fork failed")
    async with holds("v2") as refused:
        assert not refused.held
        assert refused.holder_mode is LockMode.SHARED
    assert holds.events == ["hold:v1", "release:v1"]
    assert holds.held == set()


async def test_version_holds_raise_on_a_lock_error() -> None:
    """A lock error surfaces before any block runs."""
    holds = FakeVersionHolds(errors={"v1": OSError("lock failed")})
    with pytest.raises(OSError):
        async with holds("v1"):
            pytest.fail("the block must not run")


def test_a_bounded_wait_answers_as_a_try_and_records_its_timeout() -> None:
    """Nothing is released during a fake wait, so a shared holder refuses it."""
    server = FakeLockServer()
    shared, waiter = server.connect(), server.connect()
    assert shared.try_lock_shared(KEY)
    assert not waiter.lock_within(KEY, 3.0)
    shared.unlock_shared(KEY)
    assert waiter.lock_within(KEY, 4.0)
    assert server.waits == [3.0, 4.0]


async def test_the_promotion_fence_releases_on_every_exit_and_refuses_while_promoting() -> None:
    """A taken fence is released even when its block raises; a promotion refuses it."""
    fence = FakePromotionFence()
    with pytest.raises(RuntimeError):
        async with fence() as fenced:
            assert fenced and fence.held
            raise RuntimeError("fork failed")
    assert not fence.held
    assert fence.events == ["fence:hold", "fence:release"]
    fence.promoting = True
    async with fence() as fenced:
        assert not fenced
    assert fence.attempts == 2
