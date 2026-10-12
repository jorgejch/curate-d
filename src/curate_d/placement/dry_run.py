"""Dry-run evaluation: what placement would do, without doing any of it.

For each content object, the dry run reports where each destination it would be placed
at puts it, and the verification rung the copy could reach given the hashes both
providers report. It reads the ledger and writes only its own results: nothing reaches a
destination, no source is touched, and no placement, review item or audit entry is
created, so a real execution afterwards proceeds as though it had never run.

Content that cannot be evaluated — unclassified, awaiting review, or failing to render —
is reported with the reason, and evaluation continues with the rest.

A locator the run has already proposed for other content counts as taken, as it would be
once that copy started: the later content is given its suffixed locator, just as a real
start claims one. Content is evaluated in id order, so the earlier id keeps the rendered
locator; in a real run whichever copy starts first does. Locators are taken per
destination identity, as in a real start: two destinations sharing a template in
different accounts each get the plain locator.
"""

from collections.abc import Sequence
from typing import Final
from uuid import UUID

from pydantic import AwareDatetime, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from curate_d.classification.store import ClassificationRepository, ReviewRepository
from curate_d.domain import (
    ContentObject,
    ContentObjectId,
    DestinationConfig,
    DestinationId,
    DomainModel,
    DryRunOutcome,
    Facts,
    HashAlgorithm,
    Placement,
    PlacementPolicy,
    Provider,
    RejectedValue,
    SourceObject,
    SourceObjectState,
    VerificationLevel,
)
from curate_d.ledger import ContentObjectRepository, PlacementRepository, SourceObjectRepository
from curate_d.ledger.mapping import (
    Row,
    identifier,
    moment,
    opt_identifier,
    opt_moment,
    opt_text,
    text,
)
from curate_d.ledger.tables import content_objects, dry_run_items, dry_runs, provider_connections
from curate_d.placement.pipeline import claim
from curate_d.placement.verification import expected_level
from curate_d.providers.hashes import advertised_hashes
from curate_d.rendering.planning import PlacementPlan, RenderFailure, plan_at
from curate_d.rendering.templates import TemplateSet

# Content objects evaluated per step.
PAGE_SIZE: Final = 200
# The destination recorded for content that never reached rendering.
NO_DESTINATION: Final = ""


class DryRunRequest(DomainModel):
    """One dry run: its identity, and optionally the destinations to evaluate against."""

    run_id: UUID
    destinations: tuple[DestinationConfig, ...] = Field(
        default=(),
        description="Evaluate every classified object against each of these. Empty: the "
        "active policy's rules decide, as for a real placement.",
    )


class DryRun(DomainModel):
    """A dry run's header."""

    id: UUID
    started_at: AwareDatetime
    finished_at: AwareDatetime | None = None
    template_version: str
    taxonomy_version: int


class DryRunItem(DomainModel):
    """What the dry run concluded for one content object at one destination."""

    run_id: UUID
    content_object_id: ContentObjectId
    destination_name: str = NO_DESTINATION
    destination_id: DestinationId | None = Field(
        default=None, description="None for content that did not reach rendering."
    )
    outcome: DryRunOutcome
    paths: tuple[str, ...] = ()
    facts: Facts | None = None
    locator: str | None = None
    expected_level: VerificationLevel | None = None
    reason: str | None = None
    rejected_values: tuple[RejectedValue, ...] = ()


