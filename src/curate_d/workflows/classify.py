"""Classification: from a fingerprinted content object to recorded facts or a review item.

`classify_content` runs on the CPU profile's `classify-cheap` queue. It is keyed by the
content fingerprint, so identical content at many paths is classified once, and it first
checks for an existing classification, which it reuses without running any tier. The
deterministic and local tiers run over a verified snapshot; if they settle the content its
facts are recorded, and if they escalate, the prepared request goes to `classify_remote`
on the I/O profile's `classify-llm` queue, where the remote model is called beside the
credentials only that profile holds.

The remote call is governed by a monthly spend cap. A reservation is taken before each
call; a refused reservation holds the content for the next period instead of
misclassifying it, and a daily pass hands held content back once the period has turned.

When the taxonomy grows, `reclassify_rejected` classifies again the content whose review
item was opened only for values the new version contains, under identities of that
version so the earlier runs do not stand in for them. An accepted decision closes the
content's open review item.

Every decision is audited in the same transaction that records it.
"""

import asyncio
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import BinaryIO, Final

from dbos import DBOS, ClientScheduleInput, SetWorkflowID

from curate_d.classification.deterministic import TierResult
from curate_d.classification.gating import (
    GateDecision,
    GateVerdict,
    decision_audit,
    gate,
    spend_period,
)
from curate_d.classification.prepare import Prepared, prepare
from curate_d.classification.remote import RemoteOutcome, RemoteRequest, RemoteResult
from curate_d.classification.resolution import (
    decided_by_person,
    now_acceptable,
    set_aside_audit,
)
from curate_d.classification.store import (
    ClassificationRepository,
    Hold,
    HoldRepository,
    ReviewRepository,
    SpendRepository,
    lock_decisions,
)
from curate_d.classification.taxonomy import ActiveTaxonomy, active_taxonomy
from curate_d.configuration.store import ConfigurationRepository
from curate_d.domain import (
    AuditValue,
    ClassificationResult,
    ClassificationTier,
    ContentObject,
    ContentObjectId,
    DomainModel,
    Facts,
    HashAlgorithm,
    ReviewReason,
    ReviewStatus,
    SourceObjectState,
    StrongFingerprint,
)
from curate_d.ingest.pipeline import StagedRef
from curate_d.ledger import AuditRepository, ContentObjectRepository, SourceObjectRepository
from curate_d.observability import get_logger
from curate_d.runtime.queues import QueueName
from curate_d.runtime.services import ServicesNotInstalledError, current
from curate_d.runtime.workflow_ids import classification_workflow_id
from curate_d.staging.errors import StagedEntryMissingError
from curate_d.staging.keys import StagingKey, derive_staging_key

logger = get_logger(__name__)

REMOTE_PREFIX: Final = "classify-remote"
RECLASSIFY_PREFIX: Final = "reclassify"
# Registered under this name, so a process that does not run workflows can enqueue it.
RECLASSIFY_WORKFLOW: Final = "reclassify_rejected"
_REVIEW_PAGE: Final = 500
HOLDS_SCHEDULE_NAME: Final = "classification-holds"
# Explicit, because stored schedule rows start a workflow by name: renaming the
# function must not orphan them or their firing history.
RELEASE_HOLDS_WORKFLOW: Final = "release_holds"
# Transient remote failures are retried this many times, a minute apart and doubling.
REMOTE_ATTEMPTS: Final = 4


class ClassifyOutcome(StrEnum):
    """How a classification workflow ended."""

    CLASSIFIED = "classified"
    REUSED = "reused"
    ESCALATED = "escalated"
    REVIEW = "review"
    HELD = "held"


# Steps.


@DBOS.step()
async def _classify_now_step() -> datetime:
    """Read the clock once, so a replay records the same time."""
    return current().clock()


@DBOS.step()
async def _existing_step(content_object_id: ContentObjectId) -> ClassificationResult | None:
    """The content's classification, if it already has one."""
    async with current().datasource.engine.connect() as connection:
        return await ClassificationRepository(connection).get(content_object_id)


