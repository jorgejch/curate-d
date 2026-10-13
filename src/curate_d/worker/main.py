"""Launch profile-specific workers after staging readiness and drain them on shutdown.

Set DBOS's listen queues before launch and register queues afterwards with
never-update semantics. Keep run() synchronous on the main thread for DBOS and signal
handling. Drain DBOS before closing staging so in-flight steps retain their backend.

StagingSupervisor owns one background event loop for readiness, cleanup and submitted
steps. SDK sessions and SQLAlchemy pools remain on that loop; per-tick asyncio.run()
would recreate loop-bound resources. Shutdown wakes the idle timer, grants an active
sweep a bounded drain interval, cancels it if needed, and disposes the pool on its loop.
"""

import asyncio
import logging
import os
import signal
import sys
import threading
from collections.abc import Callable, Coroutine, Sequence
from contextlib import suppress
from dataclasses import dataclass
from types import FrameType
from typing import Final, Protocol, runtime_checkable

from dbos import DBOS, ScheduleInput
from dbos._dbos import _get_dbos_instance
from dbos._error import DBOSException
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from curate_d.classification.remote import OpenRouterClassifier
from curate_d.config import Settings, StagingRole, get_settings
from curate_d.domain import Provider
from curate_d.observability import configure_logging, get_logger
from curate_d.placement.admission import TransferAdmission
from curate_d.placement.remotes import ConnectedRemotes, RcloneAccess
from curate_d.providers.access import ConnectedAccounts
from curate_d.providers.crypto import CredentialCipher
from curate_d.providers.pacing import Pacer, Quota
from curate_d.runtime import dbos_app
from curate_d.runtime.datasource import create_ledger_datasource
from curate_d.runtime.dbos_app import configure
from curate_d.runtime.identity import resolve_executor_id
from curate_d.runtime.lease import ExecutorLease, ExecutorLeaseLostError
from curate_d.runtime.profiles import WorkerProfile, resolve_profile
from curate_d.runtime.queues import register_all
from curate_d.runtime.schedule_authority import (
    ScheduleAuthority,
    StoredSchedule,
    schedule_authority,
)
from curate_d.runtime.services import Services, StagingAccess, install
from curate_d.runtime.version_lease import register_version
from curate_d.staging.errors import StagingError, StagingNotReadyError, StagingRoleMismatchError
from curate_d.staging.factories import (
    StagingReaderBackend,
    StagingWriterBackend,
    build_reader_backend,
    build_writer_backend,
    check_staging_ready,
    local_scratch_budget,
    run_idle_cleanup,
)
from curate_d.staging.object_store import S3ClientFactory
from curate_d.workflows.registry import declared_schedule_names, schedules_for

logger = get_logger(__name__)

# How often idle cleanup runs. Driven by a timer rather than by arriving work, because
# the spec requires aged content and abandoned uploads to be reclaimed on a cluster that
# has nothing to stage. Five minutes is well under the smallest configurable age bound,
# so an entry never waits a whole extra interval past its expiry.
CLEANUP_INTERVAL_SECONDS: Final = 300.0

# How long a sweep already running when shutdown arrives may take to finish. The writer
# lock is held across another pod's sweep and PostgreSQL will wait for it indefinitely,
# so this is the bound that keeps a drain from inheriting that wait.
CLEANUP_DRAIN_SECONDS: Final = 10.0

# How long shutdown waits for the staging thread. Above the drain grace with room for the
# engine to dispose; past it the thread is a daemon and the process exits regardless.
STAGING_STOP_SECONDS: Final = 30.0

# How long a signalled worker lets in-flight workflows finish before shutting DBOS down.
DRAIN_SECONDS: Final = 30

# Exit status when the executor lease is lost, distinct from an ordinary crash (1).
EXIT_LEASE_LOST: Final = 75

