"""Reconciliation: converge observed placements on the policy, and look for lost copies.

`reconcile` evaluates content a batch at a time on the `maintenance` queue: each batch is
read in one step, the running attempts it depends on are asked of DBOS, and one ledger
transaction records what the batch decided — attempts that ended without concluding are
recorded failed, render failures open a review item, and a changed retention decision is
audited. Then a placement is enqueued for every target that needs one. Placement
identities are deterministic, so an evaluation overlapping another, or repeating one,
collapses onto the same work. After a bounded number of batches a run hands over to a
continuation, so no recovered workflow replays an unbounded history.

`check_destination` lists a destination and marks registered placements it no longer
holds as absent, then reconciles the content they belonged to. A destination that cannot
be listed is recorded as unchecked, and nothing about its placements changes. A check looks
only for placements made at the destination's identity, provider, account and root, and is
recorded and keyed by it: placements left under the same name at an account or root the
policy no longer gives it are neither looked for there nor copied again because of it.
"""

from datetime import datetime
from typing import Final

from dbos import DBOS, PropagateOtelContext, ScheduleInput, SetWorkflowID
from pydantic import Field

from curate_d.classification.store import ReviewRepository
from curate_d.config import Settings
from curate_d.configuration.store import ConfigurationRepository
from curate_d.domain import (
    AuditEntry,
    AuditEntryType,
    ContentObjectId,
    DestinationConfig,
    DomainModel,
    PlacementId,
    PlacementPolicy,
    PlacementState,
    ReviewReason,
)
from curate_d.ledger import AuditRepository
from curate_d.observability import get_logger
from curate_d.placement.dry_run import content_page
from curate_d.placement.pipeline import fail, mark_absent
from curate_d.placement.policy import active_policy
from curate_d.placement.rclone import RcloneError
from curate_d.providers.errors import ProviderError
from curate_d.reconciliation.convergence import (
    Action,
    ContentEvaluation,
    Target,
    decide,
    evaluate,
)
from curate_d.reconciliation.store import DECIDED_BY, DestinationCheck, ReconciliationStore
from curate_d.rendering.planning import active_templates, render_failure_audit
from curate_d.rendering.templates import TemplateSet
from curate_d.runtime.queues import QueueName
from curate_d.runtime.services import current
from curate_d.runtime.workflow_ids import (
    destination_check_workflow_id,
    reconcile_workflow_id,
)
from curate_d.workflows.placement import (
    PlacementRequest,
    enqueue_placement,
    placement_workflow_for,
    verification_workflow_for,
)

logger = get_logger(__name__)

SCHEDULE_NAME: Final = "reconcile"
CHECK_SCHEDULE_NAME: Final = "check-destinations"
_RUNNING: Final = frozenset({"ENQUEUED", "PENDING"})
_PRESENT_PAGE: Final = 1000


class ReconcileRequest(DomainModel):
    """One part of one reconciliation run."""

    run: str = Field(min_length=1, pattern=r"^[^:]+$")
    part: int = Field(default=0, ge=0)
    after: ContentObjectId | None = Field(
        default=None, description="Continue after this content object."
    )
    content_object_ids: tuple[ContentObjectId, ...] = Field(
        default=(), description="Only these content objects; every one when empty."
    )


class ReconcileContext(DomainModel):
    """What a part evaluates with, read when it starts."""

    templates: TemplateSet
    policy: PlacementPolicy
    policy_version: int | None = Field(
        default=None, ge=1, description="The configuration version of `policy`."
    )


class ReconcileBatch(DomainModel):
    """One batch's evaluations, and where the next batch starts."""

    evaluations: tuple[ContentEvaluation, ...]
    last: ContentObjectId | None


class ReconcileSummary(DomainModel):
    """What one part did."""

    evaluated: int = 0
    placed: int = 0
    satisfied: int = 0
    in_flight: int = 0
    render_failed: int = 0
    continued: bool = False


async def enqueue_reconcile(request: ReconcileRequest) -> str:
    """Enqueue one part of a run; a repeated request collapses."""
    workflow_id = reconcile_workflow_id(request.run, request.part)
    # Carries this span to the dequeued child; without it the child roots its own trace.
    with PropagateOtelContext(), SetWorkflowID(workflow_id):
        await DBOS.enqueue_workflow_async(QueueName.MAINTENANCE.value, reconcile, request)
    return workflow_id


def _request_for(
    target: Target, attempt: int, policy_version: int | None = None
) -> PlacementRequest:
    """Return the placement request that establishes `target`."""
    return PlacementRequest(
        content_object_id=target.content_object_id,
        fingerprint=target.fingerprint,
        destination=target.destination,
        locator=target.locator,
        template_version=target.template_version,
        size_bytes=target.size_bytes,
        attempt=attempt,
        originals=target.originals,
        policy_version=policy_version,
    )


def _attempt_ids(target: Target) -> list[str]:
    """Return the workflows the target's latest attempt runs as: its copy and verification."""
    existing = target.existing
    if existing is None:
        return []
    running = target.model_copy(update={"locator": existing.locator})
    return [
        placement_workflow_for(_request_for(running, existing.attempts)),
        verification_workflow_for(existing.id, existing.attempts),
    ]


