"""Placement: one content object to one destination, copied, verified, registered.

`place_content` runs on the transfer queue for the file's size class. It records that the
copy is starting, copies through the rclone sidecar under the transfer byte budget, and
records the evidence both sides left; then it hands over to `verify_placement` on the
`verify` queue, which assesses that evidence and registers the placement, distrusts the
copy, or sends a changed source back to be fingerprinted.

An interrupted placement is run again by DBOS recovery: the copy overwrites any partial
destination content, and nothing is registered until verification says so. A transfer
that cannot be admitted, or that runs out of scratch, waits durably and retries; a copy
that fails outright is recorded as failed, and the next reconciliation pass starts a new
attempt under a new workflow identity.
"""

from datetime import datetime
from enum import StrEnum
from typing import Final

from dbos import DBOS, SetEnqueueOptions, SetWorkflowID
from pydantic import Field

from curate_d.domain import (
    ContentObjectId,
    DestinationConfig,
    DomainModel,
    Placement,
    PlacementId,
    SourceObject,
    SourceObjectState,
    StrongFingerprint,
)
from curate_d.ingest.pipeline import observed_marker
from curate_d.ledger import PlacementRepository, SourceObjectRepository
from curate_d.observability import get_logger
from curate_d.placement.admission import (
    AdmissionDeferredError,
    SizeClass,
    TransferTooLargeError,
    size_class,
)
from curate_d.placement.pipeline import PlacementStore, conclude, defer, fail, start
from curate_d.placement.rclone import RcloneError
from curate_d.placement.transfer import (
    ScratchExhaustedError,
    TransferFailedError,
    TransferSpec,
    copy_and_collect,
)
from curate_d.placement.verification import (
    Assessment,
    TransferEvidence,
    assess,
    source_changed,
)
from curate_d.providers.errors import ProviderError
from curate_d.runtime.queues import QueueName
from curate_d.runtime.services import current
from curate_d.runtime.workflow_ids import placement_attempt_workflow_id
from curate_d.workflows.ingest import IngestRequest, enqueue_ingest

logger = get_logger(__name__)

VERIFY_PREFIX: Final = "verify"
# Deferred or scratch-starved transfers wait and retry this many times, doubling from
# `transfer_retry_seconds` up to fifteen times it, before being recorded as failed.
TRANSFER_WAITS: Final = 10
_MAX_WAIT_FACTOR: Final = 15


class PlacementRequest(DomainModel):
    """What reconciliation asks placement to do."""

    content_object_id: ContentObjectId
    fingerprint: StrongFingerprint
    destination: DestinationConfig
    locator: str = Field(min_length=1)
    template_version: str = Field(min_length=1)
    size_bytes: int = Field(ge=0)
    attempt: int = Field(default=0, ge=0)
    originals: dict[str, str] = Field(
        default_factory=dict, description="Components normalization changed, by rendered form."
    )


class PlaceOutcome(StrEnum):
    """How a placement workflow ended."""

    COPIED = "copied"
    ALREADY_PRESENT = "already_present"
    FAILED = "failed"
    NO_SOURCE = "no_source"


def placement_workflow_for(request: PlacementRequest) -> str:
    """The workflow identity of one attempt at one placement."""
    return placement_attempt_workflow_id(
        request.fingerprint, request.destination.id, request.locator, request.attempt
    )


def verification_workflow_for(placement_id: PlacementId, attempt: int) -> str:
    """The workflow identity of verifying one attempt's copy."""
    return f"{VERIFY_PREFIX}:{placement_id}:{attempt}"


async def enqueue_placement(request: PlacementRequest) -> None:
    """Enqueue a placement on its size class's queue; a repeated request collapses."""
    threshold = current().settings.transfer_large_threshold_bytes
    queue = size_class(request.size_bytes, large_threshold=threshold).queue
    with (
        SetWorkflowID(placement_workflow_for(request)),
        SetEnqueueOptions(queue_partition_key=request.destination.provider.value),
    ):
        await DBOS.enqueue_workflow_async(queue.value, place_content, request)


# Steps.


@DBOS.step()
async def _placement_now_step() -> datetime:
    """Read the clock once, so a replay records the same time."""
    return current().clock()


@DBOS.step()
async def _placement_source_step(content_object_id: ContentObjectId) -> SourceObject | None:
    """A fingerprinted source of the content, first by path: the one copied."""
    async with current().datasource.engine.connect() as connection:
        sources = await SourceObjectRepository(connection).find_by_content_object(content_object_id)
    usable = [s for s in sources if s.state is SourceObjectState.FINGERPRINTED]
    return usable[0] if usable else None


@DBOS.step()
async def _copy_step(
    request: PlacementRequest, source: SourceObject, locator: str
) -> TransferEvidence:
    """Copy to the reserved `locator` under the transfer budget and collect the evidence."""
    rclone, remotes, admission = current().require_transfers()
    source_fs = await remotes.fs_for(source.provider, source.account)
    destination_fs = await remotes.fs_for(request.destination.provider, request.destination.account)
    group = placement_workflow_for(request)

    async def report(done: int, total: int) -> None:
        """Log how far the copy has got."""
        logger.info("placement.progress", workflow_id=group, bytes=done, total_bytes=total)

    with admission.admit(source.size_bytes) as cls:
        evidence = await copy_and_collect(
            rclone.client(),
            TransferSpec(
                source_fs=source_fs,
                source_path=source.path,
                destination_fs=destination_fs,
                destination_path=locator,
                size_bytes=source.size_bytes,
                group=group,
                large=cls is SizeClass.LARGE,
            ),
            on_progress=report,
            poll_seconds=1.0,
        )
    # The source's version at copy time, for the check against what was fingerprinted.
    return evidence.model_copy(update={"source_version_marker": source.version_marker})


