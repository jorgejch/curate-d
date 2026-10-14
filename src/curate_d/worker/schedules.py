"""Reconcile the application's persisted schedules at startup and periodically after.

Schedule rows are shared by every worker and every version, so only a worker on DBOS's
latest version writes them, one at a time under the schedule authority
(`runtime/schedule_authority.py`). DBOS's schedule writes commit on its own pool, not
under that lock, so a reconciliation verifies afterwards that it still held the
authority and that the latest version had not moved, and logs
`worker.schedule_reconciliation_unverified` at ERROR when it cannot. It never fails on
that: `ScheduleReconciler` reconciles again every `schedule_reconciliation_interval_seconds`,
so whichever worker is on the latest version repairs what a stale writer left.

The reconciler is a thread, not a persisted schedule: a schedule that re-applies the
schedules would be self-referential, `DBOS.apply_schedules` refuses a workflow context,
and the reconciliation's several un-checkpointed writes would have to sit in one step.
"""

import threading
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

import psycopg
from dbos import DBOS, ScheduleInput
from dbos import error as dbos_error
from sqlalchemy.exc import SQLAlchemyError

from curate_d.config import Settings
from curate_d.observability import get_logger
from curate_d.runtime.profiles import WorkerProfile
from curate_d.runtime.schedule_authority import (
    ScheduleAuthority,
    ScheduleAuthorityUnavailableError,
    StoredSchedule,
    schedule_authority,
)
from curate_d.workflows.registry import declared_schedule_names, schedules_for

logger = get_logger(__name__)

# How long shutdown waits for a reconciliation in progress. A tick finishes within a few
# statements once it holds the authority; past this the thread is a daemon and DBOS is
# shut down underneath it, which fails the tick, logged, rather than the shutdown.
RECONCILER_STOP_SECONDS: Final = 5.0

# Failures a tick expects from a database or a holder that is away for a while; anything
# else is a defect and is logged as one, but neither stops the thread.
_TRANSIENT: Final = (
    ScheduleAuthorityUnavailableError,
    dbos_error.DBOSException,
    SQLAlchemyError,
    psycopg.Error,
)


@dataclass(frozen=True, slots=True)
class Reconciliation:
    """What one schedule reconciliation wrote, and whether its authority held throughout.

    Attributes:
        latest_version: DBOS's latest application version, read under the authority
            before writing.
        applied: Whether this worker was on that version and so applied its schedules.
        deleted: The stored schedules it deleted because no profile declares them.
        verified: Whether, after writing, the authority was still held and the latest
            version unchanged. True when nothing was written.
        latest_after: The latest version re-read after writing, or None if nothing was
            written or the re-read failed.
        held: Whether the authority was still held after writing. True when nothing was
            written.
    """

    latest_version: str
    applied: bool
    deleted: tuple[StoredSchedule, ...] = ()
    verified: bool = True
    latest_after: str | None = None
    held: bool = True


def reconcile_schedules(
    authority: ScheduleAuthority,
    *,
    version: str,
    schedules: Sequence[ScheduleInput],
    declared: frozenset[str],
) -> Reconciliation:
    """Apply `schedules` and delete stored ones outside `declared`, if `version` is the latest.

    The writes commit as they are made. Afterwards the authority and the latest version
    are checked again: a writer that lost the lock, or whose version stopped being the
    latest, may have overwritten a newer worker's rows, and the result says so.
    """
    latest = authority.latest_version()
    if latest != version:
        return Reconciliation(latest_version=latest, applied=False)
    authority.apply(schedules)
    retired = tuple(stored for stored in authority.stored() if stored.name not in declared)
    for stored in retired:
        authority.delete(stored.name)
    held = authority.still_held()
    latest_after = _latest_after(authority)
    return Reconciliation(
        latest_version=latest,
        applied=True,
        deleted=retired,
        verified=held and latest_after == version,
        latest_after=latest_after,
        held=held,
    )


def _latest_after(authority: ScheduleAuthority) -> str | None:
    """Re-read the latest version after writing, or return None if that fails."""
    try:
        return authority.latest_version()
    except _TRANSIENT as error:
        logger.warning("worker.schedule_latest_unreadable", error_type=type(error).__name__)
        return None


