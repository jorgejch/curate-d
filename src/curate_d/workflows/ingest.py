"""Ingest: from a listed file to a fingerprinted content object.

Two workflows, one per profile. `ingest_source` runs on the I/O profile's `stage` queue:
it fetches the file from its provider and publishes it into staging, then hands the
staged version to `fingerprint_source` on the CPU profile's `fingerprint` queue, which
reads a verified snapshot and records the content object. Neither profile does the
other's half: the I/O profile decodes nothing, and the CPU profile reaches no provider.

Every ledger write is a datasource transaction; provider calls and staging operations are
steps. Once nothing else will read a staged version, `fingerprint_source` hands its release
to the I/O profile (`staged_release.py`); the copy classification reads is classification's
to release. Failures take one of four routes:

- The provider cannot deliver the file, or its bytes cannot be read: the source is marked
  failed with the reason, no content object is created, and nothing else waits on it.
- What arrived is not what the scan listed: the source is marked failed as a source
  change, with both observed values, for the next scan to pick up as a new version.
- Staging is full: the workflow waits durably and tries again, for a bounded time.
- The provider refuses the credentials: the connection is flagged and the source left
  for the next scan after a person re-authorizes.
"""

from datetime import datetime
from enum import StrEnum
from typing import Final
from uuid import UUID

from dbos import DBOS, PropagateOtelContext, SetEnqueueOptions, SetWorkflowID
from pydantic import Field

from curate_d.domain import AuditEntryType, DomainModel, Provider, SourceObject, SourceObjectId
from curate_d.ingest.checks import content_fingerprint
from curate_d.ingest.pipeline import (
    FingerprintRecord,
    Inspection,
    StagedRef,
    StageOutcome,
    StageResult,
    inspect_staged,
    is_current,
    record_fingerprint,
    record_source_failure,
    stage_source,
)
from curate_d.ledger import AuditRepository, ContentObjectRepository, SourceObjectRepository
from curate_d.observability import get_logger
from curate_d.providers.connections import ConnectionRepository
from curate_d.providers.errors import (
    ConnectionRequiresReauthenticationError,
    CredentialsRejectedError,
    ProviderError,
)
from curate_d.runtime.queues import QueueName
from curate_d.runtime.services import ServicesNotInstalledError, current
from curate_d.runtime.workflow_ids import classification_workflow_id, ingest_workflow_id
from curate_d.staging.errors import (
    StagedEntryMissingError,
    StagedEntrySupersededError,
    StagingCapacityExhaustedError,
    StagingError,
)
from curate_d.workflows.classify import enqueue_classification
from curate_d.workflows.staged_release import enqueue_staged_release, reads_staged

logger = get_logger(__name__)

FINGERPRINT_PREFIX: Final = "fingerprint"
# Staging full: wait, doubling from `staging_capacity_retry_seconds` up to fifteen times
# that; with the one-minute default, waits top out at fifteen minutes and total about
# three hours.
CAPACITY_WAITS: Final = 14
_MAX_WAIT_FACTOR: Final = 15
# An entry evicted before the CPU profile read it is restaged this many times.
MAX_RESTAGES: Final = 2


class IngestRequest(DomainModel):
    """A scan's hand-off: this source, at this observed version, needs ingesting."""

    source_object_id: UUID
    provider: Provider
    observed: str = Field(min_length=1)
    attempt: int = Field(default=0, ge=0)
    generation: str = Field(
        default="",
        pattern=r"^[A-Za-z0-9_.+-]*$",
        description="Distinguishes a deliberate re-ingest of the same version, such as "
        "one a full rescan asks for, from a repeated request that should collapse.",
    )


class IngestOutcome(StrEnum):
    """How an ingest ended."""

    STAGED = "staged"
    SOURCE_CHANGED = "source_changed"
    UNREADABLE = "unreadable"
    BLOCKED = "blocked"
    SUPERSEDED = "superseded"
    DEFERRED = "deferred"


class FingerprintOutcome(StrEnum):
    """How fingerprinting ended."""

    FINGERPRINTED = "fingerprinted"
    SUPERSEDED = "superseded"
    RESTAGED = "restaged"
    UNREADABLE = "unreadable"


def _version_tag(observed: str, attempt: int, generation: str) -> str:
    """The observed version, qualified by generation and restage attempt."""
    tag = observed if not generation else f"{observed}@{generation}"
    return tag if attempt == 0 else f"{tag}#{attempt}"


