"""Where classification's outcomes are kept: facts, review items, spend and holds.

As with the ledger repositories, each takes a caller-owned connection, which inside a
workflow is the ledger datasource's, and row translation is kept apart from the SQL so it
can be tested without a database.
"""

from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Final
from uuid import UUID, uuid4

from pydantic import AwareDatetime, Field
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from curate_d.classification.clustering import GeoTimePoint
from curate_d.classification.remote import RemoteRequest
from curate_d.domain import (
    ClassificationResult,
    ClassificationTier,
    ContentObjectId,
    DomainModel,
    Facts,
    GeoPoint,
    MediaKind,
    RejectedValue,
    ReviewItem,
    ReviewReason,
    ReviewStatus,
    TaxonomyField,
)
from curate_d.ledger.mapping import (
    ColumnValues,
    Row,
    identifier,
    moment,
    number,
    opt_moment,
    opt_number,
    opt_text,
    text,
)
from curate_d.ledger.tables import (
    classification_holds,
    classifications,
    remote_spend,
    review_items,
)


def _float(row: Row, column: str) -> float | None:
    """A nullable float column."""
    value = row[column]
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"column {column!r} should be a number, got {value!r}")
    return float(value)


def to_classification(row: Row) -> ClassificationResult:
    """Translate a classifications row into a `ClassificationResult`."""
    latitude, longitude = _float(row, "latitude"), _float(row, "longitude")
    confidence = _float(row, "confidence")
    return ClassificationResult(
        content_object_id=ContentObjectId(identifier(row, "content_object_id")),
        facts=Facts(
            media_kind=MediaKind(text(row, "media_kind")),
            category=opt_text(row, "category"),
            subcategory=opt_text(row, "subcategory"),
            title=opt_text(row, "title"),
            captured_at=opt_moment(row, "captured_at"),
            place=(
                None
                if latitude is None or longitude is None
                else GeoPoint(latitude=latitude, longitude=longitude)
            ),
            place_label=opt_text(row, "place_label"),
            event_group=opt_text(row, "event_group"),
            correspondent=opt_text(row, "correspondent"),
            language=opt_text(row, "language"),
        ),
        tier=ClassificationTier(text(row, "tier")),
        confidence=0.0 if confidence is None else confidence,
        produced_at=moment(row, "produced_at"),
        model_id=opt_text(row, "model_id"),
        taxonomy_version=opt_number(row, "taxonomy_version"),
    )


def classification_values(result: ClassificationResult) -> ColumnValues:
    """Flatten a `ClassificationResult` into column values."""
    facts = result.facts
    return {
        "content_object_id": result.content_object_id,
        "tier": result.tier.value,
        "confidence": result.confidence,
        "model_id": result.model_id,
        "taxonomy_version": result.taxonomy_version,
        "produced_at": result.produced_at,
        "media_kind": facts.media_kind.value,
        "category": facts.category,
        "subcategory": facts.subcategory,
        "title": facts.title,
        "captured_at": facts.captured_at,
        "latitude": None if facts.place is None else facts.place.latitude,
        "longitude": None if facts.place is None else facts.place.longitude,
        "place_label": facts.place_label,
        "event_group": facts.event_group,
        "correspondent": facts.correspondent,
        "language": facts.language,
    }


