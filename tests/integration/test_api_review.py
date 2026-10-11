"""The review queue over the ledger: each resolution path, refusals, and what follows.

Eligibility is checked the way reconciliation sees it: after a resolution the content
yields a placement target, and after an exclusion it yields none.
"""

import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine

from curate_d.api.app import create_app
from curate_d.classification.store import ClassificationRepository, ReviewRepository
from curate_d.config import Settings
from curate_d.configuration.documents import parse_taxonomy
from curate_d.configuration.store import ConfigurationRepository
from curate_d.domain import (
    SEED_TAXONOMY,
    AuditEntryType,
    ClassificationResult,
    ClassificationTier,
    ConfigurationKind,
    ContentObject,
    ContentObjectId,
    DestinationConfig,
    Facts,
    HashAlgorithm,
    MediaKind,
    PlacementPolicy,
    PlacementRule,
    Provider,
    RejectedValue,
    ReviewReason,
    ReviewStatus,
    SourceObject,
    StrongFingerprint,
    TaxonomyField,
)
from curate_d.ledger import ContentObjectRepository, SourceObjectRepository
from curate_d.ledger.tables import (
    audit_entries,
    classifications,
    content_objects,
    review_items,
    source_objects,
)
from curate_d.reconciliation.convergence import evaluate
from curate_d.rendering.templates import SEED_TEMPLATES

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 9, 30, 12, tzinfo=UTC)
_RECEIPTS = RejectedValue(field=TaxonomyField.SUBCATEGORY, value="Receipts", category="Documents")
_POLICY = PlacementPolicy(
    destinations=(
        DestinationConfig(
            name="review-lib", provider=Provider.PCLOUD, account="acct", root_path="/lib"
        ),
    ),
    rules=(
        PlacementRule(name="docs", when={"category": "Documents"}, destinations=("review-lib",)),
    ),
)


@dataclass(frozen=True)
class Queued:
    """A content object with a fingerprinted source and an open review item."""

    content_object_id: ContentObjectId
    item_id: uuid.UUID


@pytest.fixture
async def restore_taxonomy(ledger_engine: AsyncEngine) -> AsyncIterator[None]:
    """Put back the taxonomy in force before the test."""
    async with ledger_engine.begin() as connection:
        before = await ConfigurationRepository(connection).active(ConfigurationKind.TAXONOMY)
    yield
    async with ledger_engine.begin() as connection:
        repository = ConfigurationRepository(connection)
        now = await repository.active(ConfigurationKind.TAXONOMY)
        document = before[1] if before is not None else SEED_TAXONOMY.model_dump(mode="json")
        if now is None or now[1] != document:
            await repository.activate(
                ConfigurationKind.TAXONOMY, document, parse_taxonomy, created_by="test-restore"
            )


@pytest.fixture
async def queued(ledger_engine: AsyncEngine) -> AsyncIterator[Queued]:
    """A document whose subcategory the taxonomy refused, waiting for a person."""
    tag = uuid.uuid4().hex
    async with ledger_engine.begin() as connection:
        content = await ContentObjectRepository(connection).upsert(
            ContentObject.build(
                fingerprint=StrongFingerprint(algorithm=HashAlgorithm.SHA256, digest=tag * 2),
                size_bytes=10,
                media_type="application/pdf",
            )
        )
        sources = SourceObjectRepository(connection)
        source = await sources.upsert(
            SourceObject.build(
                provider=Provider.PCLOUD,
                account=f"review-{tag[:8]}",
                native_id="1",
                path="/docs/receipt.pdf",
                size_bytes=10,
                modified_at=_NOW,
            )
        )
        await sources.attach_content_object(source.id, content.id)
        item = await ReviewRepository(connection).open_item(
            content.id,
            ReviewReason.REJECTED_VALUE,
            "subcategory outside the taxonomy",
            (_RECEIPTS,),
            now=_NOW,
        )
    yield Queued(content_object_id=content.id, item_id=item.id)
    async with ledger_engine.begin() as connection:
        await connection.execute(
            delete(review_items).where(review_items.c.content_object_id == content.id)
        )
        await connection.execute(
            delete(classifications).where(classifications.c.content_object_id == content.id)
        )
        await connection.execute(delete(source_objects).where(source_objects.c.id == source.id))
        await connection.execute(delete(content_objects).where(content_objects.c.id == content.id))


@pytest.fixture
def http(settings: Settings) -> Iterator[TestClient]:
    """The app over the ledger."""
    with TestClient(create_app(settings)) as client:
        yield client