# The profile split is a credential boundary, so which staging identity each half must
# hold is fixed here rather than inferred from whichever credentials turned up.
_STAGING_ROLE: Final[dict[WorkerProfile, StagingRole]] = {
    WorkerProfile.IO: StagingRole.WRITER,
    WorkerProfile.CPU: StagingRole.READER,
}


@runtime_checkable
class StagingLifecycle(Protocol):
    """The staging supervision surface worker startup and shutdown depend on."""

    def start(self) -> None:
        """Bring staging up and prove it is ready, or raise."""
        ...

    def stop(self) -> None:
        """Tear staging down, leaving no thread, loop or pool behind."""
        ...


type StagingFactory = Callable[[Settings, WorkerProfile], StagingLifecycle]


class LeaseFactory(Protocol):
    """Builds a worker's lease on its executor identity and its version."""

    def __call__(
        self, settings: Settings, executor_id: str, /, *, version: str | None = None
    ) -> ExecutorLease:
        """Return the lease, not yet acquired."""
        ...


def require_staging_role(profile: WorkerProfile, settings: Settings) -> StagingRole:
    """Return the staging role `profile` must hold, refusing a configured mismatch.

    The check is here and not at the first upload because the symptom otherwise is an
    access denial in the middle of a workflow, which reads like a bucket policy bug and
    is actually a deployment that handed a worker the other half's credentials.

    Raises:
        StagingRoleMismatchError: Configuration declares the other role.
    """
    expected = _STAGING_ROLE[profile]
    if settings.staging_s3_role is not expected:
        raise StagingRoleMismatchError(
            f"the {profile.value} worker profile requires the staging {expected.value} "
            f"identity, but this process is configured as a "
            f"{settings.staging_s3_role.value}. Correct CURATE_D_STAGING_S3_ROLE and the "
            f"credentials it names; the profile and the role must agree."
        )
    return expected


