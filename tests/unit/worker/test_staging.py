"""Exercise the real background staging loop with substituted external effects."""

import asyncio
import inspect
import threading
import time
from datetime import timedelta
from unittest.mock import AsyncMock, Mock

import pytest
from dbos import DBOS

from curate_d.config import Settings, StagingRole
from curate_d.runtime.lease import ExecutorLease
from curate_d.runtime.profiles import WorkerProfile
from curate_d.staging.errors import (
    StagingNotReadyError,
    StagingObjectStoreError,
    StagingRoleMismatchError,
)
from curate_d.staging.factories import CleanupReport, StagingReaderBackend, StagingWriterBackend
from curate_d.staging.occupancy import ReclaimReport
from curate_d.worker import main as worker
from tests.unit.runtime.fake_lease import FakeLockServer
from tests.unit.staging.test_factories import settings_for


@pytest.fixture
def effects(monkeypatch: pytest.MonkeyPatch) -> Mock:
    """Substitute readiness, cleanup and factories while retaining the actual loop."""
    result = Mock()
    result.closed = AsyncMock()
    result.writer = StagingWriterBackend(Mock(), Mock(), Mock(), timedelta(hours=24))
    result.reader = StagingReaderBackend(Mock(), Mock())
    result.ready = AsyncMock()
    result.cleanup = AsyncMock(return_value=CleanupReport(ReclaimReport((), (), 0, 0), 0))
    monkeypatch.setattr(worker, "check_staging_ready", result.ready)
    monkeypatch.setattr(worker, "run_idle_cleanup", result.cleanup)
    monkeypatch.setattr(worker, "build_writer_backend", Mock(return_value=result.writer))
    monkeypatch.setattr(worker, "build_reader_backend", Mock(return_value=result.reader))
    monkeypatch.setattr(StagingWriterBackend, "aclose", result.closed)
    monkeypatch.setattr(StagingReaderBackend, "aclose", result.closed)
    return result


@pytest.mark.parametrize(
    "profile,role",
    [(WorkerProfile.IO, StagingRole.READER), (WorkerProfile.CPU, StagingRole.WRITER)],
)
def test_profile_role_mismatch_fails_before_start(
    profile: WorkerProfile, role: StagingRole
) -> None:
    """Refuse swapped identities before allocating a background thread."""
    with pytest.raises(StagingRoleMismatchError):
        worker.StagingSupervisor(settings_for(role), profile)


@pytest.mark.parametrize("interval,grace", [(0, 1), (-1, 1), (1, -1)])
def test_invalid_timer_bounds_fail(interval: float, grace: float) -> None:
    """Reject timers that busy-loop or make shutdown unbounded."""
    with pytest.raises(ValueError):
        worker.StagingSupervisor(
            settings_for(), WorkerProfile.IO, cleanup_interval_seconds=interval, drain_seconds=grace
        )


def test_idle_cleanup_repeats_without_jobs_on_one_loop(effects: Mock) -> None:
    """Reuse one loop for idle cleanup, readiness and submitted staging steps."""
    loops: list[asyncio.AbstractEventLoop] = []
    ticks = threading.Event()

    async def ready(*args: object, **kwargs: object) -> None:
        loops.append(asyncio.get_running_loop())

    async def clean(backend: StagingWriterBackend) -> CleanupReport:
        loops.append(asyncio.get_running_loop())
        if len(loops) >= 3:
            ticks.set()
        return CleanupReport(ReclaimReport((), (), 0, 0), 0)

    effects.ready.side_effect = ready
    effects.cleanup.side_effect = clean
    supervisor = worker.StagingSupervisor(
        settings_for(), WorkerProfile.IO, cleanup_interval_seconds=0.01
    )
    try:
        supervisor.start()
        assert ticks.wait(2), "idle cleanup did not repeat"
        assert supervisor.writer is effects.writer
        assert supervisor.reader is None

        async def step() -> asyncio.AbstractEventLoop:
            return asyncio.get_running_loop()

        assert supervisor.submit(step(), timeout=1) is loops[0]
        assert all(loop is loops[0] for loop in loops)
        assert supervisor.cleanup_ticks >= 2
        with pytest.raises(RuntimeError, match="only be started once"):
            supervisor.start()
    finally:
        supervisor.stop()
    assert supervisor.writer is None
    assert loops[0].is_closed()
    effects.closed.assert_awaited_once()
    supervisor.stop()


