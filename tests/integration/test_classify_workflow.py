"""Classification under DBOS: tiers, the review queue, reuse, the spend cap, promotion.

Content arrives through the real scan and ingest workflows; the remote tier is a stub
that answers as told and counts its calls.
"""

import asyncio
import io
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from dbos import SetWorkflowID
from PIL import Image
from PIL.PngImagePlugin import PngInfo
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from curate_d.api.review import LedgerReview
from curate_d.classification.deterministic import TierResult
from curate_d.classification.remote import RemoteOutcome, RemoteRequest, RemoteResult
from curate_d.classification.store import (
    ClassificationRepository,
    ReviewRepository,
    SpendRepository,
)
from curate_d.classification.taxonomy import active_taxonomy, promote
from curate_d.config import Settings
from curate_d.configuration.documents import parse_taxonomy
from curate_d.configuration.store import ConfigurationRepository
from curate_d.content.decode import DEFAULT_MAX_PIXELS
from curate_d.domain import (
    ClassificationResult,
    ClassificationTier,
    ConfigurationKind,
    ContentObjectId,
    Facts,
    MediaKind,
    Provider,
    RejectedValue,
    ReviewItem,
    ReviewReason,
    ReviewStatus,
    Taxonomy,
    TaxonomyField,
)
from curate_d.ledger.mapping import to_audit_entry
from curate_d.ledger.tables import audit_entries, classification_holds
from curate_d.runtime import services
from curate_d.runtime.workflow_ids import classification_workflow_id
from curate_d.workflows.classify import reclassify_rejected, release_holds
from curate_d.workflows.scan import scan_root
from tests.integration.pipeline_harness import Pipeline, running_pipeline
from tests.stubs import media

pytestmark = pytest.mark.integration

type Answer = Callable[[RemoteRequest, Facts], RemoteResult]


class StubRemote:
    """The remote tier, answering from a function of the request."""

    def __init__(self, answer: Answer) -> None:
        """Answer every call with `answer(request, base)`."""
        self.answer = answer
        self.models: list[str] = []

    async def classify(
        self, model: str, request: RemoteRequest, taxonomy: Taxonomy, base: Facts
    ) -> RemoteResult:
        """Record the call and answer it."""
        self.models.append(model)
        return self.answer(request, base)


def answered(subcategory: str, *, cost: str = "0.01") -> Answer:
    """A confident answer filing documents under `subcategory`."""

    def answer(request: RemoteRequest, base: Facts) -> RemoteResult:
        """File as told."""
        return RemoteResult(
            outcome=RemoteOutcome.ANSWERED,
            model_id="stub/model-2026",
            cost_usd=Decimal(cost),
            result=TierResult(
                tier=ClassificationTier.REMOTE_MODEL,
                facts=Facts.model_validate(
                    base.model_dump() | {"category": "Documents", "subcategory": subcategory}
                ),
                confidence=0.93,
                model_id="stub/model-2026",
            ),
        )

    return answer


def _ambiguous(tag: str) -> bytes:
    """A document neither the name nor the words settle."""
    return media.pdf([f"Minutes of the association picnic {tag}", "Weather was fine"])


async def _scan(pipeline: Pipeline) -> None:
    """Scan the account and let everything downstream finish."""
    cycle = uuid4().hex[:8]
    with SetWorkflowID(f"test-scan-{pipeline.account}-{cycle}"):
        await scan_root(Provider.PCLOUD.value, pipeline.account, "/", cycle)
    await pipeline.settle()


async def _classification(
    engine: AsyncEngine, content_object_id: ContentObjectId | None
) -> ClassificationResult | None:
    """The recorded classification."""
    assert content_object_id is not None
    async with engine.connect() as connection:
        return await ClassificationRepository(connection).get(content_object_id)


async def _review(
    engine: AsyncEngine, content_object_id: ContentObjectId | None
) -> ReviewItem | None:
    """The open review item."""
    assert content_object_id is not None
    async with engine.connect() as connection:
        return await ReviewRepository(connection).open_for(content_object_id)


