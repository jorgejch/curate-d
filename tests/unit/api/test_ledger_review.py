"""`LedgerReview`'s orchestration and error mapping, with its repositories in memory.

What a resolution may record is `classification/resolution.py`'s and is tested there;
these tests pin down what the backend does around it: refusals keep the item open, a
race with another resolution is refused, and promotion failures become refusals.
"""

from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from curate_d.api import review as module
from curate_d.api.errors import NotFoundError, RejectedError
from curate_d.api.review import LedgerReview
from curate_d.classification.taxonomy import ActiveTaxonomy
from curate_d.configuration.store import ConfigurationError
from curate_d.domain import (
    SEED_TAXONOMY,
    AuditEntry,
    ClassificationResult,
    ClassificationTier,
    ConfigurationKind,
    ContentObjectId,
    Facts,
    MediaKind,
    RejectedValue,
    ReviewItem,
    ReviewReason,
    ReviewStatus,
    TaxonomyField,
)

_NOW = datetime(2026, 9, 30, tzinfo=UTC)
_RECEIPTS = RejectedValue(field=TaxonomyField.SUBCATEGORY, value="Receipts", category="Documents")


@dataclass
class _Ledger:
    """What the fake repositories share."""

    items: dict[UUID, ReviewItem] = field(default_factory=dict)
    recorded: list[ClassificationResult] = field(default_factory=list)
    audited: list[AuditEntry] = field(default_factory=list)
    lose_races: bool = False
    promotion_failure: Exception | None = None


class _Engine:
    """Hands out a connection that the fakes ignore."""

    @asynccontextmanager
    async def begin(self) -> AsyncGenerator[object, None]:
        """A transaction."""
        yield object()

    @asynccontextmanager
    async def connect(self) -> AsyncGenerator[object, None]:
        """A connection."""
        yield object()


@pytest.fixture
def ledger(monkeypatch: pytest.MonkeyPatch) -> _Ledger:
    """The fake ledger, wired into the backend's module."""
    state = _Ledger()

    class Reviews:
        def __init__(self, _connection: object) -> None:
            """Share the fake ledger."""

        async def get(self, item_id: UUID) -> ReviewItem | None:
            """An item."""
            return state.items.get(item_id)

        async def list_open(self, *, limit: int, offset: int = 0) -> list[ReviewItem]:
            """Open items."""
            return [i for i in state.items.values() if i.status is ReviewStatus.OPEN]

        async def close(
            self, item_id: UUID, status: ReviewStatus, resolution: str, *, now: datetime
        ) -> ReviewItem | None:
            """Close an item, or lose the race when told to."""
            if state.lose_races:
                return None
            closed = state.items[item_id].model_copy(
                update={"status": status, "resolution": resolution, "resolved_at": now}
            )
            state.items[item_id] = closed
            return closed

    class Classifications:
        def __init__(self, _connection: object) -> None:
            """Share the fake ledger."""

        async def get(self, content_object_id: ContentObjectId) -> ClassificationResult | None:
            """The latest recorded."""
            return next(
                (r for r in reversed(state.recorded) if r.content_object_id == content_object_id),
                None,
            )

        async def record(self, result: ClassificationResult) -> None:
            """Record."""
            state.recorded.append(result)

    class Audit:
        def __init__(self, _connection: object) -> None:
            """Share the fake ledger."""

        async def append(self, entry: AuditEntry) -> int:
            """Append."""
            state.audited.append(entry)
            return len(state.audited)

    async def active(_configurations: object) -> ActiveTaxonomy:
        """The seed, as version 1."""
        return ActiveTaxonomy(version=1, taxonomy=SEED_TAXONOMY)

    async def promote(
        _configurations: object, values: Sequence[RejectedValue], *, promoted_by: str
    ) -> ActiveTaxonomy:
        """Version 2, or the configured failure."""
        if state.promotion_failure is not None:
            raise state.promotion_failure
        return ActiveTaxonomy(version=2, taxonomy=SEED_TAXONOMY)

    async def unlocked(_connection: object, _content_object_id: ContentObjectId) -> None:
        """No other decision runs beside these tests."""

    monkeypatch.setattr(module, "lock_decisions", unlocked)
    monkeypatch.setattr(module, "ReviewRepository", Reviews)
    monkeypatch.setattr(module, "ClassificationRepository", Classifications)
    monkeypatch.setattr(module, "AuditRepository", Audit)
    monkeypatch.setattr(module, "ConfigurationRepository", lambda _connection: None)
    monkeypatch.setattr(module, "active_taxonomy", active)
    monkeypatch.setattr(module, "promote", promote)
    return state


_RECLASSIFIED: list[int] = []


async def _reclassify(version: int) -> None:
    """Record the version re-classification was asked for."""
    _RECLASSIFIED.append(version)


def _backend() -> LedgerReview:
    """The backend over the fake engine."""
    return LedgerReview(cast(AsyncEngine, _Engine()), reclassify=_reclassify, clock=lambda: _NOW)