class StagingSupervisor:
    """Own this process's staging backend on one dedicated event loop thread.

    Constructed with the profile's declared role already checked, so a mismatch fails
    before a thread exists. `start` blocks until readiness has been proven or has failed;
    `stop` is safe to call whether or not `start` succeeded.
    """

    def __init__(
        self,
        settings: Settings,
        profile: WorkerProfile,
        *,
        cleanup_interval_seconds: float = CLEANUP_INTERVAL_SECONDS,
        drain_seconds: float = CLEANUP_DRAIN_SECONDS,
        client_factory: S3ClientFactory | None = None,
        engine: AsyncEngine | None = None,
    ) -> None:
        """Bind configuration to a profile, refusing a role its credentials contradict.

        Args:
            settings: Resolved configuration. Must have an S3 staging endpoint.
            profile: Which half of the pipeline this process serves.
            cleanup_interval_seconds: Idle-cleanup period, writer profile only.
            drain_seconds: Grace given to a sweep already running at shutdown.
            client_factory: Opens an S3 client per operation; substituted by tests.
            engine: Database engine for the cache index; created and owned here when
                omitted.

        Raises:
            ValueError: The cleanup interval is not positive or the drain grace is negative.
            StagingRoleMismatchError: The configured role is not the profile's.
        """
        if cleanup_interval_seconds <= 0 or drain_seconds < 0:
            raise ValueError("cleanup interval must be positive and drain grace nonnegative")
        self._role = require_staging_role(profile, settings)
        self._settings = settings
        self._profile = profile
        self._interval = cleanup_interval_seconds
        self._drain_seconds = drain_seconds
        self._client_factory = client_factory
        self._engine = engine
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._shutdown: asyncio.Event | None = None
        self._settled = threading.Event()
        self._failure: BaseException | None = None
        self._writer: StagingWriterBackend | None = None
        self._reader: StagingReaderBackend | None = None
        self._cleanup_ticks = 0
        self._started = False
        self._serving: asyncio.Task[None] | None = None

    @property
    def writer(self) -> StagingWriterBackend | None:
        """Return the write side, or None for a CPU worker or before startup."""
        return self._writer

    @property
    def reader(self) -> StagingReaderBackend | None:
        """Return the read side, or None for an I/O worker or before startup."""
        return self._reader

    @property
    def cleanup_ticks(self) -> int:
        """Return attempted idle-cleanup passes, including failed passes."""
        return self._cleanup_ticks

    def start(self) -> None:
        """Start the staging loop and block until readiness is proven or fails.

        Raises:
            StagingNotReadyError: The bucket could not be reached, or readiness did not
                answer within the configured transfer timeout. Either way the loop is
                torn down and nothing is left running.
            StagingError: Building the backend failed.
        """
        if self._started:
            raise RuntimeError("a staging supervisor can only be started once")
        self._started = True
        thread = threading.Thread(target=self._serve_forever, name="staging", daemon=True)
        self._thread = thread
        thread.start()
        # A bounded wait rather than an indefinite one: the probe's own connect and read
        # timeouts come from the same setting, so anything past it plus a margin is a
        # wedged loop rather than a slow endpoint.
        if not self._settled.wait(self._settings.staging_transfer_timeout_seconds + 30.0):
            # Both stops below are cleanup, not the diagnosis. A stop that cannot join
            # reports only that the thread would not unwind, which would replace the
            # reason startup actually failed with a symptom of it. The loop is a daemon
            # thread, so a refused join costs the process nothing at exit.
            with suppress(StagingNotReadyError):
                self.stop()
            raise StagingNotReadyError(
                f"staging readiness did not answer within "
                f"{self._settings.staging_transfer_timeout_seconds + 30.0} seconds; the "
                f"worker starts no queues without it"
            )
        if self._failure is not None:
            with suppress(StagingNotReadyError):
                self.stop()
            raise self._failure
        logger.info(
            "staging.started",
            profile=self._profile.value,
            role=self._role.value,
            bucket=self._settings.staging_s3_bucket,
        )

    def stop(self, timeout: float = STAGING_STOP_SECONDS) -> None:
        """Ask the staging loop to finish, then join its thread.

        Idempotent, and safe after a failed `start`: everything it touches is cleared,
        so a second call finds nothing to stop.
        """
        thread, loop, shutdown = self._thread, self._loop, self._shutdown
        if thread is None:
            return
        if loop is not None and shutdown is not None:
            with suppress(RuntimeError):
                # The event belongs to that loop, so setting it from this thread directly
                # would not wake the waiter. A loop that has already closed refuses the
                # callback, and there is then nothing left to wake.
                loop.call_soon_threadsafe(shutdown.set)
                if not self._settled.is_set() and self._serving is not None:
                    loop.call_soon_threadsafe(self._serving.cancel)
        thread.join(timeout)
        if thread.is_alive():
            raise StagingNotReadyError(f"staging did not stop within {timeout} seconds")
        self._thread = self._loop = self._shutdown = None
        logger.info("staging.stopped", profile=self._profile.value)

    def submit[T](self, work: Coroutine[None, None, T], *, timeout: float | None = None) -> T:
        """Run `work` on the staging loop from a synchronous worker thread.

        The entry point for a DBOS step that needs staging. A step that opened its own
        `asyncio.run` would build a second connection pool per call and could not touch
        these backends at all, because an engine and an SDK session belong to the loop
        that created them.

        Raises:
            StagingNotReadyError: Staging is not running, so there is no loop to run on.
        """
        loop = self._loop
        if loop is None or not loop.is_running() or not self._settled.is_set():
            # Closed explicitly: an un-awaited coroutine would otherwise surface as a
            # warning from wherever the garbage collector happened to run.
            work.close()
            raise StagingNotReadyError("staging is not running; no work can be submitted")
        if threading.current_thread() is self._thread:
            work.close()
            raise RuntimeError("submit must be called outside the staging event loop")
        try:
            future = asyncio.run_coroutine_threadsafe(work, loop)
        except RuntimeError:
            work.close()
            raise StagingNotReadyError("staging event loop has stopped") from None
        try:
            return future.result(timeout)
        except TimeoutError:
            future.cancel()
            raise

    async def run[T](self, work: Coroutine[None, None, T]) -> T:
        """Run `work` on the staging loop from a coroutine on any other loop.

        The async counterpart of `submit`, for DBOS async steps: they run on DBOS's own
        loop, and blocking it on `submit` would stall every workflow sharing it. Run from
        the staging loop itself, the work is simply awaited.

        Raises:
            StagingNotReadyError: Staging is not running, so there is no loop to run on.
        """
        loop = self._loop
        if loop is None or not loop.is_running() or not self._settled.is_set():
            work.close()
            raise StagingNotReadyError("staging is not running; no work can be submitted")
        if asyncio.get_running_loop() is loop:
            return await work
        try:
            future = asyncio.run_coroutine_threadsafe(work, loop)
        except RuntimeError:
            work.close()
            raise StagingNotReadyError("staging event loop has stopped") from None
        return await asyncio.wrap_future(future)

    def _serve_forever(self) -> None:
        """Run the staging event loop until shutdown, recording why it ended."""
        try:
            asyncio.run(self._serve())
        except BaseException as failure:
            already_started = self._settled.is_set()
            self._failure = failure
            if already_started:
                # Nobody is waiting on `_failure` any more, so this is the only place it
                # can be seen: without it the worker serves on with cleanup silently dead.
                logger.error(
                    "staging.supervisor_failed",
                    profile=self._profile.value,
                    error_type=type(failure).__name__,
                )
        finally:
            # Unblocks `start` whether readiness passed, failed, or never got that far.
            self._settled.set()

    async def _serve(self) -> None:
        """Build the backend, prove it ready, then serve the loop until asked to stop."""
        self._serving = asyncio.current_task()
        shutdown = asyncio.Event()
        self._loop = asyncio.get_running_loop()
        self._shutdown = shutdown
        backend = self._build()
        try:
            try:
                async with asyncio.timeout(self._settings.staging_transfer_timeout_seconds):
                    await check_staging_ready(self._settings, client_factory=self._client_factory)
            except TimeoutError:
                raise StagingNotReadyError(
                    "staging readiness exceeded its transfer timeout"
                ) from None
            self._publish(backend)
            self._settled.set()
            await self._serve_until_stopped(backend, shutdown)
        finally:
            # Before the engine is disposed, so nothing can pick a backend up mid-teardown.
            self._writer = self._reader = None
            await backend.aclose()

    def _build(self) -> StagingWriterBackend | StagingReaderBackend:
        """Build the one side of staging this process's role permits."""
        if self._role is StagingRole.WRITER:
            return build_writer_backend(
                self._settings, client_factory=self._client_factory, engine=self._engine
            )
        return build_reader_backend(
            self._settings, client_factory=self._client_factory, engine=self._engine
        )

    def _publish(self, backend: StagingWriterBackend | StagingReaderBackend) -> None:
        """Expose the backend to the rest of the process, on the correct side only."""
        if isinstance(backend, StagingWriterBackend):
            self._writer = backend
        else:
            self._reader = backend

    async def _serve_until_stopped(
        self, backend: StagingWriterBackend | StagingReaderBackend, shutdown: asyncio.Event
    ) -> None:
        """Run idle cleanup for a writer, or simply hold the loop open for a reader.

        A reader has no cleanup authority — sweeping lists and deletes — so its loop
        exists only to serve snapshot reads submitted from DBOS's own threads.
        """
        if not isinstance(backend, StagingWriterBackend):
            await shutdown.wait()
            return

        cleaning = asyncio.create_task(self._clean_until_stopped(backend, shutdown))
        # A cleanup loop that dies of its own accord must not leave this waiting forever
        # with its exception unretrieved; waking the wait re-raises it below.
        cleaning.add_done_callback(lambda _task: shutdown.set())
        await shutdown.wait()
        try:
            await asyncio.wait_for(cleaning, timeout=self._drain_seconds)
        except TimeoutError:
            # `wait_for` has already cancelled it and waited for the cancellation, so
            # nothing is left pending. The sweep unregisters under the writer lock and
            # deletes bytes only after that transaction commits, so a cancellation
            # either lands before the commit — the transaction rolls back and the next
            # tick, in this pod or another, repeats the pass — or after it, leaving
            # unreferenced objects the next tick's reconciliation collects. Neither
            # leaves a registration naming bytes that are gone, which nothing repairs.
            logger.warning("staging.cleanup_cancelled_at_shutdown", seconds=self._drain_seconds)

    async def _clean_until_stopped(
        self, backend: StagingWriterBackend, shutdown: asyncio.Event
    ) -> None:
        """Sweep on an interval until asked to stop, whether or not anything is staging."""
        while True:
            await self._tick(backend)
            if await _stopped_within(shutdown, self._interval):
                return

    async def _tick(self, backend: StagingWriterBackend) -> None:
        """Run one idle-cleanup pass, keeping a failed pass visible and retryable."""
        try:
            report = await run_idle_cleanup(backend)
        except (StagingError, SQLAlchemyError) as failure:
            # Reported, not fatal: a bucket that refused one listing must not take this
            # worker's cleanup down until somebody restarts the pod.
            logger.warning("staging.cleanup_failed", error_type=type(failure).__name__)
        else:
            logger.info(
                "staging.cleanup",
                evicted=len(report.reclaimed.evicted),
                protected=len(report.reclaimed.protected),
                orphans_deleted=report.reclaimed.orphans_deleted,
                freed_bytes=report.reclaimed.freed_bytes,
                uploads_aborted=report.uploads_aborted,
            )
        self._cleanup_ticks += 1