class ClassificationRepository:
    """Facts per content object."""

    def __init__(self, connection: AsyncConnection) -> None:
        """Wrap a caller-owned connection."""
        self._connection = connection

    async def get(self, content_object_id: ContentObjectId) -> ClassificationResult | None:
        """The recorded classification, or None."""
        row = (
            (
                await self._connection.execute(
                    select(classifications).where(
                        classifications.c.content_object_id == content_object_id
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        return to_classification(row) if row is not None else None

    async def record(self, result: ClassificationResult) -> None:
        """Record a classification, replacing any earlier one for the same content."""
        values = classification_values(result)
        statement = insert(classifications).values(**values)
        await self._connection.execute(
            statement.on_conflict_do_update(
                index_elements=["content_object_id"],
                set_={key: statement.excluded[key] for key in values if key != "content_object_id"},
            )
        )

    async def geotagged_photos(self) -> list[GeoTimePoint]:
        """Every photo with both a capture time and a place: the input to event grouping."""
        result = await self._connection.execute(
            select(
                classifications.c.content_object_id,
                classifications.c.captured_at,
                classifications.c.latitude,
                classifications.c.longitude,
            ).where(
                classifications.c.media_kind == MediaKind.PHOTO.value,
                classifications.c.captured_at.is_not(None),
                classifications.c.latitude.is_not(None),
            )
        )
        points: list[GeoTimePoint] = []
        for row in result.mappings():
            latitude, longitude = _float(row, "latitude"), _float(row, "longitude")
            if latitude is None or longitude is None:
                continue
            points.append(
                GeoTimePoint(
                    content_object_id=ContentObjectId(identifier(row, "content_object_id")),
                    captured_at=moment(row, "captured_at"),
                    place=GeoPoint(latitude=latitude, longitude=longitude),
                )
            )
        return points

    async def set_event_groups(
        self, groups: Mapping[ContentObjectId, str | None]
    ) -> list[ContentObjectId]:
        """Write event groups, returning the content objects whose group changed."""
        changed: list[ContentObjectId] = []
        for content_object_id, group in groups.items():
            result = await self._connection.execute(
                update(classifications)
                .where(
                    classifications.c.content_object_id == content_object_id,
                    classifications.c.event_group.is_distinct_from(group),
                )
                .values(event_group=group)
            )
            if result.rowcount:
                changed.append(content_object_id)
        return changed

    async def grouped(self) -> list[ContentObjectId]:
        """Content objects that currently carry an event group."""
        result = await self._connection.execute(
            select(classifications.c.content_object_id).where(
                classifications.c.event_group.is_not(None)
            )
        )
        return [ContentObjectId(row[0]) for row in result]


def _rejected(row: Row) -> tuple[RejectedValue, ...]:
    """The rejected values column."""
    values = row["rejected_values"]
    if not isinstance(values, list):
        raise ValueError("column 'rejected_values' should be a list")
    return tuple(RejectedValue.model_validate(value, strict=False) for value in values)


def to_review_item(row: Row) -> ReviewItem:
    """Translate a review_items row."""
    return ReviewItem(
        id=identifier(row, "id"),
        content_object_id=ContentObjectId(identifier(row, "content_object_id")),
        reason=ReviewReason(text(row, "reason")),
        detail=text(row, "detail"),
        rejected_values=_rejected(row),
        status=ReviewStatus(text(row, "status")),
        created_at=moment(row, "created_at"),
        resolved_at=opt_moment(row, "resolved_at"),
        resolution=opt_text(row, "resolution"),
    )


class RejectedValueCount(DomainModel):
    """How often a value has been rejected across open review items."""

    field: TaxonomyField
    value: str
    category: str | None = None
    occurrences: int = Field(ge=1)


# Serializes the decisions about one content object's classification: a person's and an
# automatic tier's. The advisory lock's second key is the content object's hash.
_DECISION_LOCK_CLASS: Final = 0x63646372  # b"cdcr"


async def lock_decisions(connection: AsyncConnection, content_object_id: ContentObjectId) -> None:
    """Hold the content's classification decisions for the rest of the transaction."""
    await connection.execute(
        select(
            func.pg_advisory_xact_lock(_DECISION_LOCK_CLASS, func.hashtext(str(content_object_id)))
        )
    )


class ReviewRepository:
    """The review queue."""

    def __init__(self, connection: AsyncConnection) -> None:
        """Wrap a caller-owned connection."""
        self._connection = connection

    async def open_item(
        self,
        content_object_id: ContentObjectId,
        reason: ReviewReason,
        detail: str,
        rejected: Sequence[RejectedValue],
        *,
        now: AwareDatetime,
    ) -> ReviewItem:
        """Open an item, or update the content's already-open one with the new reason."""
        existing = await self.open_for(content_object_id)
        values = {
            "reason": reason.value,
            "detail": detail,
            "rejected_values": [value.model_dump(mode="json") for value in rejected],
        }
        if existing is not None:
            row = (
                (
                    await self._connection.execute(
                        review_items.update()
                        .where(review_items.c.id == existing.id)
                        .values(**values)
                        .returning(*review_items.c)
                    )
                )
                .mappings()
                .one()
            )
        else:
            row = (
                (
                    await self._connection.execute(
                        review_items.insert()
                        .values(
                            id=uuid4(),
                            content_object_id=content_object_id,
                            created_at=now,
                            status=ReviewStatus.OPEN.value,
                            **values,
                        )
                        .returning(*review_items.c)
                    )
                )
                .mappings()
                .one()
            )
        return to_review_item(row)

    async def open_for(self, content_object_id: ContentObjectId) -> ReviewItem | None:
        """The content's open item, if it has one."""
        row = (
            (
                await self._connection.execute(
                    select(review_items).where(
                        review_items.c.content_object_id == content_object_id,
                        review_items.c.status == ReviewStatus.OPEN.value,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        return to_review_item(row) if row is not None else None

    async def excluded(self, content_object_id: ContentObjectId) -> bool:
        """Whether a person excluded the content from organization."""
        found = await self._connection.scalar(
            select(review_items.c.id)
            .where(
                review_items.c.content_object_id == content_object_id,
                review_items.c.status == ReviewStatus.EXCLUDED.value,
            )
            .limit(1)
        )
        return found is not None

    async def get(self, item_id: UUID) -> ReviewItem | None:
        """One item by id."""
        row = (
            (
                await self._connection.execute(
                    select(review_items).where(review_items.c.id == item_id)
                )
            )
            .mappings()
            .one_or_none()
        )
        return to_review_item(row) if row is not None else None

    async def list_open(self, *, limit: int, offset: int = 0) -> list[ReviewItem]:
        """Open items, oldest first."""
        result = await self._connection.execute(
            select(review_items)
            .where(review_items.c.status == ReviewStatus.OPEN.value)
            .order_by(review_items.c.created_at, review_items.c.id)
            .limit(limit)
            .offset(offset)
        )
        return [to_review_item(row) for row in result.mappings()]

    async def close(
        self, item_id: UUID, status: ReviewStatus, resolution: str, *, now: AwareDatetime
    ) -> ReviewItem | None:
        """Close an open item as resolved or excluded; None if it was not open."""
        row = (
            (
                await self._connection.execute(
                    review_items.update()
                    .where(
                        review_items.c.id == item_id,
                        review_items.c.status == ReviewStatus.OPEN.value,
                    )
                    .values(status=status.value, resolution=resolution, resolved_at=now)
                    .returning(*review_items.c)
                )
            )
            .mappings()
            .one_or_none()
        )
        return to_review_item(row) if row is not None else None

    async def rejected_value_counts(self) -> list[RejectedValueCount]:
        """Rejected values across open items, most frequent first."""
        counts: dict[tuple[TaxonomyField, str, str | None], int] = {}
        result = await self._connection.execute(
            select(review_items.c.rejected_values).where(
                review_items.c.status == ReviewStatus.OPEN.value
            )
        )
        for row in result.mappings():
            for value in _rejected(row):
                key = (value.field, value.value, value.category)
                counts[key] = counts.get(key, 0) + 1
        return [
            RejectedValueCount(field=field, value=value, category=category, occurrences=count)
            for (field, value, category), count in sorted(
                counts.items(), key=lambda item: (-item[1], item[0][1])
            )
        ]


class SpendStatus(DomainModel):
    """One period's remote spend against its cap."""

    period: str
    spent_usd: Decimal
    cap_usd: Decimal
    calls: int
    cap_reached_at: AwareDatetime | None = None


def _decimal(row: Row, column: str) -> Decimal:
    """A numeric column."""
    value = row[column]
    if not isinstance(value, Decimal):
        raise ValueError(f"column {column!r} should be numeric, got {value!r}")
    return value


def to_spend_status(row: Row) -> SpendStatus:
    """Translate a remote_spend row."""
    return SpendStatus(
        period=text(row, "period"),
        spent_usd=_decimal(row, "spent_usd"),
        cap_usd=_decimal(row, "cap_usd"),
        calls=number(row, "calls"),
        cap_reached_at=opt_moment(row, "cap_reached_at"),
    )


class SpendRepository:
    """Remote spend per period."""

    def __init__(self, connection: AsyncConnection) -> None:
        """Wrap a caller-owned connection."""
        self._connection = connection

    async def reserve(
        self, period: str, estimate: Decimal, cap: Decimal, *, now: AwareDatetime
    ) -> bool:
        """Reserve `estimate` against the period's cap, or record that the cap is reached.

        The row is locked for the check, so two workers cannot both take the last of the
        budget. The cap in force is the configured one at the time of the call.

        Returns:
            Whether the call may go ahead.
        """
        await self._connection.execute(
            insert(remote_spend)
            .values(period=period, cap_usd=cap)
            .on_conflict_do_update(index_elements=["period"], set_={"cap_usd": cap})
        )
        row = (
            (
                await self._connection.execute(
                    select(remote_spend).where(remote_spend.c.period == period).with_for_update()
                )
            )
            .mappings()
            .one()
        )
        status = to_spend_status(row)
        if status.spent_usd + estimate > cap:
            if status.cap_reached_at is None:
                await self._connection.execute(
                    remote_spend.update()
                    .where(remote_spend.c.period == period)
                    .values(cap_reached_at=now)
                )
            return False
        await self._connection.execute(
            remote_spend.update()
            .where(remote_spend.c.period == period)
            .values(
                spent_usd=remote_spend.c.spent_usd + estimate,
                calls=remote_spend.c.calls + 1,
            )
        )
        return True

    async def settle(self, period: str, estimate: Decimal, actual: Decimal) -> None:
        """Replace a reservation's estimate with the call's actual cost."""
        await self._connection.execute(
            remote_spend.update()
            .where(remote_spend.c.period == period)
            .values(spent_usd=func.greatest(remote_spend.c.spent_usd - estimate + actual, 0))
        )

    async def status(self, period: str) -> SpendStatus | None:
        """The period's spend, or None if nothing was spent in it."""
        row = (
            (
                await self._connection.execute(
                    select(remote_spend).where(remote_spend.c.period == period)
                )
            )
            .mappings()
            .one_or_none()
        )
        return to_spend_status(row) if row is not None else None


class Hold(DomainModel):
    """Content held back by the spend cap, with what it needs to be retried."""

    content_object_id: ContentObjectId
    period: str
    held_at: AwareDatetime
    request: RemoteRequest
    base: Facts


class HoldRepository:
    """Content waiting for the next spend period."""

    def __init__(self, connection: AsyncConnection) -> None:
        """Wrap a caller-owned connection."""
        self._connection = connection

    async def hold(self, hold: Hold) -> None:
        """Hold content until a later period; holding it again replaces the hold."""
        values = {
            "period": hold.period,
            "held_at": hold.held_at,
            "request": {
                "request": hold.request.model_dump(mode="json"),
                "base": hold.base.model_dump(mode="json"),
            },
        }
        statement = insert(classification_holds).values(
            content_object_id=hold.content_object_id, **values
        )
        await self._connection.execute(
            statement.on_conflict_do_update(index_elements=["content_object_id"], set_=values)
        )

    async def due(self, period: str) -> list[Hold]:
        """Holds from periods before `period`, oldest first."""
        result = await self._connection.execute(
            select(classification_holds)
            .where(classification_holds.c.period < period)
            .order_by(classification_holds.c.held_at)
        )
        holds: list[Hold] = []
        for row in result.mappings():
            document = row["request"]
            if not isinstance(document, Mapping):
                raise ValueError("column 'request' should be an object")
            holds.append(
                Hold(
                    content_object_id=ContentObjectId(identifier(row, "content_object_id")),
                    period=text(row, "period"),
                    held_at=moment(row, "held_at"),
                    request=RemoteRequest.model_validate(document["request"], strict=False),
                    base=Facts.model_validate(document["base"], strict=False),
                )
            )
        return holds

    async def release(self, content_object_id: ContentObjectId, period: str) -> None:
        """Drop a hold once its content has been handed back to the remote tier.

        Only the hold from `period` is dropped. The retry may already have run and been
        held again by a later period's cap; that replacement stays.
        """
        await self._connection.execute(
            classification_holds.delete().where(
                classification_holds.c.content_object_id == content_object_id,
                classification_holds.c.period == period,
            )
        )