def _queued(ledger: _Ledger, **changes: object) -> ReviewItem:
    """An open item that rejected a subcategory, in the fake ledger."""
    item = ReviewItem(
        id=uuid4(),
        content_object_id=ContentObjectId(uuid4()),
        reason=ReviewReason.REJECTED_VALUE,
        detail="subcategory outside the taxonomy",
        rejected_values=(_RECEIPTS,),
        created_at=_NOW,
    ).model_copy(update=changes)
    ledger.items[item.id] = item
    return item


_FINANCE = Facts(media_kind=MediaKind.DOCUMENT, category="Documents", subcategory="Finance")


async def test_resolving_records_the_facts_closes_the_item_and_audits(ledger: _Ledger) -> None:
    """All three happen together."""
    item = _queued(ledger)
    entry = await _backend().resolve(item.id, _FINANCE, by="tester")
    assert (entry.status, entry.facts) == (ReviewStatus.RESOLVED, _FINANCE)
    assert [r.content_object_id for r in ledger.recorded] == [item.content_object_id]
    assert [a.decided_by for a in ledger.audited] == ["review:tester"]


async def test_a_refused_resolution_records_nothing(ledger: _Ledger) -> None:
    """The item stays open and nothing is written."""
    item = _queued(ledger)
    with pytest.raises(RejectedError, match="Receipts"):
        await _backend().resolve(
            item.id, _FINANCE.model_copy(update={"subcategory": "Receipts"}), by="tester"
        )
    assert ledger.items[item.id].status is ReviewStatus.OPEN
    assert (ledger.recorded, ledger.audited) == ([], [])


async def test_a_resolution_racing_another_is_refused(ledger: _Ledger) -> None:
    """Whoever closes the item first wins; the other is told."""
    item = _queued(ledger)
    ledger.lose_races = True
    with pytest.raises(RejectedError, match="resolved by someone else"):
        await _backend().resolve(item.id, _FINANCE, by="tester")
    with pytest.raises(RejectedError, match="resolved by someone else"):
        await _backend().exclude(item.id, "personal", by="tester")
    assert ledger.recorded == []


async def test_an_unknown_item_is_not_found_everywhere(ledger: _Ledger) -> None:
    """Every operation names the missing item."""
    backend, missing = _backend(), uuid4()
    for call in (
        backend.entry(missing),
        backend.resolve(missing, _FINANCE, by="tester"),
        backend.promote(missing, [], by="tester"),
        backend.exclude(missing, "x", by="tester"),
    ):
        with pytest.raises(NotFoundError, match=str(missing)):
            await call


async def test_excluding_closes_the_item_and_audits_the_reason(ledger: _Ledger) -> None:
    """Excluded, with the reason on the item and in the audit trail."""
    item = _queued(ledger)
    entry = await _backend().exclude(item.id, "personal", by="tester")
    assert (entry.status, entry.resolution) == (ReviewStatus.EXCLUDED, "excluded: personal")
    assert ledger.audited[0].details["reason"] == "personal"


async def test_excluding_a_closed_item_is_refused(ledger: _Ledger) -> None:
    """Closed stays closed."""
    item = _queued(ledger, status=ReviewStatus.RESOLVED)
    with pytest.raises(RejectedError, match="already resolved"):
        await _backend().exclude(item.id, "personal", by="tester")


async def test_promoting_reports_the_new_version_and_the_open_item(ledger: _Ledger) -> None:
    """The item is still there to resolve."""
    item = _queued(ledger)
    _RECLASSIFIED.clear()
    promoted = await _backend().promote(item.id, [], by="tester")
    assert (promoted.taxonomy_version, promoted.entry.status) == (2, ReviewStatus.OPEN)
    assert _RECLASSIFIED == [2]


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        (ConfigurationError(ConfigurationKind.TAXONOMY, ["categories: bad"]), "categories: bad"),
        (ValueError("subcategory 'Receipts' needs category 'Documents'"), "cannot promote"),
    ],
)
async def test_a_failed_promotion_is_refused_with_its_reason(
    ledger: _Ledger, failure: Exception, message: str
) -> None:
    """Configuration and promotion errors both become refusals."""
    item = _queued(ledger)
    ledger.promotion_failure = failure
    with pytest.raises(RejectedError, match=message):
        await _backend().promote(item.id, [], by="tester")


async def test_open_items_carry_their_recorded_facts(ledger: _Ledger) -> None:
    """A render failure's item shows the facts that could not be rendered."""
    item = _queued(ledger, reason=ReviewReason.RENDER_FAILED, rejected_values=())
    ledger.recorded.append(
        ClassificationResult(
            content_object_id=item.content_object_id,
            facts=_FINANCE,
            tier=ClassificationTier.DETERMINISTIC,
            confidence=1.0,
            produced_at=_NOW,
        )
    )
    [entry] = await _backend().open_items(limit=10, offset=0)
    assert entry.facts == _FINANCE
    assert (await _backend().entry(item.id)).facts == _FINANCE