async def _stopped_within(shutdown: asyncio.Event, seconds: float) -> bool:
    """Wait up to `seconds`, returning True if shutdown was requested first.

    The interval and the stop signal are one wait, so a drain never has to outlast a
    sleep that was sized for an idle cluster.
    """
    try:
        await asyncio.wait_for(shutdown.wait(), timeout=seconds)
    except TimeoutError:
        return False
    return True


def _start_staging(
    settings: Settings, profile: WorkerProfile, factory: StagingFactory
) -> StagingLifecycle | None:
    """Bring staging up for this profile, or report that none is configured.

    An unset endpoint is not a fallback and is not silent: the process runs without
    staging and any staging call raises, rather than materializing a local directory and
    calling it a cache. Every deployed environment configures an endpoint.

    Raises:
        StagingNotReadyError: The configured bucket is unreachable. Deliberately fatal —
            the worker must not dequeue work whose content it cannot stage.
        StagingRoleMismatchError: The profile and the configured role disagree.
    """
    if not settings.s3_staging_enabled:
        logger.warning(
            "staging.not_configured",
            profile=profile.value,
            detail="no S3 staging endpoint is set; this worker cannot stage content and "
            "will not fall back to filesystem staging",
        )
        return None
    require_staging_role(profile, settings)
    supervisor = factory(settings, profile)
    try:
        supervisor.start()
    except BaseException:
        supervisor.stop()
        raise
    return supervisor


