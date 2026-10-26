"""Desired placements against observed ones, for one content object at a time.

Evaluation reads the ledger only. For each content object it derives the destinations the
policy wants, renders where the content goes at each, and pairs every target with the
placement the ledger holds for that content at that destination's identity, never by its
name: a name pointed at another account or root pairs with nothing, so its content is placed
again there, and a renamed destination keeps its placements. What to do about each
pair is `decide`'s: nothing when the copy is registered at the rendered locator, nothing
while an attempt is still running, otherwise a new attempt. Deciding needs to know which
attempts are still running, which the workflow asks DBOS for between the two.

Content awaiting a person — other than for a render failure, which a template change may
resolve — is left alone, and content a person excluded is never placed. The retention
decision is re-derived from the placements as they stand, counting only those at
destination identities the policy defines (the ones the destination checks still verify),
and is reported only when it, or the policy version it applied, differs from the last one
recorded.
"""

from collections.abc import Sequence
from enum import StrEnum

from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncConnection

from curate_d.classification.store import ClassificationRepository, ReviewRepository
from curate_d.domain import (
    ContentObjectId,
    DestinationConfig,
    DomainModel,
    Placement,
    PlacementPolicy,
    PlacementState,
    ReviewReason,
    SourceObjectState,
    StrongFingerprint,
)
from curate_d.ledger import (
    ContentObjectRepository,
    DestinationRepository,
    PlacementRepository,
    SourceObjectRepository,
)
from curate_d.reconciliation.retention import RetentionDecision, decide_retention
from curate_d.reconciliation.store import ReconciliationStore, RecordedRetention
from curate_d.rendering.planning import RenderFailure, plan_at
from curate_d.rendering.templates import TemplateSet

_IN_FLIGHT = frozenset({PlacementState.PENDING, PlacementState.COPYING, PlacementState.VERIFYING})


class Target(DomainModel):
    """One placement the policy wants, and what the ledger holds for it."""

    content_object_id: ContentObjectId
    fingerprint: StrongFingerprint
    size_bytes: int = Field(ge=0)
    destination: DestinationConfig
    locator: str
    template_version: str
    originals: dict[str, str] = Field(default_factory=dict)
    existing: Placement | None = None


class ContentEvaluation(DomainModel):
    """Everything one evaluation found for one content object."""

    content_object_id: ContentObjectId
    targets: tuple[Target, ...] = ()
    render_failures: tuple[RenderFailure, ...] = ()
    retention: RetentionDecision | None = Field(
        default=None,
        description="Set only when it, or its policy version, differs from the last decision "
        "recorded.",
    )


class Action(StrEnum):
    """What to do about one target."""

    SATISFIED = "satisfied"
    IN_FLIGHT = "in_flight"
    PLACE = "place"
    STALE = "stale"
    """An attempt that ended without concluding: record it failed, then place again."""


def decide(target: Target, *, running: bool) -> Action:
    """What to do about a target, given whether its latest attempt is still running."""
    existing = target.existing
    if existing is None:
        return Action.PLACE
    if existing.state is PlacementState.PRESENT and existing.locator == target.locator:
        return Action.SATISFIED
    if existing.state in _IN_FLIGHT:
        return Action.IN_FLIGHT if running else Action.STALE
    return Action.PLACE


def paired(destination: DestinationConfig, placements: Sequence[Placement]) -> Placement | None:
    """Return the placement at `destination`'s identity, whatever name it was made under."""
    return next((p for p in placements if p.destination_id == destination.id), None)


def retention_changed(
    last: RecordedRetention | None, decision: RetentionDecision, *, policy_version: int | None
) -> bool:
    """Return whether `decision` under `policy_version` is not the last one recorded.

    A new policy version counts as a change even when the outcome and reason are the same,
    so the latest entry names the version that governs. An entry written by a build that did
    not record the version reads as `None`: equal to an unknown current version, but not to
    a known one, so the first pass under a build that records it writes the decision once
    more.
    """
    if last is None:
        return True
    return (last.outcome, last.reason, last.policy_version) != (
        decision.outcome.value,
        decision.reason,
        policy_version,
    )


async def evaluate(
    connection: AsyncConnection,
    content_object_id: ContentObjectId,
    *,
    templates: TemplateSet,
    policy: PlacementPolicy,
    policy_version: int | None = None,
) -> ContentEvaluation:
    """Evaluate one content object against the templates and the policy at `policy_version`."""
    nothing = ContentEvaluation(content_object_id=content_object_id)
    reviews = ReviewRepository(connection)
    review = await reviews.open_for(content_object_id)
    if review is not None and review.reason is not ReviewReason.RENDER_FAILED:
        return nothing
    if await reviews.excluded(content_object_id):
        return nothing
    classification = await ClassificationRepository(connection).get(content_object_id)
    content = await ContentObjectRepository(connection).get(content_object_id)
    if classification is None or content is None:
        return nothing
    facts = classification.facts
    sources = await SourceObjectRepository(connection).find_by_content_object(content_object_id)
    placements = await PlacementRepository(connection).find_for_content_object(content_object_id)
    defined = policy.destinations_by_id()
    elsewhere = sorted({p.destination_id for p in placements} - defined.keys())
    retention = decide_retention(
        policy.retention_for(facts),
        placements,
        destinations=defined,
        locations=await DestinationRepository(connection).find_many(elsewhere),
    )
    last = await ReconciliationStore(connection).last_retention(content_object_id)
    changed = retention_changed(last, retention, policy_version=policy_version)
    evaluation = nothing.model_copy(update={"retention": retention if changed else None})
    if not any(s.state is SourceObjectState.FINGERPRINTED for s in sources):
        return evaluation
    paths = tuple(sorted(s.path for s in sources))
    targets: list[Target] = []
    failures: list[RenderFailure] = []
    for destination in _destinations(policy, policy.desired_destinations(facts)):
        planned = await plan_at(
            connection,
            templates,
            facts,
            content_object_id=content_object_id,
            fingerprint=content.fingerprint,
            paths=paths,
            destination=destination,
        )
        if isinstance(planned, RenderFailure):
            failures.append(planned)
            continue
        targets.append(
            Target(
                content_object_id=content_object_id,
                fingerprint=content.fingerprint,
                size_bytes=content.size_bytes,
                destination=destination,
                locator=planned.locator,
                template_version=planned.rendered.template_version,
                originals={t.rendered: t.original for t in planned.rendered.transformations},
                existing=paired(destination, placements),
            )
        )
    return evaluation.model_copy(
        update={"targets": tuple(targets), "render_failures": tuple(failures)}
    )


def _destinations(policy: PlacementPolicy, names: Sequence[str]) -> list[DestinationConfig]:
    """The configured destinations named."""
    return [d for name in names if (d := policy.destination(name)) is not None]
