"""Releasing a staged version once the workflows that read it are done.

`fingerprint_source` and `classify_content` read staged bytes on the CPU profile, which
holds no release authority, so each hands the release of the version it read to
`release_staged_entry` on the I/O profile's `maintenance` queue. The release names the
upload attempt that was read: a key restaged since holds a newer version, which it leaves
alone.

A release that fails changes nothing the consumer reported. What it leaves behind is the
idle sweep's: unregistered bytes on its next reconciliation, a registration once it ages
out.
"""

from enum import StrEnum
from typing import Final
from uuid import UUID

from dbos import DBOS, PropagateOtelContext, SetEnqueueOptions, SetWorkflowID
from dbos import error as dbos_error

from curate_d.ingest.pipeline import StagedRef
from curate_d.observability import get_logger
from curate_d.runtime.queues import QueueName
from curate_d.runtime.services import ServicesNotInstalledError, current
from curate_d.staging.errors import StagingError
from curate_d.staging.keys import StagingKey

logger = get_logger(__name__)

RELEASE_PREFIX: Final = "release-staged"
# DBOS runs un-prioritised work first, so scheduled housekeeping on `maintenance` never
# waits behind a backlog of releases.
RELEASE_PRIORITY: Final = 100
# Attempts at a release whose index or store call failed, 1 s and then 2 s apart.
RELEASE_ATTEMPTS: Final = 3


class ReleaseOutcome(StrEnum):
    """How a release ended."""

    RELEASED = "released"
    LEFT = "left"
    FAILED = "failed"


def release_workflow_id(staged: StagedRef) -> str:
    """Return the identity of releasing one staged attempt; a repeated enqueue collapses.

    Raises:
        ValueError: `staged` names no attempt, so there is nothing to identify.
    """
    if staged.attempt_id is None:
        raise ValueError(f"staged version of {staged.key} names no attempt to release")
    return f"{RELEASE_PREFIX}:{staged.key}:{staged.attempt_id.hex}"


async def enqueue_staged_release(staged: StagedRef) -> None:
    """Hand the release of `staged` to the I/O profile; nothing for an unknown attempt.

    A reference that names no attempt was built from the ledger or pickled by an earlier
    build. Its key may hold a version another reader still needs, so it is left to age.
    """
    if staged.attempt_id is None:
        return
    with (
        PropagateOtelContext(),
        SetWorkflowID(release_workflow_id(staged)),
        SetEnqueueOptions(priority=RELEASE_PRIORITY),
    ):
        await DBOS.enqueue_workflow_async(
            QueueName.MAINTENANCE.value, release_staged_entry, staged.key, staged.attempt_id
        )


async def reads_staged(workflow_id: str, staged: StagedRef) -> bool:
    """Return whether workflow `workflow_id` was handed `staged`'s attempt to read.

    A workflow's inputs are fixed when it is created, so the answer holds for as long as
    the workflow runs. A workflow pruned from history finished long ago and reads nothing
    more. DBOS records the status read as a step, so a replay sees the same answer.
    """
    status = await DBOS.get_workflow_status_async(workflow_id)
    if status is None or status.input is None or staged.attempt_id is None:
        return False
    handed = (*status.input["args"], *status.input["kwargs"].values())
    return any(
        isinstance(value, StagedRef) and value.attempt_id == staged.attempt_id for value in handed
    )


def _retryable(error: BaseException) -> bool:
    """Return whether a failed release is worth another attempt.

    Staging failures are: the index or the store may answer next time. Anything else, such
    as a process with no staging writer, is a deployment fault no retry repairs.
    """
    return isinstance(error, StagingError)


@DBOS.step(
    retries_allowed=True,
    max_attempts=RELEASE_ATTEMPTS,
    interval_seconds=1.0,
    backoff_rate=2.0,
    should_retry=_retryable,
)
async def _release_step(key: str, attempt_id: UUID) -> ReleaseOutcome:
    """Release one staged attempt on the staging loop.

    Raises:
        ServicesNotInstalledError: this process has no staging writer.
        StagingError: the index or the store refused; DBOS retries it.
    """
    staging = current().require_staging()
    writer = staging.writer
    if writer is None:
        raise ServicesNotInstalledError("this process has no staging writer")
    report = await staging.run(writer.occupancy.release(StagingKey(key), attempt_id=attempt_id))
    logger.info(
        "staging.released",
        staging_key=key,
        attempt_id=str(attempt_id),
        deleted=report.deleted,
        freed_bytes=report.freed_bytes,
    )
    return ReleaseOutcome.RELEASED if report.deleted else ReleaseOutcome.LEFT


async def _release_reporting_failure(key: str, attempt_id: UUID) -> ReleaseOutcome:
    """Run the release step, turning exhausted retries into a logged FAILED outcome."""
    try:
        return await _release_step(key, attempt_id)
    except dbos_error.DBOSMaxStepRetriesExceeded as exhausted:
        logger.warning(
            "staging.release_failed",
            staging_key=key,
            attempt_id=str(attempt_id),
            attempts=RELEASE_ATTEMPTS,
            error_type=type(exhausted.errors[-1]).__name__ if exhausted.errors else None,
        )
        return ReleaseOutcome.FAILED


@DBOS.workflow()
async def release_staged_entry(key: str, attempt_id: UUID) -> ReleaseOutcome:
    """Release one staged attempt its readers are done with; a failure is reported, not raised."""
    return await _release_reporting_failure(key, attempt_id)