def prepare_services(
    settings: Settings, profile: WorkerProfile, staging: object | None = None
) -> Services:
    """Create the ledger datasource and install what workflow code reaches at run time.

    Must run before `DBOS.launch()`, which rejects a datasource created after it. The
    datasource is created on a throwaway loop and its pool disposed before that loop
    closes, so no connection bound to the dead loop is left for DBOS's loop to pick up.

    Only the I/O profile gets a cipher and provider access, and only when the credential
    key is configured. The CPU profile is never given either: it has no business
    decrypting provider credentials or reaching a provider.

    Args:
        settings: Resolved configuration.
        profile: Which half of the pipeline this process serves.
        staging: The started staging supervisor, or None where staging is not configured.
    """

    async def create() -> Services:
        """Create the datasource and release the connections creation opened."""
        datasource = await create_ledger_datasource(settings)
        await datasource.engine.dispose()
        cipher = (
            CredentialCipher.from_settings(settings)
            if settings.credential_encryption_key is not None and profile is WorkerProfile.IO
            else None
        )
        access = (
            ConnectedAccounts(
                datasource.engine,
                cipher,
                {
                    provider: Pacer(
                        Quota(requests=settings.provider_requests_per_minute, period_seconds=60.0)
                    )
                    for provider in Provider
                },
            )
            if cipher is not None
            else None
        )
        remote = (
            OpenRouterClassifier(settings.openrouter_api_key)
            if settings.openrouter_api_key is not None and profile is WorkerProfile.IO
            else None
        )
        rclone = (
            RcloneAccess(str(settings.rclone_rc_url))
            if settings.rclone_rc_url is not None and profile is WorkerProfile.IO
            else None
        )
        return Services(
            settings=settings,
            datasource=datasource,
            cipher=cipher,
            provider_access=access,
            remote_classifier=remote,
            rclone=rclone,
            remotes=(
                ConnectedRemotes(rclone, datasource.engine, cipher)
                if rclone is not None and cipher is not None
                else None
            ),
            admission=TransferAdmission(
                settings.transfer_scratch_budget_bytes,
                large_threshold=settings.transfer_large_threshold_bytes,
            ),
            staging=staging if isinstance(staging, StagingAccess) else None,
            scratch_budget=local_scratch_budget(settings),
        )

    services = asyncio.run(create())
    install(services)
    return services


