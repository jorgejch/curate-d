"""Desired placements against observed ones, for one content object at a time.

Evaluation reads the ledger only. For each content object it derives the destinations the
policy wants, renders where the content goes at each, and pairs every target with the
placement the ledger holds for that content at that destination. What to do about each
pair is `decide`'s: nothing when the copy is registered at the rendered locator, nothing
while an attempt is still running, otherwise a new attempt. Deciding needs to know which
attempts are still running, which the workflow asks DBOS for between the two.

Content awaiting a person — other than for a render failure, which a template change may
resolve — is left alone, and content a person excluded is never placed. The retention
decision is re-derived from the placements as they stand, and reported only when it
differs from the last one recorded.
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
from curate_d.ledger import ContentObjectRepository, PlacementRepository, SourceObjectRepository
from curate_d.reconciliation.retention import RetentionDecision, decide_retention
from curate_d.reconciliation.store import ReconciliationStore
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
        default=None, description="Set only when it differs from the last decision recorded."
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


async def evaluate(
    connection: AsyncConnection,
    content_object_id: ContentObjectId,
    *,
    templates: TemplateSet,
    policy: PlacementPolicy,
) -> ContentEvaluation:
    """Evaluate one content object against the templates and the policy."""
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
    existing = {
        p.destination_name: p
        for p in await PlacementRepository(connection).find_for_content_object(content_object_id)
    }
    retention = decide_retention(policy.retention_for(facts), list(existing.values()))
    last = await ReconciliationStore(connection).last_retention(content_object_id)
    changed = last != (retention.outcome.value, retention.reason)
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
                existing=existing.get(destination.name),
            )
        )
    return evaluation.model_copy(
        update={"targets": tuple(targets), "render_failures": tuple(failures)}
    )


def _destinations(policy: PlacementPolicy, names: Sequence[str]) -> list[DestinationConfig]:
    """The configured destinations named."""
    return [d for name in names if (d := policy.destination(name)) is not None]