async def enqueue_ingest(requests: tuple[IngestRequest, ...] | list[IngestRequest]) -> None:
    """Enqueue ingests on their providers' partitions; a repeated request collapses."""
    for request in requests:
        workflow_id = ingest_workflow_id(
            str(request.source_object_id),
            _version_tag(request.observed, request.attempt, request.generation),
        )
        # Carries this span to the dequeued child; without it the child roots its own trace.
        with (
            PropagateOtelContext(),
            SetWorkflowID(workflow_id),
            SetEnqueueOptions(queue_partition_key=request.provider.value),
        ):
            await DBOS.enqueue_workflow_async(
                QueueName.STAGE.value,
                ingest_source,
                request.source_object_id,
                request.observed,
                request.attempt,
                request.generation,
            )


# Steps.


@DBOS.step()
async def _ingest_now_step() -> datetime:
    """Read the clock once, so a replay records the same time."""
    return current().clock()


@DBOS.step()
async def _source_step(source_object_id: UUID) -> SourceObject | None:
    """The source as the ledger holds it now."""
    async with current().datasource.engine.connect() as connection:
        return await SourceObjectRepository(connection).get(SourceObjectId(source_object_id))


@DBOS.step()
async def _stage_step(source: SourceObject) -> StageResult:
    """Fetch from the provider onto local scratch, then publish on the staging loop."""
    services = current()
    staging = services.require_staging()
    writer = staging.writer
    if writer is None:
        raise ServicesNotInstalledError("this process has no staging writer")
    files = await services.require_provider_access().files(source.provider, source.account)
    return await stage_source(
        files,
        writer,
        source,
        scratch_root=services.settings.staging_local_scratch_root,
        budget=services.require_scratch_budget(),
        publish_on_staging_loop=staging.run,
    )


@DBOS.step()
async def _inspect_step(staged: StagedRef) -> Inspection:
    """Read a verified snapshot and inspect it, on the staging loop."""
    staging = current().require_staging()
    reader = staging.reader
    if reader is None:
        raise ServicesNotInstalledError("this process has no staging reader")
    return await staging.run(inspect_staged(reader.reader, staged))


# Transactions.


async def _failure_tx(
    source_object_id: UUID, observed: str, reason: str, entry_type: AuditEntryType, now: datetime
) -> bool:
    """Mark the source failed and audit why."""
    connection = await current().datasource.sql_session().connection()
    return await record_source_failure(
        SourceObjectRepository(connection),
        AuditRepository(connection),
        source_object_id=source_object_id,
        observed=observed,
        reason=reason,
        entry_type=entry_type,
        now=now,
    )


async def _reauthenticate_tx(error: CredentialsRejectedError) -> None:
    """Flag the connection whose credentials the provider refused."""
    services = current()
    connection = await services.datasource.sql_session().connection()
    await ConnectionRepository(
        connection, services.require_cipher()
    ).mark_requires_reauthentication(error.provider, error.account, error.reason)


async def _fingerprint_tx(
    source_object_id: UUID, observed: str, staged: StagedRef, inspection: Inspection
) -> FingerprintRecord | None:
    """Create or find the content object and link the source to it."""
    connection = await current().datasource.sql_session().connection()
    return await record_fingerprint(
        SourceObjectRepository(connection),
        ContentObjectRepository(connection),
        source_object_id=source_object_id,
        observed=observed,
        staged=staged,
        inspection=inspection,
    )


async def _fail(
    source_object_id: UUID, observed: str, reason: str, entry_type: AuditEntryType
) -> None:
    """Record a failure through the ledger datasource."""
    await current().datasource.run_tx_step_async(
        {"name": "record_source_failure"},
        _failure_tx,
        source_object_id,
        observed,
        reason,
        entry_type,
        await _ingest_now_step(),
    )


# Workflows.