async def _taxonomy_tx() -> ActiveTaxonomy:
    """The active taxonomy, seeding it on first use."""
    connection = await current().datasource.sql_session().connection()
    return await active_taxonomy(ConfigurationRepository(connection))


@DBOS.step()
async def _content_step(
    content_object_id: ContentObjectId,
) -> tuple[ContentObject | None, tuple[str, ...]]:
    """The content object and every path its sources were found at."""
    async with current().datasource.engine.connect() as connection:
        content = await ContentObjectRepository(connection).get(content_object_id)
        sources = await SourceObjectRepository(connection).find_by_content_object(content_object_id)
    return content, tuple(source.path for source in sources)


@DBOS.step()
async def _prepare_step(
    content: ContentObject, paths: tuple[str, ...], staged: StagedRef, taxonomy: ActiveTaxonomy
) -> Prepared:
    """Run the cheap tiers over a verified snapshot, decoding off the event loop."""
    services = current()
    settings = services.settings

    def run(handle: BinaryIO | None) -> Prepared:
        """The CPU-bound part."""
        return prepare(
            handle,
            content_object_id=content.id,
            media_type=content.media_type,
            size_bytes=content.size_bytes,
            paths=paths,
            taxonomy=taxonomy.taxonomy,
            threshold=settings.classification_confidence_threshold,
            local_timezone=settings.local_timezone,
            remote_available=settings.openrouter_model is not None,
        )

    staging = services.require_staging()
    reader = staging.reader
    if reader is None:
        raise ServicesNotInstalledError("this process has no staging reader")

    async def on_snapshot() -> Prepared:
        """Open the snapshot on the staging loop and prepare in a thread."""
        try:
            async with reader.reader.open(
                StagingKey(staged.key), expected_sha256=staged.sha256
            ) as handle:
                return await asyncio.to_thread(run, handle)
        except StagedEntryMissingError:
            return await asyncio.to_thread(run, None)

    return await staging.run(on_snapshot())


@DBOS.step()
async def _remote_step(
    request: RemoteRequest, taxonomy: ActiveTaxonomy, base: Facts
) -> RemoteResult:
    """Call the configured remote model."""
    services = current()
    model = services.settings.openrouter_model
    classifier = services.remote_classifier
    if model is None or classifier is None:
        raise ServicesNotInstalledError("the remote tier is not configured in this process")
    return await classifier.classify(model, request, taxonomy.taxonomy, base)


# Transactions.


async def _record_tx(
    content_object_id: ContentObjectId,
    decision: GateDecision,
    attempts: tuple[TierResult, ...],
    inputs: dict[str, AuditValue],
    taxonomy_version: int,
    now: datetime,
) -> None:
    """Record an accepted classification, or open a review item, with its audit entry.

    A person's resolution or exclusion recorded while this decision was being made wins:
    the decision is audited as set aside and nothing else changes. The content's
    decisions are locked for the transaction, so the check and the write are one.
    """
    connection = await current().datasource.sql_session().connection()
    await lock_decisions(connection, content_object_id)
    existing = await ClassificationRepository(connection).get(content_object_id)
    excluded = await ReviewRepository(connection).excluded(content_object_id)
    if decided_by_person(existing, excluded=excluded):
        logger.info("classify.set_aside", content_object_id=str(content_object_id))
        await AuditRepository(connection).append(
            set_aside_audit(content_object_id, decision.result.tier, decision.result.facts, now=now)
        )
        return
    await AuditRepository(connection).append(
        decision_audit(content_object_id, decision, attempts, inputs=inputs, now=now)
    )
    if decision.verdict is GateVerdict.ACCEPT:
        result = decision.result
        reviews = ReviewRepository(connection)
        reopened = await reviews.open_for(content_object_id)
        if reopened is not None:
            await reviews.close(
                reopened.id,
                ReviewStatus.RESOLVED,
                f"classified at the {result.tier.value} tier under taxonomy {taxonomy_version}",
                now=now,
            )
        await ClassificationRepository(connection).record(
            ClassificationResult(
                content_object_id=content_object_id,
                facts=result.facts,
                tier=result.tier,
                confidence=result.confidence,
                produced_at=now,
                model_id=result.model_id,
                taxonomy_version=taxonomy_version,
            )
        )
        return
    await ReviewRepository(connection).open_item(
        content_object_id,
        decision.reason or ReviewReason.LOW_CONFIDENCE,
        decision.detail or "no tier could settle this content",
        decision.rejected,
        now=now,
    )


