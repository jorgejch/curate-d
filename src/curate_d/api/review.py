"""The review queue: what the pipeline could not decide, and a person's decision on it.

An item is resolved by assigning facts, which become the content's classification; or
excluded, which keeps its content out of organization for good. Values an item rejected
can be promoted into the taxonomy first, after which the item can be resolved with them.
Every refusal leaves the item in the queue. Once the taxonomy grows, the workers classify
again every item held back only by values it now contains.
"""

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Annotated, Protocol
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import AwareDatetime, BaseModel, BeforeValidator, Field
from sqlalchemy.ext.asyncio import AsyncEngine

from curate_d.api.configuration import operator
from curate_d.api.errors import NOT_FOUND, REJECTED, NotFoundError, RejectedError, problems
from curate_d.api.reclassification import Reclassify, nothing
from curate_d.classification.resolution import (
    ResolutionError,
    exclusion_audit,
    resolution_summary,
    resolve_with_facts,
    values_to_promote,
)
from curate_d.classification.store import (
    ClassificationRepository,
    ReviewRepository,
    lock_decisions,
)
from curate_d.classification.taxonomy import active_taxonomy, promote
from curate_d.configuration.store import ConfigurationError, ConfigurationRepository
from curate_d.domain import (
    ContentObjectId,
    Facts,
    RejectedValue,
    ReviewItem,
    ReviewReason,
    ReviewStatus,
)
from curate_d.ledger import AuditRepository


def _lax_facts(value: object) -> object:
    """Parse submitted facts from their JSON forms, as a tier's proposal is parsed."""
    return Facts.model_validate(dict(value), strict=False) if isinstance(value, Mapping) else value


SubmittedFacts = Annotated[Facts, BeforeValidator(_lax_facts)]


def _lax_rejected(value: object) -> object:
    """Parse a rejected value from its JSON form, its field an enum's string."""
    if isinstance(value, Mapping):
        return RejectedValue.model_validate(dict(value), strict=False)
    return value


SubmittedRejectedValue = Annotated[RejectedValue, BeforeValidator(_lax_rejected)]


class Entry(BaseModel):
    """An item in review and the facts currently recorded for its content, if any."""

    id: UUID
    content_object_id: ContentObjectId
    reason: ReviewReason
    detail: str
    rejected_values: list[RejectedValue]
    status: ReviewStatus
    created_at: AwareDatetime
    resolved_at: AwareDatetime | None = None
    resolution: str | None = None
    facts: Facts | None = None


class Exclusion(BaseModel):
    """Why the content is being kept out of organization."""

    reason: str = Field(min_length=1)


class Promotion(BaseModel):
    """Which of the item's rejected values to add to the taxonomy; all when empty."""

    values: list[SubmittedRejectedValue] = []


class Promoted(BaseModel):
    """The taxonomy version now in force, and the item still awaiting resolution."""

    taxonomy_version: int
    entry: Entry


class ReviewBackend(Protocol):
    """The review queue and its resolutions."""

    async def open_items(self, *, limit: int, offset: int) -> Sequence[Entry]:
        """Open items, oldest first."""
        ...

    async def entry(self, item_id: UUID) -> Entry:
        """One item, open or not.

        Raises:
            NotFoundError: there is no such item.
        """
        ...

    async def resolve(self, item_id: UUID, facts: Facts, *, by: str) -> Entry:
        """Resolve an item by assigning facts.

        Raises:
            NotFoundError: there is no such item.
            RejectedError: the item is not open, or a fact is outside the taxonomy.
        """
        ...

    async def promote(self, item_id: UUID, values: Sequence[RejectedValue], *, by: str) -> Promoted:
        """Add the item's rejected values to the taxonomy.

        Raises:
            NotFoundError: there is no such item.
            RejectedError: the item is not open or did not reject those values.
        """
        ...

    async def exclude(self, item_id: UUID, reason: str, *, by: str) -> Entry:
        """Keep the item's content out of organization.

        Raises:
            NotFoundError: there is no such item.
            RejectedError: the item is not open.
        """
        ...