# Steps.


@DBOS.step()
async def _reconcile_now_step() -> datetime:
    """Read the clock once, so a replay records the same time."""
    return current().clock()


@DBOS.step()
async def _reconcile_batch_step(
    request: ReconcileRequest, context: ReconcileContext, after: ContentObjectId | None
) -> ReconcileBatch:
    """Evaluate the next batch of content objects, reading the ledger only."""
    size = current().settings.reconcile_batch_size
    async with current().datasource.engine.connect() as connection:
        if request.content_object_ids:
            ids = sorted(c for c in request.content_object_ids if after is None or c > after)[:size]
        else:
            ids = await content_page(connection, after=after, limit=size)
        evaluations = [
            await evaluate(
                connection,
                content_id,
                templates=context.templates,
                policy=context.policy,
                policy_version=context.policy_version,
            )
            for content_id in ids
        ]
    return ReconcileBatch(evaluations=tuple(evaluations), last=ids[-1] if ids else None)


@DBOS.step()
async def _absent_step(destination: DestinationConfig) -> tuple[list[PlacementId], str | None]:
    """Return the registered placements the destination lost, or why it could not be listed."""
    rclone, remotes, _ = current().require_transfers()
    directory = destination.root_path.strip("/")
    try:
        fs = await remotes.fs_for(destination.provider, destination.account)
        held = {
            "/" + "/".join(part for part in (directory, path) if part)
            for path in await rclone.client().list_files(fs, directory)
        }
    except (RcloneError, ProviderError) as error:
        return [], str(error)
    absent: list[PlacementId] = []
    after: PlacementId | None = None
    async with current().datasource.engine.connect() as connection:
        store = ReconciliationStore(connection)
        while page := await store.present_at(destination.id, after=after, limit=_PRESENT_PAGE):
            absent.extend(placement_id for placement_id, locator in page if locator not in held)
            after = page[-1][0]
    return absent, None


# Transactions.


async def _open_tx() -> ReconcileContext:
    """Read the templates and policy in force, seeding them if never configured."""
    connection = await current().datasource.sql_session().connection()
    configurations = ConfigurationRepository(connection)
    templates = await active_templates(configurations)
    policy = await active_policy(configurations)
    return ReconcileContext(
        templates=templates.templates, policy=policy.policy, policy_version=policy.version
    )


async def _record_tx(
    evaluations: tuple[ContentEvaluation, ...],
    stale: tuple[PlacementId, ...],
    now: datetime,
    policy_version: int | None = None,
) -> None:
    """Record what one batch decided under `policy_version`, apart from what it enqueues."""
    connection = await current().datasource.sql_session().connection()
    for placement_id in stale:
        await fail(connection, placement_id, "the attempt ended without concluding", now=now)
    reviews = ReviewRepository(connection)
    audit = AuditRepository(connection)
    for evaluation in evaluations:
        if evaluation.render_failures:
            detail = "; ".join(
                f"{f.destination_name}: {f.reason}" for f in evaluation.render_failures
            )
            open_item = await reviews.open_for(evaluation.content_object_id)
            already = (
                open_item is not None
                and open_item.reason is ReviewReason.RENDER_FAILED
                and open_item.detail == detail
            )
            if not already:
                await reviews.open_item(
                    evaluation.content_object_id, ReviewReason.RENDER_FAILED, detail, (), now=now
                )
                for failure in evaluation.render_failures:
                    await audit.append(render_failure_audit(failure, now=now))
        if evaluation.retention is not None:
            await audit.append(
                AuditEntry(
                    entry_type=AuditEntryType.RETENTION_DECIDED,
                    occurred_at=now,
                    decided_by=DECIDED_BY,
                    details=evaluation.retention.audit_details(policy_version=policy_version),
                    content_object_id=evaluation.content_object_id,
                )
            )


async def _absent_tx(
    destination: DestinationConfig, absent: list[PlacementId], checked: str, now: datetime
) -> list[ContentObjectId]:
    """Mark placements absent and record the check; return the content they belonged to."""
    connection = await current().datasource.sql_session().connection()
    marked = [
        placement
        for placement_id in absent
        if (placement := await mark_absent(connection, placement_id, checked=checked, now=now))
        is not None
    ]
    await ReconciliationStore(connection).record_check(
        DestinationCheck(
            destination_id=destination.id,
            destination_name=destination.name,
            checked_at=now,
            reachable=True,
            absent=len(marked),
        )
    )
    return sorted({p.content_object_id for p in marked})


async def _unreachable_tx(destination: DestinationConfig, reason: str, now: datetime) -> None:
    """Record that a destination could not be checked."""
    connection = await current().datasource.sql_session().connection()
    await ReconciliationStore(connection).record_check(
        DestinationCheck(
            destination_id=destination.id,
            destination_name=destination.name,
            checked_at=now,
            reachable=False,
            reason=reason,
        )
    )


# Workflows.


