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
from curate_d.configuration.documents import parse_policy
from curate_d.configuration.store import ConfigurationRepository, ConfigurationVersion
from curate_d.domain import (
    ConfigurationKind,
    ContentObjectId,
    DestinationConfig,
    DryRunOutcome,
    Placement,
    PolicySource,
    Provider,
    VerificationLevel,
)
from curate_d.ledger import ContentObjectRepository, DestinationRepository, PlacementRepository
from curate_d.ledger.tables import (
    audit_entries,
    destinations,
    dry_runs,
    placements,
    review_items,
)
from curate_d.placement.dry_run import (
    NO_DESTINATION,
    DryRun,
    DryRunItem,
    DryRunRequest,
    DryRunStore,
    Evaluator,
    SelectingRule,
)
from curate_d.placement.policy import active_policy
from curate_d.rendering.collision import disambiguate
from curate_d.rendering.planning import active_templates
from curate_d.rendering.templates import parse_templates
from curate_d.workflows.dry_run import DraftRefusedError, start_dry_run
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
        placed = [w for w, _ in await pipeline.workflows("placement:") if str(destination.id) in w]
        assert placed == []


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


async def _policy_state(engine: AsyncEngine) -> tuple[int, list[ConfigurationVersion]]:
    """Return the policy version in force and every policy version."""
    async with engine.begin() as connection:
        configurations = ConfigurationRepository(connection)
        active = await active_policy(configurations)
        return active.version, await configurations.history(ConfigurationKind.POLICY)


async def _dated_photo(pipeline: Pipeline, name: str) -> tuple[str, ContentObjectId]:
    """Scan one new dated photo; return its file name and content object."""
    camera = ("Canon", f"EOS {pipeline.account}")
    photo = pipeline.pcloud.file(
        f"/Camera/{name}",
        media.photo(
            camera=camera,
            taken=datetime(2024, 5, 1, 9, 30),  # noqa: DTZ001 - EXIF wall-clock time
            offset="+02:00",
        ),
    )
    await _scan(pipeline)
    [content_id] = {s.content_object_id for s in await pipeline.sources() if s.content_object_id}
    return photo.name, content_id


async def test_a_draft_policy_decides_and_the_policy_in_force_is_unchanged(
    settings: Settings, ledger_engine: AsyncEngine, tmp_path: Path, run_id: UUID
) -> None:
    """The draft's rules select, items name them, and nothing but the run is written."""
    async with running_pipeline(settings, ledger_engine, tmp_path) as pipeline:
        file_name, content_id = await _dated_photo(pipeline, "IMG_0001.JPG")
        before = await _counts(ledger_engine, {content_id})
        policy_before = await _policy_state(ledger_engine)
        name = f"draft-{uuid4().hex[:8]}"
        draft = parse_policy(
            {
                "destinations": [
                    {
                        "name": name,
                        "provider": "pcloud",
                        "account": pipeline.account,
                        "root_path": "/Drafted",
                    }
                ],
                "rules": [
                    {"name": "photos", "when": {"media_kind": "photo"}, "destinations": [name]},
                    {
                        "name": "archived-photos",
                        "when": {"media_kind": "archive", "category": "Photos"},
                        "destinations": [name],
                    },
                ],
            }
        )

        await start_dry_run(DryRunRequest(run_id=run_id, policy=draft))
        await pipeline.settle()

        async with ledger_engine.connect() as connection:
            store = DryRunStore(connection)
            run = await store.get(run_id)
            items = [i for i in await store.items(run_id) if i.content_object_id == content_id]
        assert run is not None
        assert run.finished_at is not None
        assert (run.policy_source, run.policy_version) == (PolicySource.DRAFT, None)
        assert run.policy_rules == ("photos", "archived-photos")
        assert run.policy_document == draft.model_dump(mode="json")
        assert (run.templates_draft, run.templates_document) == (False, None)
        [item] = items
        assert item.destination_name == name
        assert item.outcome is DryRunOutcome.PROPOSED
        assert item.locator == f"/Drafted/Photos/2024/2024-05/{file_name}"
        assert item.rules == (SelectingRule(index=0, name="photos"),)

        assert await _policy_state(ledger_engine) == policy_before
        assert await _counts(ledger_engine, {content_id}) == before
        assert [w for w, _ in await pipeline.workflows("placement:") if name in w] == []