async def _audit_types(engine: AsyncEngine, content_object_id: ContentObjectId | None) -> list[str]:
    """Audit entry types recorded for a content object."""
    async with engine.connect() as connection:
        rows = await connection.execute(
            select(audit_entries).where(audit_entries.c.content_object_id == content_object_id)
        )
        return [to_audit_entry(row).entry_type.value for row in rows.mappings()]


@pytest.fixture
async def restore_taxonomy(ledger_engine: AsyncEngine) -> AsyncIterator[None]:
    """Put the active taxonomy back after a test that promotes into it."""
    async with ledger_engine.begin() as connection:
        before = await active_taxonomy(ConfigurationRepository(connection))
    yield
    async with ledger_engine.begin() as connection:
        repository = ConfigurationRepository(connection)
        if (await active_taxonomy(repository)).taxonomy != before.taxonomy:
            await repository.activate(
                ConfigurationKind.TAXONOMY,
                before.taxonomy.model_dump(mode="json"),
                parse_taxonomy,
                created_by="test-restore",
            )


async def test_a_camera_photo_is_classified_without_any_costlier_tier(
    settings: Settings, ledger_engine: AsyncEngine, tmp_path: Path
) -> None:
    """Deterministic facts are recorded and audited; the remote tier is never called."""
    remote = StubRemote(answered("Finance"))
    async with running_pipeline(settings, ledger_engine, tmp_path, remote=remote) as pipeline:
        # A camera model of its own keeps the bytes new: repeated bytes from an earlier run
        # would find their classification workflow already run.
        photo = media.photo(camera=("Canon", f"EOS {pipeline.account}"))
        pipeline.pcloud.file("/IMG_0001.JPG", photo)
        await _scan(pipeline)
        [source] = await pipeline.sources()
        result = await _classification(ledger_engine, source.content_object_id)
        assert result is not None
        assert result.tier is ClassificationTier.DETERMINISTIC
        assert (result.facts.category, result.facts.subcategory) == ("Photos", "Camera")
        assert result.taxonomy_version is not None
        assert remote.models == []
        assert "classification_decided" in await _audit_types(
            ledger_engine, source.content_object_id
        )


async def test_an_invoice_is_classified_by_the_local_tier(
    settings: Settings, ledger_engine: AsyncEngine, tmp_path: Path
) -> None:
    """The text settles it before the remote tier is needed."""
    remote = StubRemote(answered("Home"))
    async with running_pipeline(settings, ledger_engine, tmp_path, remote=remote) as pipeline:
        pipeline.pcloud.file(
            "/docs/electric.pdf",
            media.pdf(
                [
                    "INVOICE " + pipeline.account,
                    "Total amount due",
                    "Payment by bank",
                    "IBAN",
                    "VAT",
                ]
            ),
        )
        await _scan(pipeline)
        [source] = await pipeline.sources()
        result = await _classification(ledger_engine, source.content_object_id)
        assert result is not None
        assert result.tier is ClassificationTier.LOCAL_INFERENCE
        assert result.facts.subcategory == "Finance"
        assert remote.models == []


async def test_what_the_cheap_tiers_cannot_settle_goes_to_the_configured_model(
    settings: Settings, ledger_engine: AsyncEngine, tmp_path: Path
) -> None:
    """The remote answer is recorded with the model that produced it."""
    remote = StubRemote(answered("Home"))
    async with running_pipeline(settings, ledger_engine, tmp_path, remote=remote) as pipeline:
        pipeline.pcloud.file("/docs/minutes.pdf", _ambiguous(pipeline.account))
        await _scan(pipeline)
        [source] = await pipeline.sources()
        result = await _classification(ledger_engine, source.content_object_id)
        assert result is not None
        assert result.tier is ClassificationTier.REMOTE_MODEL
        assert result.model_id == "stub/model-2026"
        assert remote.models == ["stub/model"]


