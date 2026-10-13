"""The version lease and registry against PostgreSQL's advisory locks.

The unit tests check the lease's logic on an in-memory lock server; these check that the
SQL means what that server models: shared holders coexist, an exclusive holder excludes
both modes, a terminated backend frees every key it held, and a registration keeps its
first time. The promotion fence tests also check `lock_timeout` bounds promotion's wait.
"""

import asyncio
import threading
import time
from collections.abc import AsyncIterator
from uuid import uuid4

import psycopg
import pytest

from curate_d.config import Settings
from curate_d.runtime.lease import ExecutorLease, lease_conninfo
from curate_d.runtime.version_lease import (
    LockMode,
    PromotionFenceTimeoutError,
    hold_exclusive,
    hold_fence_exclusive,
    hold_fence_shared,
    register_version,
    registrations,
    standalone_version_lease,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def version() -> str:
    """Return a version no other run or deployed worker uses."""
    return f"9.9.9-vlease-{uuid4().hex[:8]}"


@pytest.fixture
def fence_application() -> str:
    """Return an application name whose fence no deployed sweep or other run takes."""
    return f"fence-test-{uuid4().hex[:8]}"


@pytest.fixture
async def registry_cleanup(settings: Settings, version: str) -> AsyncIterator[None]:
    """Delete the test version's registration afterwards."""
    yield
    with psycopg.connect(lease_conninfo(settings), autocommit=True) as connection:
        connection.execute("DELETE FROM lease_versions WHERE version = %(v)s", {"v": version})


def _worker_lease(settings: Settings, version: str) -> ExecutorLease:
    """Return a worker's lease on a fresh executor identity and `version`."""
    return ExecutorLease.for_settings(
        settings, f"vlease-{uuid4().hex[:8]}", version=version, purpose="version lease test"
    )


async def test_workers_of_one_version_hold_it_together(settings: Settings, version: str) -> None:
    """Shared holders coexist, and an exclusive hold is refused while any lives."""
    first, second = _worker_lease(settings, version), _worker_lease(settings, version)
    try:
        assert first.try_acquire()
        assert second.try_acquire()
        async with hold_exclusive(settings, version) as hold:
            assert not hold.held
            assert hold.holder_mode is LockMode.SHARED
    finally:
        first.release()
        second.release()

    async with hold_exclusive(settings, version) as hold:
        assert hold.held


async def test_a_second_sweep_sees_the_first_holding_exclusively(
    settings: Settings, version: str
) -> None:
    """Two sweeps of one version: the second is refused and told why."""
    async with hold_exclusive(settings, version) as first:
        assert first.held
        async with hold_exclusive(settings, version) as second:
            assert not second.held
            assert second.holder_mode is LockMode.EXCLUSIVE


async def test_a_worker_waits_for_a_sweep_then_starts(settings: Settings, version: str) -> None:
    """A shared acquire waits for an exclusive holder within its budget, then succeeds."""
    lease = _worker_lease(settings, version)
    acquired = threading.Event()
    failure: list[BaseException] = []

    def start_worker() -> None:
        try:
            lease.acquire(timeout=30, initial_backoff=0.05, max_backoff=0.1)
            acquired.set()
        except BaseException as error:  # Reported to the test thread below.
            failure.append(error)

    try:
        async with hold_exclusive(settings, version) as hold:
            assert hold.held
            thread = threading.Thread(target=start_worker)
            thread.start()
            await asyncio.sleep(0.5)
            assert not acquired.is_set(), "the worker started while a sweep held its version"
        await asyncio.to_thread(thread.join, 30)
        assert failure == []
        assert acquired.is_set()
    finally:
        lease.release()


async def test_a_terminated_backend_frees_both_keys_and_the_probe_fails(
    settings: Settings, version: str
) -> None:
    """Session locks die with their backend; the worker notices and stops."""
    executor_id = f"vlease-{uuid4().hex[:8]}"
    lease = ExecutorLease.for_settings(
        settings, executor_id, version=version, purpose="version lease kill"
    )
    lease.acquire(timeout=10)
    lost = threading.Event()
    try:
        with psycopg.connect(lease_conninfo(settings), autocommit=True) as admin:
            terminated = admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
                " WHERE application_name = %(name)s",
                {"name": f"curate-d version lease kill {executor_id}"[:63]},
            ).fetchall()
        assert terminated == [(True,)]
        lease.monitor(lost.set)
        assert await asyncio.to_thread(lost.wait, 30), "the lost lease was never reported"
        assert not lease.held
        async with hold_exclusive(settings, version) as hold:
            assert hold.held, "the terminated backend still held the version key"
        claim = ExecutorLease.for_settings(settings, executor_id)
        try:
            assert claim.try_acquire(), "the terminated backend still held the executor key"
        finally:
            claim.release()
    finally:
        lease.release()