async def test_content_no_draft_rule_selects_is_reported_unselected(
    settings: Settings, ledger_engine: AsyncEngine, tmp_path: Path, run_id: UUID
) -> None:
    """A photo under a documents-only draft is one unselected item with facts and no destination."""
    async with running_pipeline(settings, ledger_engine, tmp_path) as pipeline:
        _, content_id = await _dated_photo(pipeline, "IMG_0003.JPG")
        name = f"draft-{uuid4().hex[:8]}"
        draft = parse_policy(
            {
                "destinations": [
                    {
                        "name": name,
                        "provider": "pcloud",
                        "account": pipeline.account,
                        "root_path": "/Drafted",
                    }
                ],
                "rules": [
                    {"name": "docs", "when": {"media_kind": "document"}, "destinations": [name]}
                ],
            }
        )

        await start_dry_run(DryRunRequest(run_id=run_id, policy=draft))
        await pipeline.settle()

        async with ledger_engine.connect() as connection:
            items = await DryRunStore(connection).items(run_id)
        [item] = [i for i in items if i.content_object_id == content_id]
        assert item.outcome is DryRunOutcome.UNSELECTED
        assert (item.destination_name, item.rules, item.locator) == (NO_DESTINATION, (), None)
        assert item.facts is not None
        assert item.paths == ("/Camera/IMG_0003.JPG",)


async def test_a_draft_template_set_renders_the_run(
    settings: Settings, ledger_engine: AsyncEngine, tmp_path: Path, run_id: UUID
) -> None:
    """Locators follow the draft layout, and the header says the templates were a draft."""
    async with running_pipeline(settings, ledger_engine, tmp_path) as pipeline:
        camera = ("Canon", f"EOS {pipeline.account}")
        undated = pipeline.pcloud.file("/Camera/IMG_0002.JPG", media.photo(camera=camera, seed=2))
        await _scan(pipeline)
        [content_id] = {
            s.content_object_id for s in await pipeline.sources() if s.content_object_id
        }
        destination = DestinationConfig(
            name=f"dry-{uuid4().hex[:8]}",
            provider=Provider.PCLOUD,
            account=pipeline.account,
            root_path="/Organized",
        )
        templates = parse_templates(
            {
                "version": "draft-flat",
                "templates": [{"name": "flat", "path": "Flat/{original_name}"}],
            }
        )

        await start_dry_run(
            DryRunRequest(run_id=run_id, destinations=(destination,), templates=templates)
        )
        await pipeline.settle()

        async with ledger_engine.connect() as connection:
            store = DryRunStore(connection)
            run = await store.get(run_id)
            [item] = [i for i in await store.items(run_id) if i.content_object_id == content_id]
        assert run is not None
        assert (run.policy_source, run.policy_version) == (PolicySource.DESTINATIONS, None)
        assert run.policy_rules == ()
        assert (run.template_version, run.templates_draft) == ("draft-flat", True)
        assert run.templates_document == templates.model_dump(mode="json")
        assert item.outcome is DryRunOutcome.PROPOSED
        assert item.locator == f"/Organized/Flat/{undated.name}"
        assert item.rules == ()


async def test_a_run_without_drafts_records_the_policy_version_in_force(
    settings: Settings, ledger_engine: AsyncEngine, tmp_path: Path, run_id: UUID
) -> None:
    """The header names the active source and its version."""
    async with running_pipeline(settings, ledger_engine, tmp_path) as pipeline:
        version, _ = await _policy_state(ledger_engine)
        await start_dry_run(DryRunRequest(run_id=run_id))
        await pipeline.settle()
        async with ledger_engine.connect() as connection:
            run = await DryRunStore(connection).get(run_id)
    assert run is not None
    assert (run.policy_source, run.policy_version) == (PolicySource.ACTIVE, version)
    assert (run.templates_draft, run.policy_document) == (False, None)