async def _reserve_tx(period: str, estimate: Decimal, cap: Decimal, now: datetime) -> bool:
    """Reserve spend for one call."""
    connection = await current().datasource.sql_session().connection()
    return await SpendRepository(connection).reserve(period, estimate, cap, now=now)


async def _settle_tx(period: str, estimate: Decimal, actual: Decimal) -> None:
    """Replace the reservation with the reported cost."""
    connection = await current().datasource.sql_session().connection()
    await SpendRepository(connection).settle(period, estimate, actual)


async def _hold_tx(hold: Hold) -> None:
    """Hold content for the next period."""
    connection = await current().datasource.sql_session().connection()
    await HoldRepository(connection).hold(hold)


async def _release_tx(content_object_id: ContentObjectId, period: str) -> None:
    """Drop a hold from `period` that has been handed back."""
    connection = await current().datasource.sql_session().connection()
    await HoldRepository(connection).release(content_object_id, period)


async def _due_holds_tx(period: str) -> list[Hold]:
    """Holds from earlier periods."""
    connection = await current().datasource.sql_session().connection()
    return await HoldRepository(connection).due(period)


# Workflows.


async def _decide(
    content_object_id: ContentObjectId,
    decision: GateDecision,
    attempts: tuple[TierResult, ...],
    inputs: dict[str, AuditValue],
    taxonomy: ActiveTaxonomy,
) -> None:
    """Record a decision through the ledger datasource."""
    await current().datasource.run_tx_step_async(
        {"name": "record_classification"},
        _record_tx,
        content_object_id,
        decision,
        attempts,
        inputs,
        taxonomy.version,
        await _classify_now_step(),
    )


async def enqueue_classification(
    fingerprint: StrongFingerprint, content_object_id: ContentObjectId, staged: StagedRef
) -> None:
    """Enqueue classification of one content fingerprint; repeat content collapses."""
    with SetWorkflowID(classification_workflow_id(fingerprint)):
        await DBOS.enqueue_workflow_async(
            QueueName.CLASSIFY_CHEAP.value, classify_content, content_object_id, staged
        )


@DBOS.workflow()
async def classify_content(
    content_object_id: ContentObjectId, staged: StagedRef, generation: str = ""
) -> ClassifyOutcome:
    """Classify one content object with the cheap tiers, escalating if they cannot settle it.

    `generation` names a repeat classification, such as one after the taxonomy grew, so
    its escalation is a remote call of its own rather than the earlier one.
    """
    if await _existing_step(content_object_id) is not None:
        return ClassifyOutcome.REUSED
    content, paths = await _content_step(content_object_id)
    if content is None:
        raise ValueError(f"content object {content_object_id} does not exist")
    taxonomy = await current().datasource.run_tx_step_async(
        {"name": "active_taxonomy"}, _taxonomy_tx
    )
    prepared = await _prepare_step(content, paths, staged, taxonomy)
    decision = prepared.decision
    if decision.verdict is GateVerdict.ESCALATE and prepared.remote is not None:
        suffix = f":{generation}" if generation else ""
        with SetWorkflowID(f"{REMOTE_PREFIX}:{content_object_id}{suffix}"):
            await DBOS.enqueue_workflow_async(
                QueueName.CLASSIFY_LLM.value,
                classify_remote,
                content_object_id,
                prepared.remote,
                decision.result.facts,
                generation,
            )
        return ClassifyOutcome.ESCALATED
    await _decide(content_object_id, decision, prepared.attempts, prepared.inputs, taxonomy)
    return (
        ClassifyOutcome.CLASSIFIED
        if decision.verdict is GateVerdict.ACCEPT
        else ClassifyOutcome.REVIEW
    )