@dataclass(frozen=True, slots=True)
class Reconciliation:
    """What one schedule reconciliation wrote, once its transaction commits.

    Attributes:
        latest_version: DBOS's latest application version, read under the authority.
        applied: Whether this worker was on that version and so applied its schedules.
        deleted: The stored schedules it deleted because no profile declares them.
    """

    latest_version: str
    applied: bool
    deleted: tuple[StoredSchedule, ...] = ()


def reconcile_schedules(
    authority: ScheduleAuthority,
    *,
    version: str,
    schedules: Sequence[ScheduleInput],
    declared: frozenset[str],
) -> Reconciliation:
    """Apply `schedules` and delete stored ones outside `declared`, if `version` is the latest.

    Every read and write goes through `authority`, so all of it commits with the
    transaction that holds the lock or none of it does.
    """
    latest = authority.latest_version()
    if latest != version:
        return Reconciliation(latest_version=latest, applied=False)
    authority.apply(schedules)
    retired = tuple(stored for stored in authority.stored() if stored.name not in declared)
    for stored in retired:
        authority.delete(stored.name)
    return Reconciliation(latest_version=latest, applied=True, deleted=retired)


def apply_schedules(profile: WorkerProfile, settings: Settings) -> None:
    """Upsert this profile's persisted schedules, then delete undeclared ones. Call after launch.

    A stored schedule (this application's, or one no application has claimed, as
    `DBOS.list_schedules` returns them) that no profile declares any more is deleted, so
    a renamed or dropped pass stops firing instead of ticking a workflow nobody runs. Only
    a profile that applies schedules deletes any, and it keeps every profile's names, not
    just its own. A crash before the commit leaves the deletions for the next start.

    Schedule rows are shared by every version of the application, so only a worker on the
    deployment's latest version (DBOS's, by registration time) touches them: DBOS enqueues
    every tick on that version, so its declarations are the ones that count. A replica of
    an older version restarting mid-rollout would otherwise delete a schedule the newer
    one added or rewrite one it changed, and the newer workers apply only at startup.
    Every build is its own version: a dev image carries its build in the version
    (`dbos_app.application_version`), so an older dev build is never the latest either.

    The version check, the upsert and the deletions run in one system-database
    transaction holding a lock shared by every worker (`runtime/schedule_authority.py`).
    A worker that passes the check and stalls keeps the next one waiting; one whose
    backend is lost meanwhile loses the lock and its pending writes together, so it
    cannot apply or delete after a newer version has registered and applied its own.
    Outcomes are logged after the commit, so a logged deletion happened.

    Promotion only changes which version is latest; it applies nothing. After a rollback,
    promote the restored version with `python -m curate_d.runtime.promote <version>`
    before its workers start, or restart them afterwards. Workers never promote
    themselves: one cannot tell a rollback from newer replicas crash-looping mid-rollout.

    Raises:
        ScheduleAuthorityUnavailableError: Another worker held the lock past the timeout.
        sqlalchemy.exc.DBAPIError: The transaction failed, its backend lost included;
            nothing it wrote was committed.
    """
    schedules = schedules_for(profile, settings)
    if not schedules:
        return
    declared = declared_schedule_names(settings)
    with schedule_authority() as authority:
        outcome = reconcile_schedules(
            authority, version=DBOS.application_version, schedules=schedules, declared=declared
        )
    if not outcome.applied:
        logger.warning(
            "worker.schedules_left_to_latest_version",
            version=DBOS.application_version,
            latest_version=outcome.latest_version,
        )
    for stored in outcome.deleted:
        logger.info("worker.schedule_deleted", schedule=stored.name, workflow=stored.workflow)