def router(backend: Callable[[Request], ReviewBackend]) -> APIRouter:
    """The review endpoints over `backend`."""
    routes = APIRouter(prefix="/review", tags=["review"])
    uses = Depends(backend)
    by = Depends(operator)

    @routes.get("", response_model=list[Entry])
    async def list_open(
        review: Annotated[ReviewBackend, uses],
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> Sequence[Entry]:
        """Items awaiting a person, oldest first."""
        return await review.open_items(limit=limit, offset=offset)

    @routes.get("/{item_id}", response_model=Entry, responses=problems(NOT_FOUND))
    async def get_entry(item_id: UUID, review: Annotated[ReviewBackend, uses]) -> Entry:
        """One item and the facts recorded for its content."""
        return await review.entry(item_id)

    @routes.post("/{item_id}/facts", response_model=Entry, responses=problems(NOT_FOUND, REJECTED))
    async def resolve(
        item_id: UUID,
        facts: SubmittedFacts,
        review: Annotated[ReviewBackend, uses],
        who: Annotated[str, by],
    ) -> Entry:
        """Resolve by assigning facts; the content becomes eligible for placement."""
        return await review.resolve(item_id, facts, by=who)

    @routes.post(
        "/{item_id}/taxonomy", response_model=Promoted, responses=problems(NOT_FOUND, REJECTED)
    )
    async def extend_taxonomy(
        item_id: UUID,
        promotion: Promotion,
        review: Annotated[ReviewBackend, uses],
        who: Annotated[str, by],
    ) -> Promoted:
        """Promote values the item rejected into the taxonomy; resolve with them next."""
        return await review.promote(item_id, promotion.values, by=who)

    @routes.post(
        "/{item_id}/exclusion", response_model=Entry, responses=problems(NOT_FOUND, REJECTED)
    )
    async def exclude(
        item_id: UUID,
        exclusion: Exclusion,
        review: Annotated[ReviewBackend, uses],
        who: Annotated[str, by],
    ) -> Entry:
        """Keep the item's content out of organization."""
        return await review.exclude(item_id, exclusion.reason, by=who)

    return routes


def entry_of(item: ReviewItem, facts: Facts | None) -> Entry:
    """The response for an item."""
    return Entry(
        **item.model_dump(exclude={"rejected_values"}),
        rejected_values=list(item.rejected_values),
        facts=facts,
    )


class LedgerReview:
    """The review queue in the ledger."""

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        reclassify: Reclassify = nothing,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        """Bind the ledger, what to do when the taxonomy grows, and the clock."""
        self._engine = engine
        self._reclassify = reclassify
        self._clock = clock

    async def open_items(self, *, limit: int, offset: int) -> Sequence[Entry]:
        """Open items, oldest first, with their recorded facts."""
        async with self._engine.connect() as connection:
            classifications = ClassificationRepository(connection)
            entries = []
            for item in await ReviewRepository(connection).list_open(limit=limit, offset=offset):
                recorded = await classifications.get(item.content_object_id)
                entries.append(entry_of(item, None if recorded is None else recorded.facts))
            return entries

    async def entry(self, item_id: UUID) -> Entry:
        """One item and its recorded facts."""
        async with self._engine.connect() as connection:
            item = await _item(ReviewRepository(connection), item_id)
            recorded = await ClassificationRepository(connection).get(item.content_object_id)
            return entry_of(item, None if recorded is None else recorded.facts)

    async def resolve(self, item_id: UUID, facts: Facts, *, by: str) -> Entry:
        """Record the facts as the content's classification and close the item."""
        now = self._clock()
        async with self._engine.begin() as connection:
            reviews = ReviewRepository(connection)
            item = await _item(reviews, item_id)
            await lock_decisions(connection, item.content_object_id)
            taxonomy = await active_taxonomy(ConfigurationRepository(connection))
            try:
                result, audit = resolve_with_facts(
                    item,
                    facts,
                    taxonomy.taxonomy,
                    taxonomy_version=taxonomy.version,
                    by=by,
                    now=now,
                )
            except ResolutionError as error:
                raise RejectedError(str(error)) from None
            closed = await reviews.close(
                item_id, ReviewStatus.RESOLVED, resolution_summary(facts), now=now
            )
            if closed is None:
                raise RejectedError(f"review item {item_id} was resolved by someone else")
            await ClassificationRepository(connection).record(result)
            await AuditRepository(connection).append(audit)
            return entry_of(closed, facts)

    async def promote(self, item_id: UUID, values: Sequence[RejectedValue], *, by: str) -> Promoted:
        """Add the item's rejected values to the taxonomy as a new version."""
        try:
            async with self._engine.begin() as connection:
                item = await _item(ReviewRepository(connection), item_id)
                promoted = await promote(
                    ConfigurationRepository(connection),
                    values_to_promote(item, values),
                    promoted_by=by,
                )
                recorded = await ClassificationRepository(connection).get(item.content_object_id)
        except (ResolutionError, ConfigurationError) as error:
            raise RejectedError(str(error)) from None
        except ValueError as error:
            raise RejectedError(f"cannot promote: {error}") from None
        await self._reclassify(promoted.version)
        return Promoted(
            taxonomy_version=promoted.version,
            entry=entry_of(item, None if recorded is None else recorded.facts),
        )

    async def exclude(self, item_id: UUID, reason: str, *, by: str) -> Entry:
        """Close the item as excluded."""
        now = self._clock()
        async with self._engine.begin() as connection:
            reviews = ReviewRepository(connection)
            item = await _item(reviews, item_id)
            await lock_decisions(connection, item.content_object_id)
            try:
                audit = exclusion_audit(item, reason, by=by, now=now)
            except ResolutionError as error:
                raise RejectedError(str(error)) from None
            closed = await reviews.close(
                item_id, ReviewStatus.EXCLUDED, f"excluded: {reason}", now=now
            )
            if closed is None:
                raise RejectedError(f"review item {item_id} was resolved by someone else")
            await AuditRepository(connection).append(audit)
            recorded = await ClassificationRepository(connection).get(item.content_object_id)
            return entry_of(closed, None if recorded is None else recorded.facts)


async def _item(reviews: ReviewRepository, item_id: UUID) -> ReviewItem:
    """An item by id.

    Raises:
        NotFoundError: there is no such item.
    """
    item = await reviews.get(item_id)
    if item is None:
        raise NotFoundError("review item", item_id)
    return item