@DBOS.workflow()
async def classify_remote(
    content_object_id: ContentObjectId, request: RemoteRequest, base: Facts, generation: str
) -> ClassifyOutcome:
    """Ask the remote model, within the spend cap, and record what it establishes."""
    if await _existing_step(content_object_id) is not None:
        return ClassifyOutcome.REUSED
    services = current()
    settings = services.settings
    datasource = services.datasource
    taxonomy = await datasource.run_tx_step_async({"name": "active_taxonomy"}, _taxonomy_tx)
    inputs: dict[str, AuditValue] = {
        "media_type": request.media_type,
        "names": ", ".join(request.paths),
    }
    estimate = settings.remote_call_estimate_usd

    wait = 60.0
    answer: RemoteResult | None = None
    for attempt in range(REMOTE_ATTEMPTS):
        now = await _classify_now_step()
        period = spend_period(now)
        allowed = await datasource.run_tx_step_async(
            {"name": "reserve_spend"},
            _reserve_tx,
            period,
            estimate,
            settings.remote_spend_cap_usd,
            now,
        )
        if not allowed:
            await datasource.run_tx_step_async(
                {"name": "hold"},
                _hold_tx,
                Hold(
                    content_object_id=content_object_id,
                    period=period,
                    held_at=now,
                    request=request,
                    base=base,
                ),
            )
            logger.warning("classification.held_by_spend_cap", period=period)
            return ClassifyOutcome.HELD
        answer = await _remote_step(request, taxonomy, base)
        await datasource.run_tx_step_async(
            {"name": "settle_spend"}, _settle_tx, period, estimate, answer.cost_usd
        )
        if answer.outcome is not RemoteOutcome.UNAVAILABLE or attempt == REMOTE_ATTEMPTS - 1:
            break
        await DBOS.sleep_async(wait)
        wait *= 2

    assert answer is not None
    fallback = TierResult(
        tier=ClassificationTier.REMOTE_MODEL,
        facts=base,
        confidence=0.0,
        evidence={"model": answer.model_id, "detail": answer.detail},
        model_id=answer.model_id,
    )
    if answer.outcome is RemoteOutcome.ANSWERED and answer.result is not None:
        decision = gate(
            answer.result,
            taxonomy.taxonomy,
            threshold=settings.classification_confidence_threshold,
            last_tier=True,
        )
    elif answer.outcome is RemoteOutcome.REJECTED_VALUE:
        decision = GateDecision(
            verdict=GateVerdict.REVIEW,
            result=fallback,
            reason=ReviewReason.REJECTED_VALUE,
            rejected=answer.rejected,
            detail=answer.detail,
        )
    elif answer.outcome is RemoteOutcome.MALFORMED:
        decision = GateDecision(
            verdict=GateVerdict.REVIEW,
            result=fallback,
            reason=ReviewReason.MALFORMED_OUTPUT,
            detail=f"the remote model's answer was unusable: {answer.detail}",
        )
    else:
        decision = GateDecision(
            verdict=GateVerdict.REVIEW,
            result=fallback,
            reason=ReviewReason.LOW_CONFIDENCE,
            detail=f"the remote tier stayed unavailable: {answer.detail}",
        )
    await _decide(content_object_id, decision, (decision.result,), inputs, taxonomy)
    return (
        ClassifyOutcome.CLASSIFIED
        if decision.verdict is GateVerdict.ACCEPT
        else ClassifyOutcome.REVIEW
    )