@DBOS.workflow()
async def reconcile(request: ReconcileRequest) -> ReconcileSummary:
    """Converge a bounded number of batches, then hand over to a continuation."""
    datasource = current().datasource
    context = await datasource.run_tx_step_async({"name": "open_reconcile"}, _open_tx)
    summary = ReconcileSummary()
    after = request.after
    for _ in range(current().settings.reconcile_batches_per_run):
        batch = await _reconcile_batch_step(request, context, after)
        if batch.last is None:
            return summary
        targets = [t for e in batch.evaluations for t in e.targets]
        attempt_ids = [workflow_id for t in targets for workflow_id in _attempt_ids(t)]
        statuses = (
            await DBOS.list_workflows_async(
                workflow_ids=attempt_ids, load_input=False, load_output=False
            )
            if attempt_ids
            else []
        )
        running = {s.workflow_id for s in statuses if s.status in _RUNNING}
        place: list[PlacementRequest] = []
        stale: list[PlacementId] = []
        for target in targets:
            action = decide(target, running=any(i in running for i in _attempt_ids(target)))
            attempts = 0 if target.existing is None else target.existing.attempts
            if action is Action.STALE and target.existing is not None:
                stale.append(target.existing.id)
                place.append(_request_for(target, attempts + 1, context.policy_version))
            elif action is Action.PLACE:
                # A copy registered elsewhere is moving: a new attempt, so that moving back
                # to a locator it once had is not mistaken for that earlier placement.
                moving = (
                    target.existing is not None and target.existing.state is PlacementState.PRESENT
                )
                place.append(
                    _request_for(
                        target, attempts + 1 if moving else attempts, context.policy_version
                    )
                )
            summary = summary.model_copy(
                update={
                    "satisfied": summary.satisfied + (action is Action.SATISFIED),
                    "in_flight": summary.in_flight + (action is Action.IN_FLIGHT),
                }
            )
        await datasource.run_tx_step_async(
            {"name": "record_reconcile"},
            _record_tx,
            batch.evaluations,
            tuple(stale),
            await _reconcile_now_step(),
            context.policy_version,
        )
        for placement in place:
            await enqueue_placement(placement)
        summary = summary.model_copy(
            update={
                "evaluated": summary.evaluated + len(batch.evaluations),
                "placed": summary.placed + len(place),
                "render_failed": summary.render_failed
                + sum(bool(e.render_failures) for e in batch.evaluations),
            }
        )
        after = batch.last
    await enqueue_reconcile(request.model_copy(update={"part": request.part + 1, "after": after}))
    return summary.model_copy(update={"continued": True})


@DBOS.workflow()
async def reconcile_all(scheduled_time: datetime, context: object) -> None:
    """Start a reconciliation run over every content object."""
    run = f"sweep-{scheduled_time.strftime('%Y%m%dT%H%M%S')}"
    await enqueue_reconcile(ReconcileRequest(run=run))


@DBOS.workflow()
async def check_destination(destination: DestinationConfig, cycle: str) -> int:
    """Mark placements the destination lost as absent, and re-establish them."""
    datasource = current().datasource
    absent, unreachable = await _absent_step(destination)
    now = await _reconcile_now_step()
    if unreachable is not None:
        logger.warning(
            "reconcile.destination_unreachable", destination=destination.name, reason=unreachable
        )
        await datasource.run_tx_step_async(
            {"name": "record_unreachable"}, _unreachable_tx, destination, unreachable, now
        )
        return 0
    affected = await datasource.run_tx_step_async(
        {"name": "mark_absent"},
        _absent_tx,
        destination,
        absent,
        f"check {cycle}",
        now,
    )
    if affected:
        await enqueue_reconcile(
            ReconcileRequest(
                run=f"loss-{destination.id}-{cycle}", content_object_ids=tuple(affected)
            )
        )
    return len(affected)


@DBOS.workflow()
async def check_destinations(scheduled_time: datetime, context: object) -> None:
    """Check every destination the active policy defines.

    Placements at a destination dropped from the policy are no longer checked, so retention
    does not count them.
    """
    policy = (await current().datasource.run_tx_step_async({"name": "open_check"}, _open_tx)).policy
    cycle = scheduled_time.strftime("%Y%m%dT%H%M%S")
    for destination in policy.destinations:
        with (
            PropagateOtelContext(),
            SetWorkflowID(destination_check_workflow_id(destination.id, cycle)),
        ):
            await DBOS.enqueue_workflow_async(
                QueueName.MAINTENANCE.value, check_destination, destination, cycle
            )


def schedule(settings: Settings) -> ScheduleInput:
    """Return the persisted schedule of the reconciliation sweep."""
    return {
        "schedule_name": SCHEDULE_NAME,
        "workflow_fn": reconcile_all,
        "schedule": settings.reconcile_schedule,
        "context": None,
        "queue_name": QueueName.MAINTENANCE.value,
    }


def check_schedule(settings: Settings) -> ScheduleInput:
    """Return the persisted schedule of the destination checks."""
    return {
        "schedule_name": CHECK_SCHEDULE_NAME,
        "workflow_fn": check_destinations,
        "schedule": settings.destination_check_schedule,
        "context": None,
        "queue_name": QueueName.MAINTENANCE.value,
    }