async def test_a_dry_run_rewound_from_its_start_starts_over(
    settings: Settings, ledger_engine: AsyncEngine, tmp_path: Path, run_id: UUID
) -> None:
    """The header is written again and items the earlier execution recorded are discarded."""
    async with running_pipeline(settings, ledger_engine, tmp_path) as pipeline:
        _, content_id = await _dated_photo(pipeline, "IMG_0005.JPG")
        destination = DestinationConfig(
            name=f"dry-{uuid4().hex[:8]}",
            provider=Provider.PCLOUD,
            account=pipeline.account,
            root_path="/Organized",
        )
        workflow_id = await start_dry_run(DryRunRequest(run_id=run_id, destinations=(destination,)))
        await pipeline.settle()
        handle: WorkflowHandleAsync[int] = await DBOS.retrieve_workflow_async(workflow_id)
        await handle.get_result()
        # Stand in for what an earlier policy recorded: other rules, another destination.
        stale = DryRunItem(
            run_id=run_id,
            content_object_id=content_id,
            destination_name="retired",
            outcome=DryRunOutcome.PROPOSED,
            locator="/retired/IMG_0005.JPG",
            rules=(SelectingRule(index=0, name="retired"),),
        )
        async with ledger_engine.begin() as connection:
            await connection.execute(
                dry_runs.update()
                .where(dry_runs.c.id == run_id)
                .values(policy_source=PolicySource.ACTIVE.value, policy_rules=["retired"])
            )
            await DryRunStore(connection).add([stale])

        rewound = await DBOS.rewind_workflow_async(workflow_id, start_step=1)
        await rewound.get_result()

        async with ledger_engine.connect() as connection:
            store = DryRunStore(connection)
            run = await store.get(run_id)
            items = [i for i in await store.items(run_id) if i.content_object_id == content_id]
    assert run is not None
    assert run.finished_at is not None
    assert (run.policy_source, run.policy_rules) == (PolicySource.DESTINATIONS, ())
    [item] = items
    assert (item.destination_name, item.outcome) == (destination.name, DryRunOutcome.PROPOSED)


async def test_a_draft_the_taxonomy_refuses_at_open_fails_the_run(
    settings: Settings, ledger_engine: AsyncEngine, tmp_path: Path, run_id: UUID
) -> None:
    """The run ends in error naming the problem, and records no header and no items.

    Enqueued without the API's check, the draft stands for one accepted under a taxonomy
    that a later activation replaced before the run opened.
    """
    async with running_pipeline(settings, ledger_engine, tmp_path) as pipeline:
        draft = parse_policy(
            {
                "destinations": [
                    {"name": "drafted", "provider": "pcloud", "account": "1", "root_path": "/D"}
                ],
                "rules": [
                    {"name": "x", "when": {"category": "Fotos"}, "destinations": ["drafted"]}
                ],
            }
        )
        workflow_id = await start_dry_run(DryRunRequest(run_id=run_id, policy=draft))
        await pipeline.settle()
        handle: WorkflowHandleAsync[int] = await DBOS.retrieve_workflow_async(workflow_id)
        with pytest.raises(DraftRefusedError, match="category 'Fotos'"):
            await handle.get_result()
        assert [status for _, status in await pipeline.workflows(workflow_id)] == ["ERROR"]
        async with ledger_engine.connect() as connection:
            store = DryRunStore(connection)
            assert await store.get(run_id) is None
            assert await store.items(run_id) == []


async def test_an_unknown_run_has_no_results(ledger_engine: AsyncEngine) -> None:
    """Asking about a run that never happened is an answer, not an error."""
    async with ledger_engine.connect() as connection:
        assert await DryRunStore(connection).get(uuid4()) is None


async def _twins(pipeline: Pipeline) -> tuple[ContentObjectId, ContentObjectId]:
    """Scan two photos rendering to one locator; return their content ids in id order."""
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
    return first, second


_RENDERED = "/Organized/Photos/2024/2024-05/IMG_0001.JPG"