async def test_a_standalone_lease_shares_the_version_with_workers(
    settings: Settings, version: str
) -> None:
    """The operator sweep holds the version beside live workers, and blocks a sweep of it."""
    worker = _worker_lease(settings, version)
    standalone = standalone_version_lease(settings, version)
    try:
        assert worker.try_acquire()
        standalone.acquire(timeout=10)
        standalone.ensure_held()
        async with hold_exclusive(settings, version) as hold:
            assert not hold.held
    finally:
        standalone.release()
        worker.release()


async def test_registration_keeps_the_first_time(
    settings: Settings, version: str, registry_cleanup: None
) -> None:
    """A restart never retimes a version; the database clock sets the time."""
    register_version(settings, version)
    first = (await registrations(settings))[version]
    time.sleep(0.01)
    register_version(settings, version)
    assert (await registrations(settings))[version] == first


async def test_sweeps_hold_the_promotion_fence_together(
    settings: Settings, fence_application: str
) -> None:
    """Two shared holders coexist, and a promotion times out while either holds it."""
    async with (
        hold_fence_shared(settings, application_name=fence_application) as first,
        hold_fence_shared(settings, application_name=fence_application) as second,
    ):
        assert first and second
        with pytest.raises(PromotionFenceTimeoutError):
            await asyncio.to_thread(_promote_within, settings, fence_application, 0.2)
    await asyncio.to_thread(_promote_within, settings, fence_application, 5)


def _promote_within(settings: Settings, application_name: str, timeout: float) -> None:
    """Hold the fence exclusively for an instant, as a promotion does."""
    with hold_fence_exclusive(settings, timeout=timeout, application_name=application_name):
        pass


async def test_a_sweep_holding_the_fence_blocks_promotion_until_released(
    settings: Settings, fence_application: str
) -> None:
    """Promotion waits for the sweep; a sweep starting meanwhile is refused, not queued ahead."""
    promoted = threading.Event()
    release = threading.Event()
    failure: list[BaseException] = []

    def promote() -> None:
        try:
            with hold_fence_exclusive(settings, timeout=30, application_name=fence_application):
                promoted.set()
                release.wait(30)
        except BaseException as error:  # Reported to the test thread below.
            failure.append(error)

    thread = threading.Thread(target=promote)
    try:
        async with hold_fence_shared(settings, application_name=fence_application) as fenced:
            assert fenced
            thread.start()
            await asyncio.sleep(0.5)
            assert not promoted.is_set(), "promotion ran while a sweep held the fence"
            async with hold_fence_shared(settings, application_name=fence_application) as late:
                assert not late, "a new sweep took the fence ahead of a waiting promotion"
        assert await asyncio.to_thread(promoted.wait, 30), "promotion never took the fence"
        async with hold_fence_shared(settings, application_name=fence_application) as during:
            assert not during
    finally:
        release.set()
        await asyncio.to_thread(thread.join, 30)
    assert failure == []
    async with hold_fence_shared(settings, application_name=fence_application) as after:
        assert after