async def test_malformed_remote_output_goes_to_review_not_the_ledger(
    settings: Settings, ledger_engine: AsyncEngine, tmp_path: Path
) -> None:
    """No invented facts are recorded; the item waits for a person."""

    def malformed(request: RemoteRequest, base: Facts) -> RemoteResult:
        """An answer that did not parse."""
        return RemoteResult(
            outcome=RemoteOutcome.MALFORMED, model_id="stub/model", detail="not JSON"
        )

    async with running_pipeline(
        settings, ledger_engine, tmp_path, remote=StubRemote(malformed)
    ) as pipeline:
        pipeline.pcloud.file("/docs/minutes.pdf", _ambiguous(pipeline.account))
        await _scan(pipeline)
        [source] = await pipeline.sources()
        assert await _classification(ledger_engine, source.content_object_id) is None
        item = await _review(ledger_engine, source.content_object_id)
        assert item is not None
        assert item.reason is ReviewReason.MALFORMED_OUTPUT
        assert "classification_rejected" in await _audit_types(
            ledger_engine, source.content_object_id
        )


async def test_without_a_remote_tier_unsettled_content_goes_to_review(
    settings: Settings, ledger_engine: AsyncEngine, tmp_path: Path
) -> None:
    """Exhausting the tiers routes to review with low confidence."""
    async with running_pipeline(settings, ledger_engine, tmp_path) as pipeline:
        pipeline.pcloud.file("/docs/minutes.pdf", _ambiguous(pipeline.account))
        await _scan(pipeline)
        [source] = await pipeline.sources()
        item = await _review(ledger_engine, source.content_object_id)
        assert item is not None
        assert item.reason is ReviewReason.LOW_CONFIDENCE


def _bomb(tag: str) -> bytes:
    """Return a 450 MP PNG header, above Pillow's own ceiling, distinct per `tag`."""
    return media.png_header(30000, 15000, comment=tag)


def _plain_png(tag: str) -> bytes:
    """Return a 400x300 PNG with no text and no metadata, distinct per `tag`."""
    info = PngInfo()
    info.add_text("Comment", tag)
    buffer = io.BytesIO()
    Image.new("RGB", (400, 300), (90, 120, 150)).save(buffer, format="PNG", pnginfo=info)
    return buffer.getvalue()


@pytest.mark.parametrize(
    ("build", "max_pixels"),
    [
        pytest.param(_bomb, DEFAULT_MAX_PIXELS, id="decompression-bomb-header"),
        pytest.param(_plain_png, 100_000, id="over-budget"),
    ],
)
async def test_an_image_over_the_decode_budget_goes_to_review_without_a_remote_call(
    settings: Settings,
    ledger_engine: AsyncEngine,
    tmp_path: Path,
    build: Callable[[str], bytes],
    max_pixels: int,
) -> None:
    """Refused before decoding: review as resource_limit, the workflow succeeds, no call."""
    remote = StubRemote(answered("Home"))
    async with running_pipeline(
        settings, ledger_engine, tmp_path, remote=remote, cpu_decode_max_pixels=max_pixels
    ) as pipeline:
        content = build(pipeline.account)
        pipeline.pcloud.file(f"/misc/upload-{pipeline.account}.png", content)
        await _scan(pipeline)
        [source] = await pipeline.sources()
        [stored] = await pipeline.contents()
        assert stored.media_type == "image/png"
        assert stored.perceptual_fingerprint is None
        item = await _review(ledger_engine, source.content_object_id)
        assert item is not None
        assert item.reason is ReviewReason.RESOURCE_LIMIT
        assert "decode_budget" in item.detail
        assert remote.models == []
        [(_, status)] = await pipeline.workflows(classification_workflow_id(stored.fingerprint))
        assert status == "SUCCESS"
        assert "classification_rejected" in await _audit_types(
            ledger_engine, source.content_object_id
        )