@DBOS.workflow(name=RELEASE_HOLDS_WORKFLOW)
async def release_holds(scheduled_time: datetime, context: object) -> None:
    """Hand content held by an earlier period's cap back to the remote tier."""
    datasource = current().datasource
    period = spend_period(await _classify_now_step())
    for hold in await datasource.run_tx_step_async({"name": "due_holds"}, _due_holds_tx, period):
        with SetWorkflowID(f"{REMOTE_PREFIX}:{hold.content_object_id}:{period}"):
            await DBOS.enqueue_workflow_async(
                QueueName.CLASSIFY_LLM.value,
                classify_remote,
                hold.content_object_id,
                hold.request,
                hold.base,
                period,
            )
        await datasource.run_tx_step_async(
            {"name": "release_hold"}, _release_tx, hold.content_object_id, hold.period
        )


class Reclassification(DomainModel):
    """One content object to classify again, and the staged bytes to read."""

    content_object_id: ContentObjectId
    fingerprint: StrongFingerprint
    staged: StagedRef


@DBOS.step()
async def _reclassifiable_step(taxonomy: ActiveTaxonomy) -> list[Reclassification]:
    """Content held in review only for values `taxonomy` now contains."""
    found: list[Reclassification] = []
    async with current().datasource.engine.connect() as connection:
        reviews = ReviewRepository(connection)
        contents = ContentObjectRepository(connection)
        sources = SourceObjectRepository(connection)
        offset = 0
        while page := await reviews.list_open(limit=_REVIEW_PAGE, offset=offset):
            offset += len(page)
            for item in now_acceptable(page, taxonomy.taxonomy):
                content = await contents.get(item.content_object_id)
                fingerprinted = [
                    s
                    for s in await sources.find_by_content_object(item.content_object_id)
                    if s.state is SourceObjectState.FINGERPRINTED
                ]
                sha1 = fingerprinted[0].reported(HashAlgorithm.SHA1) if fingerprinted else None
                if content is None or sha1 is None:
                    logger.warning(
                        "classify.reclassify_skipped", content_object_id=str(item.content_object_id)
                    )
                    continue
                found.append(
                    Reclassification(
                        content_object_id=content.id,
                        fingerprint=content.fingerprint,
                        staged=StagedRef(
                            key=derive_staging_key(fingerprinted[0].id),
                            sha256=content.fingerprint.digest,
                            sha1=sha1.digest,
                            size_bytes=content.size_bytes,
                        ),
                    )
                )
    return found


@DBOS.workflow(name=RECLASSIFY_WORKFLOW)
async def reclassify_rejected(taxonomy_version: int) -> int:
    """Classify again what taxonomy `taxonomy_version` would now accept; return how many."""
    taxonomy = await current().datasource.run_tx_step_async(
        {"name": "reclassify_taxonomy"}, _taxonomy_tx
    )
    generation = f"taxonomy-{taxonomy_version}"
    candidates = await _reclassifiable_step(taxonomy)
    for candidate in candidates:
        with SetWorkflowID(f"{RECLASSIFY_PREFIX}:{candidate.fingerprint.digest}:{generation}"):
            await DBOS.enqueue_workflow_async(
                QueueName.CLASSIFY_CHEAP.value,
                classify_content,
                candidate.content_object_id,
                candidate.staged,
                generation,
            )
    return len(candidates)


def reclassification_workflow_id(taxonomy_version: int) -> str:
    """The identity of re-classifying after one taxonomy version: once per version."""
    return f"{RECLASSIFY_PREFIX}:taxonomy-{taxonomy_version}"


def schedule() -> ClientScheduleInput:
    """Return the daily schedule that releases holds once their period has turned."""
    return {
        "schedule_name": HOLDS_SCHEDULE_NAME,
        "workflow_name": RELEASE_HOLDS_WORKFLOW,
        "schedule": "15 0 * * *",
        "context": None,
        "queue_name": QueueName.MAINTENANCE.value,
    }
