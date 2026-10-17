"""The dry run under DBOS: proposed locators and levels, failures reported, nothing placed.

Content arrives through the real scan, ingest and classification workflows. The pipeline
has no transfer services at all, so any attempt to copy would fail loudly.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from dbos import DBOS, SetWorkflowID, WorkflowHandleAsync
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from curate_d.config import Settings
from curate_d.configuration.store import ConfigurationRepository
from curate_d.domain import (
    ContentObjectId,
    DestinationConfig,
    DryRunOutcome,
    Provider,
    VerificationLevel,
)
from curate_d.ledger import ContentObjectRepository
from curate_d.ledger.tables import audit_entries, dry_runs, placements, review_items
from curate_d.placement.dry_run import DryRun, DryRunItem, DryRunRequest, DryRunStore, Evaluator
from curate_d.placement.policy import active_policy
from curate_d.rendering.collision import disambiguate
from curate_d.rendering.planning import active_templates
from curate_d.workflows.dry_run import start_dry_run
from curate_d.workflows.scan import scan_root
from tests.integration.pipeline_harness import Pipeline, running_pipeline
from tests.stubs import media

pytestmark = pytest.mark.integration


@pytest.fixture
async def run_id(ledger_engine: AsyncEngine) -> AsyncIterator[UUID]:
    """A dry run id, and its results deleted afterwards."""
    run = uuid4()
    yield run
    async with ledger_engine.begin() as connection:
        await connection.execute(delete(dry_runs).where(dry_runs.c.id == run))


async def _scan(pipeline: Pipeline) -> None:
    """Scan the account and let ingest and classification finish."""
    cycle = uuid4().hex[:8]
    with SetWorkflowID(f"test-scan-{pipeline.account}-{cycle}"):
        await scan_root(Provider.PCLOUD.value, pipeline.account, "/", cycle)
    await pipeline.settle()


async def _counts(engine: AsyncEngine, content_ids: set[UUID]) -> tuple[int, int, int]:
    """Placements, review items and audit entries for `content_ids`."""
    async with engine.connect() as connection:
        return (
            await connection.scalar(
                select(func.count()).where(placements.c.content_object_id.in_(content_ids))
            ),
            await connection.scalar(
                select(func.count()).where(review_items.c.content_object_id.in_(content_ids))
            ),
            await connection.scalar(
                select(func.count()).where(audit_entries.c.content_object_id.in_(content_ids))
            ),
        )  # type: ignore[return-value]


async def test_a_dry_run_proposes_locators_and_levels_and_places_nothing(
    settings: Settings, ledger_engine: AsyncEngine, tmp_path: Path, run_id: UUID
) -> None:
    """Every object is reported; a render failure does not stop the others."""
    async with running_pipeline(settings, ledger_engine, tmp_path) as pipeline:
        # A camera model of this run's own makes the bytes, and so the content, new: the
        # same bytes from an earlier run would find their classification already decided.
        camera = ("Canon", f"EOS {pipeline.account}")
        dated = pipeline.pcloud.file(
            "/Camera/IMG_0001.JPG",
            media.photo(
                camera=camera,
                taken=datetime(2024, 5, 1, 9, 30),  # noqa: DTZ001 - EXIF wall-clock time
                offset="+02:00",
            ),
        )
        undated = pipeline.pcloud.file("/Camera/IMG_0002.JPG", media.photo(camera=camera, seed=2))
        await _scan(pipeline)
        by_path = {s.path: s for s in await pipeline.sources()}
        content_ids = {s.content_object_id for s in by_path.values() if s.content_object_id}
        assert len(content_ids) == 2
        before = await _counts(ledger_engine, content_ids)  # type: ignore[arg-type]
        destination = DestinationConfig(
            name=f"dry-{uuid4().hex[:8]}",
            provider=Provider.PCLOUD,
            account=pipeline.account,
            root_path="/Organized",
        )

        workflow_id = await start_dry_run(DryRunRequest(run_id=run_id, destinations=(destination,)))
        await pipeline.settle()
        handle: WorkflowHandleAsync[int] = await DBOS.retrieve_workflow_async(workflow_id)
        assert await handle.get_result() >= 2

        async with ledger_engine.connect() as connection:
            store = DryRunStore(connection)
            run = await store.get(run_id)
            items = await store.items(run_id)
        assert run is not None
        assert run.finished_at is not None
        assert run.template_version == "seed-1"
        ours: dict[str, DryRunItem] = {
            item.paths[0]: item for item in items if item.content_object_id in content_ids
        }
        proposed = ours[f"/Camera/{dated.name}"]
        assert proposed.outcome is DryRunOutcome.PROPOSED
        assert proposed.destination_name == destination.name
        assert proposed.destination_id == destination.id
        assert proposed.locator == "/Organized/Photos/2024/2024-05/IMG_0001.JPG"
        assert proposed.expected_level is VerificationLevel.STRONG_COMMON_HASH
        assert proposed.facts is not None
        assert proposed.facts.category == "Photos"
        failed = ours[f"/Camera/{undated.name}"]
        assert failed.outcome is DryRunOutcome.RENDER_FAILED
        assert failed.locator is None
        assert failed.reason is not None
        assert "captured_at" in failed.reason

        assert await _counts(ledger_engine, content_ids) == before  # type: ignore[arg-type]
        assert [
            w for w, _ in await pipeline.workflows("placement:") if str(destination.id) in w
        ] == []


async def test_a_locator_proposed_for_other_content_is_taken(
    settings: Settings, ledger_engine: AsyncEngine, tmp_path: Path, run_id: UUID
) -> None:
    """Two photos render to one locator: the later id is proposed its suffixed form,
    whether the earlier proposal is on the same page or recorded by an earlier one."""
    async with running_pipeline(settings, ledger_engine, tmp_path) as pipeline:
        camera = ("Canon", f"EOS {pipeline.account}")
        taken = datetime(2024, 5, 1, 9, 30)  # noqa: DTZ001 - EXIF wall-clock time
        for folder, seed in (("Camera", 1), ("Phone", 3)):
            pipeline.pcloud.file(
                f"/{folder}/IMG_0001.JPG",
                media.photo(camera=camera, taken=taken, offset="+02:00", seed=seed),
            )
        await _scan(pipeline)
        first, second = sorted(
            ContentObjectId(s.content_object_id)
            for s in await pipeline.sources()
            if s.content_object_id is not None
        )
        destination = DestinationConfig(
            name=f"dry-{uuid4().hex[:8]}",
            provider=Provider.PCLOUD,
            account=pipeline.account,
            root_path="/Organized",
        )
        rendered = "/Organized/Photos/2024/2024-05/IMG_0001.JPG"

        await start_dry_run(DryRunRequest(run_id=run_id, destinations=(destination,)))
        await pipeline.settle()

        async with ledger_engine.connect() as connection:
            items = {
                item.content_object_id: item
                for item in await DryRunStore(connection).items(run_id)
                if item.content_object_id in (first, second)
            }
            contents = ContentObjectRepository(connection)
            first_content = await contents.get(first)
            second_content = await contents.get(second)
        assert first_content is not None and second_content is not None
        assert items[first].locator == rendered
        assert items[second].locator == disambiguate(rendered, second_content.fingerprint)
        assert {i.outcome for i in items.values()} == {DryRunOutcome.PROPOSED}

        # An earlier page recorded the rendered locator for other content.
        async with ledger_engine.connect() as connection:
            transaction = await connection.begin()
            try:
                configurations = ConfigurationRepository(connection)
                store = DryRunStore(connection)
                earlier = uuid4()
                await store.create(
                    DryRun(
                        id=earlier,
                        started_at=datetime.now(UTC),
                        template_version="seed-1",
                        taxonomy_version=1,
                    )
                )
                await store.add(
                    [
                        DryRunItem(
                            run_id=earlier,
                            content_object_id=ContentObjectId(uuid4()),
                            destination_name=destination.name,
                            destination_id=destination.id,
                            outcome=DryRunOutcome.PROPOSED,
                            locator=rendered,
                        )
                    ]
                )
                evaluator = Evaluator(
                    connection,
                    run_id=earlier,
                    templates=(await active_templates(configurations)).templates,
                    policy=(await active_policy(configurations)).policy,
                    destinations=(destination,),
                )
                [item] = await evaluator.evaluate(first)
            finally:
                await transaction.rollback()
        assert item.locator == disambiguate(rendered, first_content.fingerprint)


async def test_one_template_in_two_accounts_is_proposed_plain_at_both(
    settings: Settings, ledger_engine: AsyncEngine, tmp_path: Path, run_id: UUID
) -> None:
    """Two destinations with one root in different accounts do not take each other's paths.

    Two photos render to one locator: at each destination the earlier id keeps it and the
    later is suffixed, exactly as if the other destination did not exist.
    """
    async with running_pipeline(settings, ledger_engine, tmp_path) as pipeline:
        camera = ("Canon", f"EOS {pipeline.account}")
        taken = datetime(2024, 5, 1, 9, 30)  # noqa: DTZ001 - EXIF wall-clock time
        for folder, seed in (("Camera", 1), ("Phone", 3)):
            pipeline.pcloud.file(
                f"/{folder}/IMG_0001.JPG",
                media.photo(camera=camera, taken=taken, offset="+02:00", seed=seed),
            )
        await _scan(pipeline)
        first, second = sorted(
            ContentObjectId(s.content_object_id)
            for s in await pipeline.sources()
            if s.content_object_id is not None
        )
        tag = uuid4().hex[:8]
        destinations = tuple(
            DestinationConfig(
                name=f"dry-{side}-{tag}",
                provider=Provider.PCLOUD,
                account=f"{pipeline.account}-{side}",
                root_path="/Organized",
            )
            for side in ("a", "b")
        )
        rendered = "/Organized/Photos/2024/2024-05/IMG_0001.JPG"

        await start_dry_run(DryRunRequest(run_id=run_id, destinations=destinations))
        await pipeline.settle()

        async with ledger_engine.connect() as connection:
            items = [
                item
                for item in await DryRunStore(connection).items(run_id)
                if item.content_object_id in (first, second)
            ]
            second_content = await ContentObjectRepository(connection).get(second)
        assert second_content is not None
        suffixed = disambiguate(rendered, second_content.fingerprint)
        assert sorted(
            (
                item.destination_id == destinations[1].id,
                item.content_object_id == second,
                item.locator,
            )
            for item in items
        ) == [
            (False, False, rendered),
            (False, True, suffixed),
            (True, False, rendered),
            (True, True, suffixed),
        ]
        assert {i.outcome for i in items} == {DryRunOutcome.PROPOSED}


async def test_a_repeated_dry_run_request_runs_once(
    settings: Settings, ledger_engine: AsyncEngine, tmp_path: Path, run_id: UUID
) -> None:
    """The run id is the workflow identity."""
    async with running_pipeline(settings, ledger_engine, tmp_path) as pipeline:
        first = await start_dry_run(DryRunRequest(run_id=run_id))
        second = await start_dry_run(DryRunRequest(run_id=run_id))
        await pipeline.settle()
        assert first == second
        assert [status for _, status in await pipeline.workflows(first)] == ["SUCCESS"]


async def test_an_unknown_run_has_no_results(ledger_engine: AsyncEngine) -> None:
    """Asking about a run that never happened is an answer, not an error."""
    async with ledger_engine.connect() as connection:
        assert await DryRunStore(connection).get(uuid4()) is None