async def _targets(engine: AsyncEngine, content_object_id: ContentObjectId) -> int:
    """How many placements reconciliation would want for the content."""
    async with engine.connect() as connection:
        evaluation = await evaluate(
            connection, content_object_id, templates=SEED_TEMPLATES, policy=_POLICY
        )
    return len(evaluation.targets)


async def test_assigned_facts_resolve_the_item_and_make_the_content_placeable(
    http: TestClient, queued: Queued, ledger_engine: AsyncEngine
) -> None:
    """Recorded at the human-review tier, audited, and wanted at the destination."""
    assert await _targets(ledger_engine, queued.content_object_id) == 0
    response = http.post(
        f"/review/{queued.item_id}/facts",
        json={"media_kind": "document", "category": "Documents", "subcategory": "Finance"},
        headers={"Tailscale-User-Login": "tester"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "resolved"
    async with ledger_engine.connect() as connection:
        recorded = await ClassificationRepository(connection).get(queued.content_object_id)
        decided: list[str] = list(
            (
                await connection.execute(
                    select(audit_entries.c.decided_by).where(
                        audit_entries.c.content_object_id == queued.content_object_id,
                        audit_entries.c.entry_type == AuditEntryType.CLASSIFICATION_DECIDED.value,
                    )
                )
            )
            .scalars()
            .all()
        )
    assert recorded is not None
    assert (recorded.tier, recorded.facts.subcategory) == (
        ClassificationTier.HUMAN_REVIEW,
        "Finance",
    )
    assert decided == ["review:tester"]
    assert http.get(f"/review/{queued.item_id}").json()["status"] == "resolved"
    assert await _targets(ledger_engine, queued.content_object_id) == 1


async def test_facts_outside_the_taxonomy_are_refused_and_the_item_stays(
    http: TestClient, queued: Queued, ledger_engine: AsyncEngine
) -> None:
    """The refused value is named; nothing is recorded."""
    response = http.post(
        f"/review/{queued.item_id}/facts",
        json={"media_kind": "document", "category": "Documents", "subcategory": "Receipts"},
    )

    assert response.status_code == 422
    assert "Receipts" in response.json()["detail"]
    assert http.get(f"/review/{queued.item_id}").json()["status"] == "open"
    async with ledger_engine.connect() as connection:
        assert await ClassificationRepository(connection).get(queued.content_object_id) is None


async def test_promoting_the_rejected_value_lets_the_item_be_resolved_with_it(
    http: TestClient, queued: Queued, restore_taxonomy: None
) -> None:
    """The taxonomy grows a version; then the once-refused value resolves the item."""
    before = http.get("/configuration/taxonomy").json()["version"]
    promoted = http.post(f"/review/{queued.item_id}/taxonomy", json={"values": []})
    resolved = http.post(
        f"/review/{queued.item_id}/facts",
        json={"media_kind": "document", "category": "Documents", "subcategory": "Receipts"},
    )

    assert promoted.status_code == 200
    assert promoted.json()["taxonomy_version"] == before + 1
    assert promoted.json()["entry"]["status"] == "open"
    assert (
        "Receipts"
        in http.get("/configuration/taxonomy").json()["document"]["categories"]["Documents"]
    )
    assert resolved.status_code == 200


async def test_an_excluded_item_is_never_placed(
    http: TestClient, queued: Queued, ledger_engine: AsyncEngine
) -> None:
    """Even with facts recorded, reconciliation wants nothing for excluded content."""
    async with ledger_engine.begin() as connection:
        await ClassificationRepository(connection).record(
            ClassificationResult(
                content_object_id=queued.content_object_id,
                facts=Facts(media_kind=MediaKind.DOCUMENT, category="Documents"),
                tier=ClassificationTier.DETERMINISTIC,
                confidence=1.0,
                produced_at=_NOW,
            )
        )
    assert await _targets(ledger_engine, queued.content_object_id) == 0  # still in review

    excluded = http.post(f"/review/{queued.item_id}/exclusion", json={"reason": "personal"})

    assert excluded.status_code == 200
    assert excluded.json()["status"] == ReviewStatus.EXCLUDED.value
    assert await _targets(ledger_engine, queued.content_object_id) == 0
    again = http.post(f"/review/{queued.item_id}/facts", json={"media_kind": "document"})
    assert again.status_code == 422
    assert "already excluded" in again.json()["detail"]