def test_reader_never_builds_or_runs_writer_cleanup(
    effects: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep writer construction and cleanup unreachable from the CPU profile."""
    build_writer = Mock(side_effect=AssertionError("CPU cannot construct writer"))
    monkeypatch.setattr(worker, "build_writer_backend", build_writer)
    supervisor = worker.StagingSupervisor(settings_for(StagingRole.READER), WorkerProfile.CPU)
    try:
        supervisor.start()
        assert supervisor.reader is effects.reader
        assert supervisor.writer is None
    finally:
        supervisor.stop()
    build_writer.assert_not_called()
    effects.cleanup.assert_not_awaited()
    effects.closed.assert_awaited_once()


def test_stop_interrupts_long_idle_interval(effects: Mock) -> None:
    """Wake a sleeping cleanup loop promptly when shutdown arrives."""
    supervisor = worker.StagingSupervisor(
        settings_for(), WorkerProfile.IO, cleanup_interval_seconds=3600
    )
    supervisor.start()
    before = time.monotonic()
    supervisor.stop(timeout=2)
    assert time.monotonic() - before < 1
    effects.closed.assert_awaited_once()


def test_stop_cancels_blocked_sweep_and_disposes_engine(effects: Mock) -> None:
    """Bound a blocked cleanup pass by the configured drain grace."""
    entered = threading.Event()
    cancelled = threading.Event()

    async def blocked(backend: StagingWriterBackend) -> CleanupReport:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        raise AssertionError("unreachable")

    effects.cleanup.side_effect = blocked
    supervisor = worker.StagingSupervisor(settings_for(), WorkerProfile.IO, drain_seconds=0.02)
    try:
        supervisor.start()
        assert entered.wait(2)
    finally:
        supervisor.stop(timeout=2)
    assert cancelled.is_set()
    effects.closed.assert_awaited_once()


def test_failed_cleanup_is_reported_and_retried(
    effects: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep timer cleanup alive after a retryable staging failure."""
    succeeded = threading.Event()
    count = 0

    async def clean(backend: StagingWriterBackend) -> CleanupReport:
        nonlocal count
        count += 1
        if count == 1:
            raise StagingObjectStoreError("temporary listing failure")
        succeeded.set()
        return CleanupReport(ReclaimReport((), (), 0, 0), 0)

    log = Mock()
    monkeypatch.setattr(worker, "logger", log)
    effects.cleanup.side_effect = clean
    supervisor = worker.StagingSupervisor(
        settings_for(), WorkerProfile.IO, cleanup_interval_seconds=0.01
    )
    try:
        supervisor.start()
        assert succeeded.wait(2)
    finally:
        supervisor.stop()
    log.warning.assert_called_once_with(
        "staging.cleanup_failed", error_type="StagingObjectStoreError"
    )


def test_failed_readiness_disposes_backend_and_stops_thread(effects: Mock) -> None:
    """Leave no loop or pool behind after an unreachable-bucket startup failure."""
    effects.ready.side_effect = StagingNotReadyError("unavailable")
    supervisor = worker.StagingSupervisor(settings_for(), WorkerProfile.IO)
    with pytest.raises(StagingNotReadyError, match="unavailable"):
        supervisor.start()
    assert supervisor.writer is None
    assert supervisor._thread is None
    effects.closed.assert_awaited_once()


def test_readiness_has_bounded_timeout(effects: Mock) -> None:
    """Cancel stalled readiness before a worker can dequeue."""

    async def blocked(*args: object, **kwargs: object) -> None:
        await asyncio.Event().wait()

    effects.ready.side_effect = blocked
    settings = settings_for().model_copy(update={"staging_transfer_timeout_seconds": 0.01})
    supervisor = worker.StagingSupervisor(settings, WorkerProfile.IO)
    with pytest.raises(StagingNotReadyError, match="timeout"):
        supervisor.start()
    assert supervisor._thread is None
    effects.closed.assert_awaited_once()


def test_submit_before_start_closes_coroutine() -> None:
    """Refuse a step without a running loop and close its unawaited coroutine."""
    supervisor = worker.StagingSupervisor(settings_for(), WorkerProfile.IO)

    async def step() -> int:
        return 1

    work = step()
    with pytest.raises(StagingNotReadyError):
        supervisor.submit(work)
    assert inspect.iscoroutine(work)
    assert work.cr_frame is None
    supervisor.stop()


def test_run_executes_on_the_staging_loop_from_another_loop(effects: Mock) -> None:
    """An async caller awaits work that runs on the staging loop, not its own."""
    supervisor = worker.StagingSupervisor(settings_for(StagingRole.READER), WorkerProfile.CPU)

    async def which_loop() -> asyncio.AbstractEventLoop:
        return asyncio.get_running_loop()

    async def caller() -> tuple[asyncio.AbstractEventLoop, asyncio.AbstractEventLoop]:
        return asyncio.get_running_loop(), await supervisor.run(which_loop())

    try:
        supervisor.start()
        own, staging = asyncio.run(caller())
        assert own is not staging
        assert staging is supervisor._loop
    finally:
        supervisor.stop()


def test_run_before_start_closes_coroutine() -> None:
    """An async caller gets the same refusal as a synchronous one."""
    supervisor = worker.StagingSupervisor(settings_for(), WorkerProfile.IO)

    async def step() -> int:
        return 1

    work = step()
    with pytest.raises(StagingNotReadyError):
        asyncio.run(supervisor.run(work))
    assert inspect.getcoroutinestate(work) == inspect.CORO_CLOSED
    supervisor.stop()


def test_submit_timeout_cancels_outstanding_effect(effects: Mock) -> None:
    """Cancel timed-out submitted work rather than letting effects run unnoticed."""
    cancelled = threading.Event()

    async def blocked() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    supervisor = worker.StagingSupervisor(settings_for(StagingRole.READER), WorkerProfile.CPU)
    try:
        supervisor.start()
        with pytest.raises(TimeoutError):
            supervisor.submit(blocked(), timeout=0.02)
        assert cancelled.wait(2)
    finally:
        supervisor.stop()


def test_submit_from_loop_refuses_deadlock(effects: Mock) -> None:
    """Reject synchronous submission from the staging loop itself."""
    supervisor = worker.StagingSupervisor(settings_for(StagingRole.READER), WorkerProfile.CPU)

    async def nested() -> None:
        async def inner() -> None:
            return None

        with pytest.raises(RuntimeError, match="outside"):
            supervisor.submit(inner())

    try:
        supervisor.start()
        supervisor.submit(nested(), timeout=1)
    finally:
        supervisor.stop()


@pytest.fixture
def startup_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record synchronous DBOS lifecycle operations."""
    calls: list[str] = []
    monkeypatch.setattr(worker, "resolve_executor_id", lambda: "worker-test")
    monkeypatch.setattr(worker, "configure", lambda *a, **kw: calls.append("configure"))
    monkeypatch.setattr(DBOS, "listen_queues", lambda *a: calls.append("listen"))
    monkeypatch.setattr(DBOS, "launch", lambda: calls.append("launch"))
    monkeypatch.setattr(worker, "register_all", lambda: calls.append("register"))
    monkeypatch.setattr(worker, "prepare_services", lambda settings, profile, staging: None)
    monkeypatch.setattr(worker, "apply_schedules", lambda profile, settings: None)
    monkeypatch.setattr(DBOS, "destroy", lambda **kw: calls.append("drain"))
    monkeypatch.setattr(worker, "_install_shutdown_handler", lambda stop: None)
    locks = FakeLockServer(events=calls)
    monkeypatch.setattr(
        ExecutorLease, "for_settings", lambda settings, executor_id: locks.lease(executor_id)
    )
    return calls


class RecordedLifecycle:
    """Record staging startup and shutdown around DBOS operations."""

    def __init__(self, calls: list[str], failure: Exception | None = None) -> None:
        self.calls = calls
        self.failure = failure

    def start(self) -> None:
        self.calls.append("staging-ready")
        if self.failure is not None:
            raise self.failure

    def stop(self) -> None:
        self.calls.append("staging-stop")


def test_readiness_precedes_dbos_and_shutdown_follows_drain(startup_calls: list[str]) -> None:
    """Keep queues disabled until readiness and retain staging through the DBOS drain."""
    stop = threading.Event()
    stop.set()
    worker.run(
        settings_for(),
        WorkerProfile.IO,
        stop=stop,
        staging_factory=lambda settings, profile: RecordedLifecycle(startup_calls),
    )
    assert startup_calls == [
        "staging-ready",
        "lease-acquired",
        "configure",
        "listen",
        "launch",
        "register",
        "drain",
        "staging-stop",
        "lease-released",
    ]


def test_worker_readiness_failure_starts_no_dbos(startup_calls: list[str]) -> None:
    """Exit before launching DBOS when the S3 endpoint is unavailable."""
    with pytest.raises(StagingNotReadyError):
        worker.run(
            settings_for(),
            WorkerProfile.IO,
            block=False,
            staging_factory=lambda settings, profile: RecordedLifecycle(
                startup_calls, StagingNotReadyError("offline")
            ),
        )
    assert startup_calls == ["staging-ready", "staging-stop"]


def test_worker_profile_mismatch_rejects_even_substituted_factory(startup_calls: list[str]) -> None:
    """Enforce profile identity independently of the selected lifecycle factory."""
    with pytest.raises(StagingRoleMismatchError):
        worker.run(
            settings_for(StagingRole.READER),
            WorkerProfile.IO,
            block=False,
            staging_factory=lambda settings, profile: RecordedLifecycle(startup_calls),
        )
    assert startup_calls == []


def test_startup_failure_after_launch_drains_before_closing_staging(
    startup_calls: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dispose launched DBOS and staging when queue registration fails."""
    monkeypatch.setattr(
        worker, "register_all", Mock(side_effect=RuntimeError("registration failed"))
    )
    with pytest.raises(RuntimeError):
        worker.run(
            settings_for(),
            WorkerProfile.IO,
            staging_factory=lambda settings, profile: RecordedLifecycle(startup_calls),
        )
    assert startup_calls[-3:] == ["drain", "staging-stop", "lease-released"]


def test_unconfigured_worker_has_no_filesystem_staging(startup_calls: list[str]) -> None:
    """Keep legacy non-staging workers explicit when no endpoint is configured."""
    factory = Mock(side_effect=AssertionError("must not build fallback"))
    worker.run(
        Settings(staging_s3_endpoint_url=None),
        WorkerProfile.IO,
        block=False,
        staging_factory=factory,
    )
    factory.assert_not_called()
    assert startup_calls == [
        "lease-acquired",
        "configure",
        "listen",
        "launch",
        "register",
        "lease-released",
    ]


@pytest.mark.parametrize(
    "readiness,expected",
    [(StagingNotReadyError("unavailable"), "unavailable"), (None, "timeout")],
)
def test_a_stop_that_cannot_join_does_not_replace_the_startup_diagnosis(
    effects: Mock, monkeypatch: pytest.MonkeyPatch, readiness: Exception | None, expected: str
) -> None:
    """Keep the reason startup failed when the cleanup stop reports a join timeout."""

    async def blocked(*args: object, **kwargs: object) -> None:
        await asyncio.Event().wait()

    effects.ready.side_effect = readiness or blocked
    settings = settings_for().model_copy(update={"staging_transfer_timeout_seconds": 0.01})
    supervisor = worker.StagingSupervisor(settings, WorkerProfile.IO)
    joined = supervisor.stop

    def stop_then_report_a_join_failure(timeout: float = worker.STAGING_STOP_SECONDS) -> None:
        """Unwind the loop as usual, then fail the way a wedged thread would."""
        joined(timeout)
        raise StagingNotReadyError("staging did not stop within 30.0 seconds")

    monkeypatch.setattr(supervisor, "stop", stop_then_report_a_join_failure)
    with pytest.raises(StagingNotReadyError, match=expected) as caught:
        supervisor.start()
    assert "did not stop" not in str(caught.value)