async def test_a_proposal_at_one_account_does_not_take_the_locator_at_another(
    settings: Settings, ledger_engine: AsyncEngine, tmp_path: Path, run_id: UUID
) -> None:
    """One root and template in two accounts: other content's proposal at one leaves the
    plain locator free at the other."""
    async with running_pipeline(settings, ledger_engine, tmp_path) as pipeline:
        first, _ = await _twins(pipeline)
        here = DestinationConfig(
            name=f"dry-{uuid4().hex[:8]}",
            provider=Provider.PCLOUD,
            account=pipeline.account,
            root_path="/Organized",
        )
        there = here.model_copy(
            update={"name": f"dry-{uuid4().hex[:8]}", "account": f"acct-{uuid4().hex[:8]}"}
        )

        async with ledger_engine.connect() as connection:
            transaction = await connection.begin()
            try:
                configurations = ConfigurationRepository(connection)
                store = DryRunStore(connection)
                await store.create(
                    DryRun(
                        id=run_id,
                        started_at=datetime.now(UTC),
                        template_version="seed-1",
                        taxonomy_version=1,
                    )
                )
                # An earlier page proposed the rendered locator for other content, here only.
                await store.add(
                    [
                        DryRunItem(
                            run_id=run_id,
                            content_object_id=ContentObjectId(uuid4()),
                            destination_name=here.name,
                            destination_id=here.id,
                            outcome=DryRunOutcome.PROPOSED,
                            locator=_RENDERED,
                        )
                    ]
                )
                evaluator = Evaluator(
                    connection,
                    run_id=run_id,
                    templates=(await active_templates(configurations)).templates,
                    policy=(await active_policy(configurations)).policy,
                    destinations=(here, there),
                )
                at_here, at_there = await evaluator.evaluate(first)
                content = await ContentObjectRepository(connection).get(first)
            finally:
                await transaction.rollback()
        assert content is not None
        assert (at_here.destination_id, at_here.locator) == (
            here.id,
            disambiguate(_RENDERED, content.fingerprint),
        )
        assert (at_there.destination_id, at_there.locator, at_there.outcome) == (
            there.id,
            _RENDERED,
            DryRunOutcome.PROPOSED,
        )


async def test_placements_left_at_a_names_previous_account_do_not_count(
    settings: Settings, ledger_engine: AsyncEngine, tmp_path: Path, run_id: UUID
) -> None:
    """A copy at the old account is neither already present nor an occupant at the new one,
    and the run records no destination."""
    async with running_pipeline(settings, ledger_engine, tmp_path) as pipeline:
        first, second = await _twins(pipeline)
        old = DestinationConfig(
            name=f"dry-{uuid4().hex[:8]}",
            provider=Provider.PCLOUD,
            account=f"acct-{uuid4().hex[:8]}",
            root_path="/Organized",
        )
        moved = old.model_copy(update={"account": pipeline.account})
        async with ledger_engine.begin() as connection:
            await DestinationRepository(connection).ensure(old.identity)
            await PlacementRepository(connection).upsert(
                Placement.build(
                    content_object_id=second,
                    destination_id=old.id,
                    destination_name=old.name,
                    locator=_RENDERED,
                    template_version="seed-1",
                )
            )
        try:
            workflow_id = await start_dry_run(DryRunRequest(run_id=run_id, destinations=(moved,)))
            await pipeline.settle()
            handle: WorkflowHandleAsync[int] = await DBOS.retrieve_workflow_async(workflow_id)
            await handle.get_result()

            async with ledger_engine.connect() as connection:
                items = {
                    item.content_object_id: item
                    for item in await DryRunStore(connection).items(run_id)
                    if item.content_object_id in (first, second)
                }
                later = await ContentObjectRepository(connection).get(second)
                recorded = await DestinationRepository(connection).find_many([moved.id])
            assert later is not None
            assert {i.outcome for i in items.values()} == {DryRunOutcome.PROPOSED}
            assert items[first].locator == _RENDERED
            assert items[second].locator == disambiguate(_RENDERED, later.fingerprint)
            assert {i.destination_id for i in items.values()} == {moved.id}
            assert recorded == {}
        finally:
            async with ledger_engine.begin() as connection:
                await connection.execute(
                    delete(placements).where(placements.c.destination_id == old.id)
                )
                await connection.execute(delete(destinations).where(destinations.c.id == old.id))