def _install_shutdown_handler(stop: threading.Event) -> None:
    """Set `stop` on SIGTERM or SIGINT so Kubernetes gets a clean drain rather than a kill."""

    def handle(signal_number: int, _frame: FrameType | None) -> None:
        """Log the received signal and set the stop event to begin a clean drain."""
        logger.info("worker.shutdown_requested", signal=signal.Signals(signal_number).name)
        stop.set()

    signal.signal(signal.SIGTERM, handle)
    signal.signal(signal.SIGINT, handle)


def _drain_dbos(timeout_seconds: int) -> bool:
    """Shut DBOS down after up to `timeout_seconds` for in-flight workflows.

    DBOS stops waiting at the deadline but does not stop the workflow threads; the
    interpreter joins them at exit. Their executor identity is still in use until then,
    so the caller keeps the lease unless nothing was left running.

    Returns:
        Whether no workflow was still running when DBOS shut down.
    """
    try:
        # Private: DBOS's own drain loop counts this set. Pinned in test_worker_main.py.
        active = _get_dbos_instance()._active_workflows_set
    except DBOSException:
        active = None
    DBOS.destroy(workflow_completion_timeout_sec=timeout_seconds)
    return active is None or not active.activeList()


def run(
    settings: Settings,
    profile: WorkerProfile,
    *,
    stop: threading.Event | None = None,
    block: bool = True,
    staging_factory: StagingFactory = StagingSupervisor,
    lease_factory: LeaseFactory | None = None,
) -> None:
    """Launch a worker for one profile and serve until asked to stop.

    Args:
        settings: Resolved configuration.
        profile: Which half of the pipeline this process serves.
        stop: Set to begin a drain. A fresh event with signal handlers installed on it
            when omitted.
        block: False returns once the worker is serving, tearing staging back down and
            releasing the lease on the way out. For tests; a deployed worker always
            blocks.
        staging_factory: Builds the staging supervisor, substituted by tests that must
            not open an event loop.
        lease_factory: Builds the executor and version lease; a lease on the configured
            DBOS system database when omitted. Substituted by tests that must not connect.

    Raises:
        ExecutorIdentityError: No stable executor identity is configured.
        VersionRegistryError: This build's version could not be registered in
            `lease_versions`. DBOS is never launched.
        ExecutorLeaseUnavailableError: Another live process holds this identity's lease,
            or a recovery sweep holds this version exclusively, past the acquire budget.
            DBOS is never launched.
        ExecutorLeaseLostError: The lease was lost while serving. DBOS was shut down
            without waiting for in-flight workflows; the caller must exit at once.
        StagingNotReadyError: Staging is configured but unreachable. No queue is
            listened to and no workflow is dequeued.
        StagingRoleMismatchError: The profile and the configured staging role disagree.
    """
    executor_id = resolve_executor_id()
    logger.info(
        "worker.starting",
        profile=profile.value,
        executor_id=executor_id,
        queues=[queue.value for queue in profile.queues],
    )

    shutdown = stop if stop is not None else threading.Event()
    lease_lost = threading.Event()

    def on_lease_lost() -> None:
        """Wake the main thread to stop at once; the identity may already be in use."""
        lease_lost.set()
        shutdown.set()

    # Before DBOS: a worker that cannot reach staging must not dequeue at all, and there
    # is no second backend for it to try.
    staging = _start_staging(settings, profile, staging_factory)
    lease: ExecutorLease | None = None
    launched = False
    drained = False
    try:
        # Registered first: the recovery sweep treats an unregistered version as one that
        # predates the version lease, and leaves its rows alone.
        version = dbos_app.APPLICATION_VERSION
        register_version(settings, version)
        # Before launch, because launch runs startup recovery for this identity: a second
        # live process doing that would claim the first one's in-flight workflows. The
        # version key, held shared beside it, tells the sweep this version is live; it is
        # kept with the executor key until exit, even when the drain does not finish.
        lease = (lease_factory or ExecutorLease.for_settings)(
            settings, executor_id, version=version
        )
        lease.acquire(stop=shutdown)
        lease.monitor(on_lease_lost)
        prepare_services(settings, profile, staging)
        configure(settings, executor_id=executor_id)
        DBOS.listen_queues([queue.value for queue in profile.queues])
        DBOS.launch()
        launched = True
        register_all()
        apply_schedules(profile, settings)

        logger.info("worker.ready", profile=profile.value, executor_id=executor_id)
        if not block:
            return

        _install_shutdown_handler(shutdown)
        shutdown.wait()
        if lease_lost.is_set():
            raise ExecutorLeaseLostError(
                f"lost the executor lease for {executor_id!r}; stopping without draining"
            )
    finally:
        try:
            # Let workflows already in flight finish rather than orphaning them as PENDING
            # on a version this deployment is about to retire.
            if launched and block:
                drained = _drain_dbos(0 if lease_lost.is_set() else DRAIN_SECONDS)
        finally:
            try:
                # After the drain: a workflow finishing its last step may still be reading
                # staged content, and tearing the loop down underneath it would fail work
                # that was about to succeed.
                if staging is not None:
                    staging.stop()
            finally:
                # Last, and only once nothing runs under this identity: releasing earlier
                # would let a successor recover workflows still executing here. A retained
                # lease is released when the process exits and its connection closes.
                if lease is not None:
                    if drained or not (launched and block):
                        lease.release()
                    else:
                        logger.warning("worker.lease_retained_until_exit", executor_id=executor_id)
    logger.info("worker.stopped", profile=profile.value)


def main() -> int:
    """Configure logging and run the worker for the profile named in the environment.

    A lost executor lease exits immediately with `EXIT_LEASE_LOST` through `os._exit`: a
    normal exit would join DBOS's executor threads and so keep running workflows under
    an identity another process may now hold.

    Returns:
        0 once `run` returns after a signalled drain.
    """
    configure_logging(level=logging.INFO)
    try:
        run(get_settings(), resolve_profile())
    except ExecutorLeaseLostError as error:
        logger.critical("worker.lease_lost_exiting", error=str(error), status=EXIT_LEASE_LOST)
        sys.stderr.flush()
        os._exit(EXIT_LEASE_LOST)
    return 0


if __name__ == "__main__":
    sys.exit(main())