async def test_repeat_content_reuses_its_classification(
    settings: Settings, ledger_engine: AsyncEngine, tmp_path: Path
) -> None:
    """Two paths, one fingerprint, one remote call; a later copy invokes no tier."""
    remote = StubRemote(answered("Home"))
    async with running_pipeline(settings, ledger_engine, tmp_path, remote=remote) as pipeline:
        content = _ambiguous(pipeline.account)
        pipeline.pcloud.file("/a/minutes.pdf", content)
        pipeline.pcloud.file("/b/minutes copy.pdf", content)
        await _scan(pipeline)
        assert len(remote.models) == 1
        pipeline.pcloud.file("/c/third copy.pdf", content)
        await _scan(pipeline)
        sources = await pipeline.sources()
        assert len({s.content_object_id for s in sources}) == 1
        assert len(remote.models) == 1
        [stored] = await pipeline.contents()
        classify_runs = await pipeline.workflows(classification_workflow_id(stored.fingerprint))
        assert len(classify_runs) == 1


async def test_the_spend_cap_holds_content_instead_of_misclassifying_it(
    settings: Settings, ledger_engine: AsyncEngine, tmp_path: Path
) -> None:
    """Calls proceed below the cap; at the cap content is held, and released next month."""
    remote = StubRemote(answered("Home", cost="0.02"))
    async with running_pipeline(
        settings,
        ledger_engine,
        tmp_path,
        remote=remote,
        remote_spend_cap_usd=Decimal("0.05"),
        remote_call_estimate_usd=Decimal("0.02"),
    ) as pipeline:
        for name in ("one", "two", "three"):
            pipeline.pcloud.file(f"/docs/{name}.pdf", _ambiguous(f"{pipeline.account}-{name}"))
        await _scan(pipeline)

        assert len(remote.models) == 2
        sources = await pipeline.sources()
        classified = [
            s for s in sources if await _classification(ledger_engine, s.content_object_id)
        ]
        assert len(classified) == 2
        async with ledger_engine.connect() as connection:
            status = await SpendRepository(connection).status(pipeline.period)
            held = (await connection.execute(select(classification_holds))).mappings().all()
        assert status is not None
        assert status.spent_usd == Decimal("0.04")
        assert status.cap_reached_at is not None
        held_ids = {row["content_object_id"] for row in held}
        [unclassified] = [s for s in sources if s not in classified]
        assert unclassified.content_object_id in held_ids
        assert await _review(ledger_engine, unclassified.content_object_id) is None

        # The next month: the hold is handed back and classified.
        installed = services.current()
        services.install(replace(installed, clock=lambda: installed.clock() + timedelta(days=40)))
        with SetWorkflowID(f"test-release-{uuid4()}"):
            await release_holds(datetime.now(UTC), None)
        await pipeline.settle()
        assert len(remote.models) == 3
        assert await _classification(ledger_engine, unclassified.content_object_id) is not None


async def test_a_promoted_value_is_accepted_by_later_classification(
    settings: Settings, ledger_engine: AsyncEngine, tmp_path: Path, restore_taxonomy: None
) -> None:
    """Rejected into review, promoted through the procedure, then accepted."""
    remote = StubRemote(answered("Receipts"))
    async with running_pipeline(settings, ledger_engine, tmp_path, remote=remote) as pipeline:
        pipeline.pcloud.file("/docs/first.pdf", _ambiguous(f"{pipeline.account}-1"))
        await _scan(pipeline)
        [first] = await pipeline.sources()
        item = await _review(ledger_engine, first.content_object_id)
        assert item is not None
        assert item.reason is ReviewReason.REJECTED_VALUE
        assert item.rejected_values == (
            RejectedValue(field=TaxonomyField.SUBCATEGORY, value="Receipts", category="Documents"),
        )

        async with ledger_engine.begin() as connection:
            promoted = await promote(
                ConfigurationRepository(connection), item.rejected_values, promoted_by="test"
            )
        assert promoted.taxonomy.accepts(
            TaxonomyField.SUBCATEGORY, "Receipts", category="Documents"
        )

        pipeline.pcloud.file("/docs/second.pdf", _ambiguous(f"{pipeline.account}-2"))
        await _scan(pipeline)
        second = next(s for s in await pipeline.sources() if s.path == "/docs/second.pdf")
        result = await _classification(ledger_engine, second.content_object_id)
        assert result is not None
        assert result.facts.subcategory == "Receipts"
        assert result.taxonomy_version == promoted.version