@DBOS.step()
async def _verification_inputs_step(
    placement_id: PlacementId,
) -> tuple[Placement | None, TransferEvidence | None, SourceObject | None]:
    """The placement, its evidence, and the source it was copied from."""
    async with current().datasource.engine.connect() as connection:
        placement = await PlacementRepository(connection).get(placement_id)
        evidence = await PlacementStore(connection).evidence(placement_id)
        source = (
            None
            if placement is None or placement.source_object_id is None
            else await SourceObjectRepository(connection).get(placement.source_object_id)
        )
    return placement, evidence, source


# Transactions.


async def _start_tx(
    request: PlacementRequest, source: SourceObject, now: datetime
) -> tuple[Placement, bool]:
    """Record that the copy is starting, unless the placement is already present."""
    connection = await current().datasource.sql_session().connection()
    return await start(
        connection,
        content_object_id=request.content_object_id,
        fingerprint=request.fingerprint,
        destination=request.destination,
        locator=request.locator,
        template_version=request.template_version,
        source=source,
        transformations=request.originals,
        attempt=request.attempt,
        now=now,
    )


async def _evidence_tx(placement_id: PlacementId, evidence: TransferEvidence) -> None:
    """Record the copy's evidence, before anything assesses it."""
    connection = await current().datasource.sql_session().connection()
    await PlacementStore(connection).set_evidence(placement_id, evidence)


async def _defer_tx(placement_id: PlacementId, reason: str) -> None:
    """Record the resource error a copy is waiting out."""
    connection = await current().datasource.sql_session().connection()
    await defer(connection, placement_id, reason)


async def _fail_tx(placement_id: PlacementId, reason: str, now: datetime) -> None:
    """Record a copy that did not complete."""
    connection = await current().datasource.sql_session().connection()
    await fail(connection, placement_id, reason, now=now)


async def _conclude_tx(
    placement_id: PlacementId, assessment: Assessment, now: datetime
) -> SourceObject | None:
    """Register, distrust or fail the placement; return a source sent back, if any."""
    connection = await current().datasource.sql_session().connection()
    _, reset = await conclude(connection, placement_id, assessment, now=now)
    return reset


# Workflows.


@DBOS.workflow()
async def place_content(request: PlacementRequest) -> PlaceOutcome:
    """Copy one content object to one destination and hand it to verification."""
    datasource = current().datasource
    settings = current().settings
    source = await _placement_source_step(request.content_object_id)
    if source is None:
        return PlaceOutcome.NO_SOURCE
    placement, starting = await datasource.run_tx_step_async(
        {"name": "start_placement"}, _start_tx, request, source, await _placement_now_step()
    )
    if not starting:
        return PlaceOutcome.ALREADY_PRESENT
    first_wait = settings.transfer_retry_seconds
    wait = first_wait
    evidence: TransferEvidence | None = None
    failure: str | None = None
    for _ in range(TRANSFER_WAITS):
        try:
            evidence = await _copy_step(request, source, placement.locator)
            break
        except (AdmissionDeferredError, ScratchExhaustedError) as waiting:
            failure = str(waiting)
            logger.info("placement.waiting", placement_id=str(placement.id), reason=failure)
            await datasource.run_tx_step_async(
                {"name": "defer_placement"}, _defer_tx, placement.id, failure
            )
            await DBOS.sleep_async(wait)
            wait = min(wait * 2, first_wait * _MAX_WAIT_FACTOR)
        except (
            TransferTooLargeError,
            TransferFailedError,
            RcloneError,
            ProviderError,
        ) as error:
            failure = str(error)
            break
    if evidence is None:
        await datasource.run_tx_step_async(
            {"name": "fail_placement"},
            _fail_tx,
            placement.id,
            failure or "the transfer did not complete",
            await _placement_now_step(),
        )
        return PlaceOutcome.FAILED
    await datasource.run_tx_step_async(
        {"name": "record_evidence"}, _evidence_tx, placement.id, evidence
    )
    with SetWorkflowID(verification_workflow_for(placement.id, request.attempt)):
        await DBOS.enqueue_workflow_async(QueueName.VERIFY.value, verify_placement, placement.id)
    return PlaceOutcome.COPIED


@DBOS.workflow()
async def verify_placement(placement_id: PlacementId) -> str:
    """Assess recorded evidence and register, distrust or send the source back."""
    placement, evidence, source = await _verification_inputs_step(placement_id)
    if placement is None or evidence is None:
        return "missing"
    assessment = (
        source_changed(
            evidence,
            fingerprinted_digests=source.reported_digests,
            fingerprinted_marker=source.version_marker,
        )
        if source is not None
        else None
    ) or assess(evidence)
    reset = await current().datasource.run_tx_step_async(
        {"name": "conclude_placement"},
        _conclude_tx,
        placement_id,
        assessment,
        await _placement_now_step(),
    )
    if reset is not None:
        await enqueue_ingest(
            [
                IngestRequest(
                    source_object_id=reset.id,
                    provider=reset.provider,
                    observed=observed_marker(reset),
                    generation=f"placement-{placement.attempts}",
                )
            ]
        )
    return assessment.verdict.value