def apply_schedules(profile: WorkerProfile, settings: Settings) -> None:
    """Upsert this profile's persisted schedules, then delete undeclared ones. Call after launch.

    A stored schedule of this application that no profile declares any more is deleted,
    so a renamed or dropped pass stops firing instead of ticking a workflow nobody runs.
    Only a profile that applies schedules deletes any, and it keeps every profile's
    names, not just its own. `DBOS.apply_schedules` keeps a stored schedule's status, so
    one an operator paused stays paused.

    Schedule rows are shared by every version of the application, so only a worker on the
    deployment's latest version (DBOS's, by registration time) touches them: DBOS enqueues
    every tick on that version, so its declarations are the ones that count. A replica of
    an older version restarting mid-rollout would otherwise delete a schedule the newer
    one added or rewrite one it changed. Every build is its own version: a dev image
    carries its build in the version (`dbos_app.application_version`), so an older dev
    build is never the latest either.

    The version check and the writes run under a lock shared by every worker
    (`runtime/schedule_authority.py`), but the writes commit outside it. A worker that
    loses the lock or sees the latest version move while writing logs
    `worker.schedule_reconciliation_unverified` at ERROR and carries on; the next
    reconciliation by a worker on the latest version repairs the rows.

    Promotion only changes which version is latest; it applies nothing. After a rollback,
    promote the restored version with `python -m curate_d.runtime.promote <version>`: its
    workers re-apply within `schedule_reconciliation_interval_seconds`, or at once on a
    restart. Workers never promote themselves: one cannot tell a rollback from newer
    replicas crash-looping mid-rollout.

    Raises:
        ScheduleAuthorityUnavailableError: Another worker held the lock past the timeout.
        dbos.error.DBOSException: DBOS refused a declaration or has no latest version.
        sqlalchemy.exc.SQLAlchemyError: A DBOS read or write failed.
        psycopg.Error: The authority's session failed.
    """
    schedules = schedules_for(profile, settings)
    if not schedules:
        return
    declared = declared_schedule_names(settings)
    version = DBOS.application_version
    with schedule_authority(settings) as authority:
        outcome = reconcile_schedules(
            authority, version=version, schedules=schedules, declared=declared
        )
    if not outcome.applied:
        logger.warning(
            "worker.schedules_left_to_latest_version",
            version=version,
            latest_version=outcome.latest_version,
        )
    for stored in outcome.deleted:
        logger.info("worker.schedule_deleted", schedule=stored.name, workflow=stored.workflow)
    if not outcome.verified:
        logger.error(
            "worker.schedule_reconciliation_unverified",
            version=version,
            latest_before=outcome.latest_version,
            latest_after=outcome.latest_after,
            held=outcome.held,
            applied=[entry["schedule_name"] for entry in schedules],
            deleted=[stored.name for stored in outcome.deleted],
        )


class ScheduleReconciler:
    """A daemon thread re-applying a profile's schedules every interval until stopped.

    The first reconciliation runs one interval after `start`, the startup one having
    just run. A failed reconciliation is logged and the next interval tries again.
    """

    def __init__(
        self,
        profile: WorkerProfile,
        settings: Settings,
        *,
        stop: threading.Event,
        interval_seconds: float | None = None,
    ) -> None:
        """Bind the reconciler to a profile and the worker's stop event.

        Args:
            profile: The profile whose schedules are re-applied.
            settings: Resolved configuration.
            stop: The worker's shutdown event; setting it ends the thread.
            interval_seconds: Time between reconciliations;
                `settings.schedule_reconciliation_interval_seconds` when omitted.
        """
        self._profile = profile
        self._settings = settings
        self._stop = stop
        self._interval = (
            settings.schedule_reconciliation_interval_seconds
            if interval_seconds is None
            else interval_seconds
        )
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the thread.

        Raises:
            RuntimeError: The reconciler was already started.
        """
        if self._thread is not None:
            raise RuntimeError("a schedule reconciler can only be started once")
        self._thread = threading.Thread(target=self._serve, name="schedule-reconciler", daemon=True)
        self._thread.start()

    def join(self, timeout: float) -> bool:
        """Wait up to `timeout` seconds for the thread to end; return whether it has.

        Does not stop it: set the stop event first.
        """
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        if thread.is_alive():
            logger.warning("worker.schedule_reconciler_still_running", timeout_seconds=timeout)
            return False
        return True

    def _serve(self) -> None:
        """Reconcile once per interval until the stop event is set."""
        while not self._stop.wait(self._interval):
            self._tick()

    def _tick(self) -> None:
        """Reconcile once, logging rather than raising any failure."""
        try:
            apply_schedules(self._profile, self._settings)
        except _TRANSIENT as error:
            logger.warning(
                "worker.schedule_reconciliation_failed",
                error_type=type(error).__name__,
                error=str(error),
            )
        except Exception as error:  # A defect must not end the healing loop either.
            logger.error(
                "worker.schedule_reconciliation_failed",
                error_type=type(error).__name__,
                error=str(error),
                exc_info=True,
            )


def start_schedule_reconciler(
    profile: WorkerProfile, settings: Settings, *, stop: threading.Event
) -> ScheduleReconciler | None:
    """Start re-applying `profile`'s schedules periodically, or return None if it has none."""
    if not schedules_for(profile, settings):
        return None
    reconciler = ScheduleReconciler(profile, settings, stop=stop)
    reconciler.start()
    return reconciler