async def test_content_refused_a_value_is_classified_again_once_the_value_is_promoted(
    settings: Settings, ledger_engine: AsyncEngine, tmp_path: Path, restore_taxonomy: None
) -> None:
    """The item held for Receipts is re-run under the new version, accepted and closed."""
    remote = StubRemote(answered("Receipts"))
    async with running_pipeline(settings, ledger_engine, tmp_path, remote=remote) as pipeline:
        pipeline.pcloud.file("/docs/receipt.pdf", _ambiguous(f"{pipeline.account}-r"))
        await _scan(pipeline)
        [source] = await pipeline.sources()
        item = await _review(ledger_engine, source.content_object_id)
        assert item is not None
        async with ledger_engine.begin() as connection:
            promoted = await promote(
                ConfigurationRepository(connection), item.rejected_values, promoted_by="test"
            )

        with SetWorkflowID(f"test-reclassify-{uuid4()}"):
            assert await reclassify_rejected(promoted.version) == 1
        await pipeline.settle()

        result = await _classification(ledger_engine, source.content_object_id)
        assert result is not None
        assert (result.facts.subcategory, result.taxonomy_version) == ("Receipts", promoted.version)
        assert await _review(ledger_engine, source.content_object_id) is None
        async with ledger_engine.connect() as connection:
            closed = await ReviewRepository(connection).get(item.id)
        assert closed is not None
        assert closed.status is ReviewStatus.RESOLVED
        assert closed.resolution is not None
        assert f"taxonomy {promoted.version}" in closed.resolution
        assert len(remote.models) == 2


class _PausedRemote(StubRemote):
    """Answers the first call at once and holds every later one until released."""

    def __init__(self, answer: Answer) -> None:
        """Hold later calls."""
        super().__init__(answer)
        self.waiting = asyncio.Event()
        self.release = asyncio.Event()

    async def classify(
        self, model: str, request: RemoteRequest, taxonomy: Taxonomy, base: Facts
    ) -> RemoteResult:
        """Answer, after the release for every call but the first."""
        if self.models:
            self.waiting.set()
            await self.release.wait()
        return await super().classify(model, request, taxonomy, base)


async def test_a_person_resolving_during_reclassification_is_not_overwritten(
    settings: Settings, ledger_engine: AsyncEngine, tmp_path: Path, restore_taxonomy: None
) -> None:
    """The model answers after the person resolved: its result is set aside and audited."""
    remote = _PausedRemote(answered("Receipts"))
    async with running_pipeline(settings, ledger_engine, tmp_path, remote=remote) as pipeline:
        pipeline.pcloud.file("/docs/receipt.pdf", _ambiguous(f"{pipeline.account}-race"))
        await _scan(pipeline)
        [source] = await pipeline.sources()
        item = await _review(ledger_engine, source.content_object_id)
        assert item is not None
        async with ledger_engine.begin() as connection:
            promoted = await promote(
                ConfigurationRepository(connection), item.rejected_values, promoted_by="test"
            )

        with SetWorkflowID(f"test-reclassify-{uuid4()}"):
            await reclassify_rejected(promoted.version)
        await asyncio.wait_for(remote.waiting.wait(), timeout=30)
        finance = Facts(media_kind=MediaKind.DOCUMENT, category="Documents", subcategory="Finance")
        await LedgerReview(ledger_engine).resolve(item.id, finance, by="tester")
        remote.release.set()
        await pipeline.settle()

        result = await _classification(ledger_engine, source.content_object_id)
        assert result is not None
        assert (result.tier, result.facts.subcategory) == (
            ClassificationTier.HUMAN_REVIEW,
            "Finance",
        )
        async with ledger_engine.connect() as connection:
            rows = await connection.execute(
                select(audit_entries).where(
                    audit_entries.c.content_object_id == source.content_object_id,
                    audit_entries.c.entry_type == "classification_rejected",
                )
            )
            set_aside = [
                entry
                for entry in (to_audit_entry(row) for row in rows.mappings())
                if "set_aside" in entry.details
            ]
        assert [(e.decided_by, e.details["subcategory"]) for e in set_aside] == [
            ("classification:remote_model", "Receipts")
        ]