@DBOS.workflow()
async def ingest_source(
    source_object_id: UUID, observed: str, attempt: int = 0, generation: str = ""
) -> IngestOutcome:
    """Fetch and stage one version of a source, then hand it to fingerprinting."""
    source = await _source_step(source_object_id)
    if source is None or not is_current(source, observed):
        return IngestOutcome.SUPERSEDED
    first_wait = current().settings.staging_capacity_retry_seconds
    wait = first_wait
    result: StageResult | None = None
    for _ in range(CAPACITY_WAITS):
        try:
            result = await _stage_step(source)
            break
        except StagingCapacityExhaustedError:
            await DBOS.sleep_async(wait)
            wait = min(wait * 2, first_wait * _MAX_WAIT_FACTOR)
        except CredentialsRejectedError as rejected:
            await current().datasource.run_tx_step_async(
                {"name": "flag_connection"}, _reauthenticate_tx, rejected
            )
            return IngestOutcome.BLOCKED
        except ConnectionRequiresReauthenticationError:
            return IngestOutcome.BLOCKED
        except ProviderError as error:
            await _fail(source_object_id, observed, str(error), AuditEntryType.SOURCE_READ_FAILED)
            return IngestOutcome.UNREADABLE
        except StagingError as error:
            await _fail(
                source_object_id,
                observed,
                f"staging failed: {error}",
                AuditEntryType.SOURCE_READ_FAILED,
            )
            return IngestOutcome.UNREADABLE
    if result is None:
        await _fail(
            source_object_id,
            observed,
            "staging capacity stayed exhausted; the next full rescan retries",
            AuditEntryType.SOURCE_READ_FAILED,
        )
        return IngestOutcome.DEFERRED
    if result.outcome is StageOutcome.SOURCE_CHANGED or result.staged is None:
        await _fail(
            source_object_id,
            observed,
            result.reason or "source changed since it was listed",
            AuditEntryType.SOURCE_DISCREPANCY_DETECTED,
        )
        return IngestOutcome.SOURCE_CHANGED
    tag = _version_tag(observed, attempt, generation)
    with PropagateOtelContext(), SetWorkflowID(f"{FINGERPRINT_PREFIX}:{source_object_id}:{tag}"):
        await DBOS.enqueue_workflow_async(
            QueueName.FINGERPRINT.value,
            fingerprint_source,
            source_object_id,
            observed,
            result.staged,
            attempt,
            generation,
        )
    return IngestOutcome.STAGED


@DBOS.workflow()
async def fingerprint_source(
    source_object_id: UUID,
    observed: str,
    staged: StagedRef,
    attempt: int = 0,
    generation: str = "",
) -> FingerprintOutcome:
    """Inspect a staged version and record the content object it is."""
    try:
        inspection = await _inspect_step(staged)
    except StagedEntrySupersededError:
        return FingerprintOutcome.SUPERSEDED
    except StagedEntryMissingError:
        source = await _source_step(source_object_id)
        if source is None or not is_current(source, observed):
            return FingerprintOutcome.SUPERSEDED
        if attempt >= MAX_RESTAGES:
            await _fail(
                source_object_id,
                observed,
                f"staged content was evicted before it was read, {attempt + 1} times",
                AuditEntryType.SOURCE_READ_FAILED,
            )
            return FingerprintOutcome.UNREADABLE
        await enqueue_ingest(
            [
                IngestRequest(
                    source_object_id=source_object_id,
                    provider=source.provider,
                    observed=observed,
                    attempt=attempt + 1,
                    generation=generation,
                )
            ]
        )
        return FingerprintOutcome.RESTAGED
    except StagingError as error:
        await _fail(
            source_object_id,
            observed,
            f"staged content could not be read: {error}",
            AuditEntryType.SOURCE_READ_FAILED,
        )
        # The source is failed for this version; the next scan's ingest stages afresh.
        await enqueue_staged_release(staged)
        return FingerprintOutcome.UNREADABLE
    record = await current().datasource.run_tx_step_async(
        {"name": "record_fingerprint"},
        _fingerprint_tx,
        source_object_id,
        observed,
        staged,
        inspection,
    )
    if record is None:
        # The source moved on; its newer version stages an attempt of its own.
        await enqueue_staged_release(staged)
        return FingerprintOutcome.SUPERSEDED
    logger.info(
        "ingest.fingerprinted",
        source_object_id=str(source_object_id),
        content_object_id=str(record.content_object_id),
        new_content=record.newly_created,
    )
    fingerprint = content_fingerprint(staged.sha256)
    await enqueue_classification(fingerprint, record.content_object_id, staged)
    # Identical content found at several sources is classified once, from whichever copy
    # reached classification first; that copy is classification's to release.
    if staged.attempt_id is not None and not await reads_staged(
        classification_workflow_id(fingerprint), staged
    ):
        await enqueue_staged_release(staged)
    return FingerprintOutcome.FINGERPRINTED