class DryRunStore:
    """Dry runs and their items."""

    def __init__(self, connection: AsyncConnection) -> None:
        """Wrap a caller-owned connection."""
        self._connection = connection

    async def create(self, run: DryRun) -> None:
        """Record that a run started; a repeat of the same run changes nothing."""
        existing = await self.get(run.id)
        if existing is None:
            await self._connection.execute(dry_runs.insert().values(**run.model_dump()))

    async def finish(self, run_id: UUID, now: AwareDatetime) -> None:
        """Record that a run finished."""
        await self._connection.execute(
            dry_runs.update().where(dry_runs.c.id == run_id).values(finished_at=now)
        )

    async def add(self, items: Sequence[DryRunItem]) -> None:
        """Record items, replacing any a repeated page already recorded."""
        for item in items:
            await self._connection.execute(
                dry_run_items.delete().where(
                    dry_run_items.c.run_id == item.run_id,
                    dry_run_items.c.content_object_id == item.content_object_id,
                    dry_run_items.c.destination_name == item.destination_name,
                )
            )
        if items:
            await self._connection.execute(
                dry_run_items.insert(), [_item_values(item) for item in items]
            )

    async def get(self, run_id: UUID) -> DryRun | None:
        """A run's header, or None if there is no such run."""
        row = (
            (await self._connection.execute(select(dry_runs).where(dry_runs.c.id == run_id)))
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        return DryRun(
            id=identifier(row, "id"),
            started_at=moment(row, "started_at"),
            finished_at=opt_moment(row, "finished_at"),
            template_version=text(row, "template_version"),
            taxonomy_version=int(row["taxonomy_version"]),
        )

    async def proposed_for(
        self, run_id: UUID, destination_id: DestinationId, locator: str
    ) -> ContentObjectId | None:
        """The content a run already proposed `locator` for at a destination, if any."""
        value = await self._connection.scalar(
            select(dry_run_items.c.content_object_id).where(
                dry_run_items.c.run_id == run_id,
                dry_run_items.c.destination_id == destination_id,
                dry_run_items.c.locator == locator,
                dry_run_items.c.outcome == DryRunOutcome.PROPOSED.value,
            )
        )
        return None if value is None else ContentObjectId(value)

    async def items(self, run_id: UUID) -> list[DryRunItem]:
        """A run's items, by content object and destination."""
        rows = await self._connection.execute(
            select(dry_run_items)
            .where(dry_run_items.c.run_id == run_id)
            .order_by(dry_run_items.c.content_object_id, dry_run_items.c.destination_name)
        )
        return [_to_item(row) for row in rows.mappings()]


def _item_values(item: DryRunItem) -> dict[str, object]:
    """The dry_run_items row for an item."""
    return {
        "run_id": item.run_id,
        "content_object_id": item.content_object_id,
        "destination_name": item.destination_name,
        "destination_id": item.destination_id,
        "outcome": item.outcome.value,
        "paths": "\n".join(item.paths),
        "facts": None if item.facts is None else item.facts.model_dump(mode="json"),
        "locator": item.locator,
        "expected_level": None if item.expected_level is None else int(item.expected_level),
        "reason": item.reason,
        "rejected_values": [value.model_dump(mode="json") for value in item.rejected_values],
    }


def _to_item(row: Row) -> DryRunItem:
    """An item from its dry_run_items row."""
    paths = text(row, "paths")
    level = row["expected_level"]
    facts = row["facts"]
    values = row["rejected_values"]
    destination_id = opt_identifier(row, "destination_id")
    return DryRunItem(
        run_id=identifier(row, "run_id"),
        content_object_id=ContentObjectId(identifier(row, "content_object_id")),
        destination_name=text(row, "destination_name"),
        destination_id=None if destination_id is None else DestinationId(destination_id),
        outcome=DryRunOutcome(text(row, "outcome")),
        paths=tuple(paths.split("\n")) if paths else (),
        facts=None if facts is None else Facts.model_validate(facts, strict=False),
        locator=opt_text(row, "locator"),
        expected_level=None if level is None else VerificationLevel(int(str(level))),
        reason=opt_text(row, "reason"),
        rejected_values=tuple(
            RejectedValue.model_validate(value, strict=False)
            for value in (values if isinstance(values, list) else [])
        ),
    )


async def content_page(
    connection: AsyncConnection, *, after: ContentObjectId | None, limit: int = PAGE_SIZE
) -> list[ContentObjectId]:
    """Content object ids in id order, after `after`."""
    query = select(content_objects.c.id).order_by(content_objects.c.id).limit(limit)
    if after is not None:
        query = query.where(content_objects.c.id > after)
    return [ContentObjectId(row[0]) for row in await connection.execute(query)]


class Evaluator:
    """Evaluates content objects against a template set and a policy, reading the ledger."""

    def __init__(
        self,
        connection: AsyncConnection,
        *,
        run_id: UUID,
        templates: TemplateSet,
        policy: PlacementPolicy,
        destinations: Sequence[DestinationConfig] = (),
    ) -> None:
        """Bind the run, what it evaluates with, and any destinations overriding the policy."""
        self._connection = connection
        self._run_id = run_id
        self._templates = templates
        self._policy = policy
        self._destinations = tuple(destinations)
        self._hosts: dict[tuple[Provider, str], str | None] = {}
        # Locators proposed while this evaluator ran, by destination identity; earlier
        # pages' are read back from the run's recorded items.
        self._proposed: dict[DestinationId, dict[str, ContentObjectId]] = {}

    async def evaluate(self, content_object_id: ContentObjectId) -> list[DryRunItem]:
        """Items for one content object; a failure is reported as an item, never raised."""
        try:
            return await self._evaluate(content_object_id)
        except Exception as error:
            return [
                DryRunItem(
                    run_id=self._run_id,
                    content_object_id=content_object_id,
                    outcome=DryRunOutcome.RENDER_FAILED,
                    reason=f"evaluation failed: {type(error).__name__}: {error}",
                )
            ]

    async def _evaluate(self, content_object_id: ContentObjectId) -> list[DryRunItem]:
        """Items for one content object."""
        sources = await SourceObjectRepository(self._connection).find_by_content_object(
            content_object_id
        )
        paths = tuple(sorted(s.path for s in sources))
        unresolved = DryRunItem(
            run_id=self._run_id,
            content_object_id=content_object_id,
            outcome=DryRunOutcome.UNCLASSIFIED,
            paths=paths,
        )
        reviews = ReviewRepository(self._connection)
        if await reviews.excluded(content_object_id):
            return [unresolved.model_copy(update={"reason": "excluded in review"})]
        review = await reviews.open_for(content_object_id)
        if review is not None:
            return [
                unresolved.model_copy(
                    update={
                        "reason": f"awaiting review ({review.reason.value}): {review.detail}",
                        "rejected_values": review.rejected_values,
                    }
                )
            ]
        classification = await ClassificationRepository(self._connection).get(content_object_id)
        if classification is None:
            return [unresolved.model_copy(update={"reason": "not classified yet"})]
        content = await ContentObjectRepository(self._connection).get(content_object_id)
        if content is None:
            return [unresolved.model_copy(update={"reason": "content object is gone"})]
        facts = classification.facts
        destinations = self._destinations or tuple(
            destination
            for name in self._policy.desired_destinations(facts)
            if (destination := self._policy.destination(name)) is not None
        )
        copied = next((s for s in sources if s.state is SourceObjectState.FINGERPRINTED), None)
        items: list[DryRunItem] = []
        for destination in destinations:
            outcome = await plan_at(
                self._connection,
                self._templates,
                facts,
                content_object_id=content_object_id,
                fingerprint=content.fingerprint,
                paths=paths,
                destination=destination,
            )
            base = DryRunItem(
                run_id=self._run_id,
                content_object_id=content_object_id,
                destination_name=destination.name,
                destination_id=destination.id,
                outcome=DryRunOutcome.RENDER_FAILED,
                paths=paths,
                facts=facts,
            )
            if isinstance(outcome, RenderFailure):
                items.append(base.model_copy(update={"reason": outcome.reason}))
                continue
            locator, present = await self._claim(outcome, content, destination)
            items.append(
                base.model_copy(
                    update={
                        "outcome": (
                            DryRunOutcome.ALREADY_PRESENT if present else DryRunOutcome.PROPOSED
                        ),
                        "locator": locator,
                        "expected_level": await self._expected_level(copied, destination),
                    }
                )
            )
        return items

    async def _claim(
        self, planned: PlacementPlan, content: ContentObject, destination: DestinationConfig
    ) -> tuple[str, bool]:
        """The locator a copy would claim, given what this run proposed; and if it is there.

        Returns:
            The locator, and whether the content already occupies it.
        """
        if planned.already_present:
            return planned.locator, True
        destination_id = destination.id
        proposed = self._proposed.setdefault(destination_id, {})
        placements_repo = PlacementRepository(self._connection)
        store = DryRunStore(self._connection)
        own: set[str] = set()

        async def holder(candidate: str) -> Placement | None:
            """The ledger's occupant of `candidate`, else other content this run put there."""
            occupant = await placements_repo.find_at_locator(
                destination_id=destination_id, locator=candidate
            )
            if occupant is not None:
                if occupant.content_object_id == content.id:
                    own.add(candidate)
                return occupant
            other = proposed.get(candidate) or await store.proposed_for(
                self._run_id, destination_id, candidate
            )
            if other is None or other == content.id:
                return None
            return Placement.build(
                content_object_id=other,
                destination_id=destination_id,
                destination_name=destination.name,
                locator=candidate,
                template_version=planned.rendered.template_version,
            )

        locator, _ = await claim(
            planned.locator,
            content_object_id=content.id,
            fingerprint=content.fingerprint,
            holder=holder,
        )
        if locator in own:
            return locator, True
        proposed[locator] = content.id
        return locator, False

    async def _expected_level(
        self, source: SourceObject | None, destination: DestinationConfig
    ) -> VerificationLevel:
        """The rung a copy from `source` to `destination` could reach."""
        source_algorithms: frozenset[HashAlgorithm] = frozenset()
        if source is not None:
            host = await self._host(source.provider, source.account)
            source_algorithms = advertised_hashes(source.provider, host)
        destination_algorithms = advertised_hashes(
            destination.provider, await self._host(destination.provider, destination.account)
        )
        return expected_level(source_algorithms, destination_algorithms)

    async def _host(self, provider: Provider, account: str) -> str | None:
        """The API host serving an account, if it is connected."""
        key = (provider, account)
        if key not in self._hosts:
            self._hosts[key] = await self._connection.scalar(
                select(provider_connections.c.api_host).where(
                    provider_connections.c.provider == provider.value,
                    provider_connections.c.account == account,
                )
            )
        return self._hosts[key]
