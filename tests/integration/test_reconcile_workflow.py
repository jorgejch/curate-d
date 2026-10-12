"""Reconciliation under DBOS: convergence, overlap, policy change, loss, retention.

Content arrives through the real scan, ingest and classification workflows; placements
run through the local stack's rclone daemon (see `transfer_harness`). Each test activates
a policy of its own over destinations no other run shares, and puts the active policy
back afterwards. Reconciliation is scoped to the test's content, which is what the
scheduled sweep does over every content object.
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from dbos import DBOS, ScheduleInput, SetWorkflowID, WorkflowHandleAsync
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from curate_d.classification.store import ReviewRepository
from curate_d.config import Settings
from curate_d.configuration.documents import parse_policy
from curate_d.configuration.store import ConfigurationRepository
from curate_d.domain import (
    AuditEntry,
    AuditEntryType,
    ConfigurationKind,
    ContentObjectId,
    DestinationConfig,
    HashAlgorithm,
    Placement,
    PlacementPolicy,
    PlacementRule,
    PlacementState,
    Provider,
    RetentionRule,
    ReviewReason,
    VerificationLevel,
)
from curate_d.ledger import ContentObjectRepository, PlacementRepository
from curate_d.ledger.mapping import to_audit_entry
from curate_d.ledger.tables import audit_entries, destination_checks
from curate_d.placement.policy import active_policy
from curate_d.placement.rclone import RcloneClient
from curate_d.reconciliation.store import ReconciliationStore
from curate_d.runtime.workflow_ids import reconcile_workflow_id
from curate_d.workflows import reconcile
from curate_d.workflows.reconcile import (
    ReconcileRequest,
    ReconcileSummary,
    check_destination,
    enqueue_reconcile,
)
from curate_d.workflows.scan import scan_root
from tests.integration.pipeline_harness import SETTLE_TIMEOUT_SECONDS, Pipeline
from tests.integration.transfer_harness import (
    DESTINATION_ACCOUNT,
    ScriptedAccess,
    WorkspaceRemotes,
    rclone_workspace,
    transfer_pipeline,
)
from tests.stubs import media

pytestmark = pytest.mark.integration

type Activate = Callable[[PlacementPolicy], Awaitable[None]]

_DATED = "/Camera/IMG_0001.JPG"
_UNDATED = "/Camera/IMG_0002.JPG"
_TWIN = "/Phone/IMG_0001.JPG"
_OFFLINE = "offline"
_BACKFILL = (datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC))


@pytest.fixture
async def activate(ledger_engine: AsyncEngine) -> AsyncIterator[Activate]:
    """Activate policies for the test; the policy active before it is put back."""
    async with ledger_engine.begin() as connection:
        before = (await active_policy(ConfigurationRepository(connection))).policy

    async def set_policy(policy: PlacementPolicy) -> None:
        """Make `policy` the active one."""
        async with ledger_engine.begin() as connection:
            await ConfigurationRepository(connection).activate(
                ConfigurationKind.POLICY,
                policy.model_dump(mode="json"),
                parse_policy,
                created_by="test",
            )

    yield set_policy
    async with ledger_engine.begin() as connection:
        if (await active_policy(ConfigurationRepository(connection))).policy != before:
            await ConfigurationRepository(connection).activate(
                ConfigurationKind.POLICY,
                before.model_dump(mode="json"),
                parse_policy,
                created_by="test-restore",
            )


@pytest.fixture
async def daemon() -> AsyncIterator[tuple[RcloneClient, str]]:
    """A private directory on the local daemon."""
    async with rclone_workspace() as workspace:
        yield workspace


@pytest.fixture
def names() -> tuple[str, str]:
    """Two destination names no other run shares."""
    tag = uuid4().hex[:8]
    return f"lib-{tag}", f"offsite-{tag}"


def _destination(name: str, *, account: str = DESTINATION_ACCOUNT) -> DestinationConfig:
    """A destination rooted at its own directory."""
    return DestinationConfig(
        name=name, provider=Provider.PCLOUD, account=account, root_path=f"/{name}"
    )


def _policy(*destinations: str, retention: tuple[RetentionRule, ...] = ()) -> PlacementPolicy:
    """Photos go to every destination named."""
    return PlacementPolicy(
        destinations=tuple(_destination(name) for name in destinations),
        rules=(
            PlacementRule(name="photos", when={"category": "Photos"}, destinations=destinations),
        )
        if destinations
        else (),
        retention=retention,
    )


async def _ingest(
    pipeline: Pipeline,
    daemon: tuple[RcloneClient, str],
    *,
    undated: bool = False,
    twin: bool = False,
) -> dict[str, ContentObjectId]:
    """A dated photo on the account and in the daemon.

    Optionally an undated one, and a different photo of the same name and date that
    renders to the same locator.
    """
    rclone, workspace = daemon
    camera = ("Canon", f"EOS {pipeline.account}")
    taken = datetime(2024, 5, 1, 9, 30)  # noqa: DTZ001 - EXIF wall-clock time
    files = {_DATED: media.photo(camera=camera, taken=taken, offset="+02:00")}
    if undated:
        files[_UNDATED] = media.photo(camera=camera, seed=2)
    if twin:
        files[_TWIN] = media.photo(camera=camera, taken=taken, offset="+02:00", seed=3)
    for path, content in files.items():
        pipeline.pcloud.file(path, content)
        directory, name = path.rsplit("/", 1)
        await rclone.upload("/", f"{workspace}/source{directory}", name, content)
    cycle = uuid4().hex[:8]
    with SetWorkflowID(f"test-scan-{pipeline.account}-{cycle}"):
        await scan_root(Provider.PCLOUD.value, pipeline.account, "/", cycle)
    await pipeline.settle()
    ids = {s.path: s.content_object_id for s in await pipeline.sources()}
    assert all(ids.values())
    return {path: content_id for path, content_id in ids.items() if content_id is not None}


async def _reconcile(pipeline: Pipeline, *content_ids: ContentObjectId) -> ReconcileSummary:
    """Reconcile `content_ids`, let the work settle, and return the first part's summary."""
    run = f"test-{uuid4().hex[:10]}"
    await enqueue_reconcile(ReconcileRequest(run=run, content_object_ids=content_ids))
    await pipeline.settle()
    handle: WorkflowHandleAsync[ReconcileSummary] = await DBOS.retrieve_workflow_async(
        reconcile_workflow_id(run, 0)
    )
    return await handle.get_result()


async def _backfill(pipeline: Pipeline, schedule: ScheduleInput) -> None:
    """Apply `schedule`, run the ticks it would have fired on 2026-01-01, then delete it.

    A backfilled tick is enqueued with no executor, which `settle` does not wait for, so
    each is awaited by its identity before the work it enqueued is settled.
    """
    await DBOS.apply_schedules_async([schedule])
    try:
        ticks = await asyncio.to_thread(
            DBOS.backfill_schedule, schedule["schedule_name"], *_BACKFILL
        )
        for tick in ticks:
            handle: WorkflowHandleAsync[None] = await DBOS.retrieve_workflow_async(
                tick.get_workflow_id()
            )
            await asyncio.wait_for(handle.get_result(), SETTLE_TIMEOUT_SECONDS)
        await pipeline.settle()
    finally:
        await DBOS.delete_schedule_async(schedule["schedule_name"])


async def _placements(engine: AsyncEngine, content_id: ContentObjectId) -> dict[str, Placement]:
    """The content's placements by destination."""
    async with engine.connect() as connection:
        found = await PlacementRepository(connection).find_for_content_object(content_id)
    return {p.destination_name: p for p in found}


async def _audit(
    engine: AsyncEngine, content_id: ContentObjectId, entry_type: AuditEntryType, since: int
) -> list[AuditEntry]:
    """Entries of one type for the content, recorded after entry `since`."""
    async with engine.connect() as connection:
        rows = await connection.execute(
            select(audit_entries)
            .where(
                audit_entries.c.content_object_id == content_id,
                audit_entries.c.entry_type == entry_type.value,
                audit_entries.c.id > since,
            )
            .order_by(audit_entries.c.id)
        )
        return [to_audit_entry(row) for row in rows.mappings()]


async def _high_water(engine: AsyncEngine) -> int:
    """The latest audit entry id: entries before it belong to earlier runs."""
    async with engine.connect() as connection:
        value = await connection.scalar(
            select(audit_entries.c.id).order_by(audit_entries.c.id.desc()).limit(1)
        )
    return int(value or 0)


async def test_a_missing_placement_is_created_and_a_satisfied_one_creates_no_work(
    settings: Settings,
    ledger_engine: AsyncEngine,
    tmp_path: Path,
    daemon: tuple[RcloneClient, str],
    activate: Activate,
    names: tuple[str, str],
) -> None:
    """The first pass places the photo; the second finds it there and does nothing."""
    library, _ = names
    await activate(_policy(library))
    access = ScriptedAccess()
    remotes = WorkspaceRemotes(daemon[1])
    async with transfer_pipeline(settings, ledger_engine, tmp_path, remotes, access) as pipeline:
        content_id = (await _ingest(pipeline, daemon))[_DATED]

        first = await _reconcile(pipeline, content_id)
        placement = (await _placements(ledger_engine, content_id))[library]
        assert (first.evaluated, first.placed) == (1, 1)
        assert placement.state is PlacementState.PRESENT
        assert placement.locator == f"/{library}/Photos/2024/2024-05/IMG_0001.JPG"

        second = await _reconcile(pipeline, content_id)
        assert (second.placed, second.satisfied) == (0, 1)
        assert access.copies == 1
        assert (await _placements(ledger_engine, content_id))[library] == placement


async def test_two_contents_rendering_to_one_locator_both_keep_their_bytes(
    settings: Settings,
    ledger_engine: AsyncEngine,
    tmp_path: Path,
    daemon: tuple[RcloneClient, str],
    activate: Activate,
    names: tuple[str, str],
) -> None:
    """Evaluated in one batch, both see the path free; the second to start is suffixed."""
    library, _ = names
    await activate(_policy(library))
    rclone, workspace = daemon
    access = ScriptedAccess()
    async with transfer_pipeline(
        settings, ledger_engine, tmp_path, WorkspaceRemotes(workspace), access
    ) as pipeline:
        ids = await _ingest(pipeline, daemon, twin=True)

        summary = await _reconcile(pipeline, ids[_DATED], ids[_TWIN])

        assert summary.placed == 2
        placed = {
            content_id: (await _placements(ledger_engine, content_id))[library]
            for content_id in (ids[_DATED], ids[_TWIN])
        }
        rendered = f"/{library}/Photos/2024/2024-05/IMG_0001.JPG"
        [suffixed] = {p.locator for p in placed.values()} - {rendered}
        assert suffixed.startswith(f"/{library}/Photos/2024/2024-05/IMG_0001 (")
        async with ledger_engine.connect() as connection:
            contents = ContentObjectRepository(connection)
            for content_id, placement in placed.items():
                assert placement.state is PlacementState.PRESENT
                content = await contents.get(content_id)
                assert content is not None
                held = await rclone.hashsum_file(
                    "/",
                    f"{workspace}/destination{placement.locator}",
                    HashAlgorithm.SHA256,
                    download=True,
                )
                assert held == content.fingerprint

        await _reconcile(pipeline, ids[_DATED], ids[_TWIN])
        assert access.copies == 2


async def test_overlapping_evaluations_perform_one_transfer(
    settings: Settings,
    ledger_engine: AsyncEngine,
    tmp_path: Path,
    daemon: tuple[RcloneClient, str],
    activate: Activate,
    names: tuple[str, str],
) -> None:
    """Two runs over the same content enqueue the same placement identity."""
    library, _ = names
    await activate(_policy(library))
    access = ScriptedAccess()
    remotes = WorkspaceRemotes(daemon[1])
    async with transfer_pipeline(settings, ledger_engine, tmp_path, remotes, access) as pipeline:
        content_id = (await _ingest(pipeline, daemon))[_DATED]

        await asyncio.gather(
            *(
                enqueue_reconcile(
                    ReconcileRequest(
                        run=f"overlap-{uuid4().hex[:8]}", content_object_ids=(content_id,)
                    )
                )
                for _ in range(2)
            )
        )
        await pipeline.settle()

        assert access.copies == 1
        assert list(await _placements(ledger_engine, content_id)) == [library]
        placed = [w for w, _ in await pipeline.workflows("placement:") if library in w]
        assert len(placed) == 1


async def test_tightening_the_policy_adds_placements_and_keeps_existing_ones(
    settings: Settings,
    ledger_engine: AsyncEngine,
    tmp_path: Path,
    daemon: tuple[RcloneClient, str],
    activate: Activate,
    names: tuple[str, str],
) -> None:
    """A second destination is placed; the first placement is left exactly as it was."""
    library, offsite = names
    await activate(_policy(library))
    access = ScriptedAccess()
    remotes = WorkspaceRemotes(daemon[1])
    async with transfer_pipeline(settings, ledger_engine, tmp_path, remotes, access) as pipeline:
        content_id = (await _ingest(pipeline, daemon))[_DATED]
        await _reconcile(pipeline, content_id)
        existing = (await _placements(ledger_engine, content_id))[library]

        await activate(_policy(library, offsite))
        summary = await _reconcile(pipeline, content_id)

        placements = await _placements(ledger_engine, content_id)
        assert (summary.placed, summary.satisfied) == (1, 1)
        assert placements[offsite].state is PlacementState.PRESENT
        assert placements[library] == existing
        assert access.copies == 2


async def test_a_lost_copy_is_marked_absent_and_re_established(
    settings: Settings,
    ledger_engine: AsyncEngine,
    tmp_path: Path,
    daemon: tuple[RcloneClient, str],
    activate: Activate,
    names: tuple[str, str],
) -> None:
    """The check finds the file gone, records it, and reconciliation copies it again."""
    library, _ = names
    await activate(_policy(library))
    rclone, workspace = daemon
    access = ScriptedAccess()
    async with transfer_pipeline(
        settings, ledger_engine, tmp_path, WorkspaceRemotes(workspace), access
    ) as pipeline:
        content_id = (await _ingest(pipeline, daemon))[_DATED]
        await _reconcile(pipeline, content_id)
        placed = (await _placements(ledger_engine, content_id))[library]
        await rclone.delete("/", f"{workspace}/destination{placed.locator}")
        since = await _high_water(ledger_engine)

        with SetWorkflowID(f"test-check-{uuid4().hex[:10]}"):
            assert await check_destination(_destination(library), "c1") == 1
        await pipeline.settle()

        [absent] = await _audit(ledger_engine, content_id, AuditEntryType.PLACEMENT_ABSENT, since)
        assert absent.placement_id == placed.id
        restored = (await _placements(ledger_engine, content_id))[library]
        assert (restored.state, restored.attempts) == (PlacementState.PRESENT, 1)
        assert await rclone.stat("/", f"{workspace}/destination{placed.locator}") is not None
        async with ledger_engine.connect() as connection:
            [check] = await ReconciliationStore(connection).checks([library])
        assert (check.reachable, check.absent) == (True, 1)
        assert access.copies == 2


async def test_an_unreachable_destination_is_recorded_and_its_placements_kept(
    settings: Settings,
    ledger_engine: AsyncEngine,
    tmp_path: Path,
    daemon: tuple[RcloneClient, str],
    activate: Activate,
    names: tuple[str, str],
) -> None:
    """Not being able to look is not the same as finding nothing."""
    library, _ = names
    await activate(_policy(library))
    access = ScriptedAccess()
    remotes = WorkspaceRemotes(daemon[1], offline={_OFFLINE})
    async with transfer_pipeline(settings, ledger_engine, tmp_path, remotes, access) as pipeline:
        content_id = (await _ingest(pipeline, daemon))[_DATED]
        await _reconcile(pipeline, content_id)
        placed = (await _placements(ledger_engine, content_id))[library]

        with SetWorkflowID(f"test-check-{uuid4().hex[:10]}"):
            assert await check_destination(_destination(library, account=_OFFLINE), "c1") == 0

        assert (await _placements(ledger_engine, content_id))[library] == placed
        async with ledger_engine.connect() as connection:
            [check] = await ReconciliationStore(connection).checks([library])
        assert not check.reachable
        assert check.reason is not None
        assert "unreachable" in check.reason
    async with ledger_engine.begin() as connection:
        await connection.execute(
            destination_checks.delete().where(destination_checks.c.destination_name == library)
        )


async def test_retention_follows_the_strongly_verified_copies(
    settings: Settings,
    ledger_engine: AsyncEngine,
    tmp_path: Path,
    daemon: tuple[RcloneClient, str],
    activate: Activate,
    names: tuple[str, str],
) -> None:
    """Retained while the original is needed, permitted with two strong copies, withdrawn
    when one is found weaker; each decision audited with its inputs and rule."""
    library, offsite = names
    rule = RetentionRule(name="archive", release_source=True, required_copies=2)
    await activate(_policy(library, retention=(rule,)))
    access = ScriptedAccess()
    remotes = WorkspaceRemotes(daemon[1])
    async with transfer_pipeline(settings, ledger_engine, tmp_path, remotes, access) as pipeline:
        content_id = (await _ingest(pipeline, daemon))[_DATED]
        since = await _high_water(ledger_engine)

        await _reconcile(pipeline, content_id)  # places the copy; the original is still needed
        await _reconcile(pipeline, content_id)
        await activate(_policy(library, offsite, retention=(rule,)))
        await _reconcile(pipeline, content_id)  # places the second copy
        await _reconcile(pipeline, content_id)
        async with ledger_engine.begin() as connection:
            repository = PlacementRepository(connection)
            second = (await _placements(ledger_engine, content_id))[offsite]
            await repository.upsert(
                second.model_copy(update={"verification_level": VerificationLevel.WEAK})
            )
        await _reconcile(pipeline, content_id)

        decisions = await _audit(ledger_engine, content_id, AuditEntryType.RETENTION_DECIDED, since)
        assert [(d.details["outcome"], d.details["reason"]) for d in decisions] == [
            ("retain", "0 of 2 required copies are strongly verified"),
            ("retain", "the original is one of the 2 required copies; releasing it would leave 1"),
            (
                "release_permitted",
                "2 strongly verified copies meet the 2 required without the original",
            ),
            (
                "retain",
                "the original is one of the 2 required copies; releasing it would leave 1; "
                f"1 weaker copies do not count ({offsite})",
            ),
        ]
        assert decisions[-1].details["rule"] == "archive"
        assert decisions[-1].details["weak_copies"] == offsite
        assert decisions[-1].details["strong_copies"] == library


async def test_decisions_record_the_policy_version_they_applied(
    settings: Settings,
    ledger_engine: AsyncEngine,
    tmp_path: Path,
    daemon: tuple[RcloneClient, str],
    activate: Activate,
    names: tuple[str, str],
) -> None:
    """The retention decision and the copy a run starts name the policy version in force."""
    library, _ = names
    await activate(_policy(library, retention=(RetentionRule(name="keep"),)))
    async with ledger_engine.begin() as connection:
        version = (await active_policy(ConfigurationRepository(connection))).version
    access = ScriptedAccess()
    remotes = WorkspaceRemotes(daemon[1])
    async with transfer_pipeline(settings, ledger_engine, tmp_path, remotes, access) as pipeline:
        content_id = (await _ingest(pipeline, daemon))[_DATED]
        since = await _high_water(ledger_engine)

        await _reconcile(pipeline, content_id)

        [decided] = await _audit(ledger_engine, content_id, AuditEntryType.RETENTION_DECIDED, since)
        assert (decided.details["rule"], decided.details["policy_version"]) == ("keep", version)
        [started] = await _audit(ledger_engine, content_id, AuditEntryType.PLACEMENT_STARTED, since)
        assert started.details["policy_version"] == version
        assert started.details["destination_root"] == f"/{library}"
        assert started.details["destination_account"] == DESTINATION_ACCOUNT


async def test_a_render_failure_opens_one_review_item_and_places_nothing(
    settings: Settings,
    ledger_engine: AsyncEngine,
    tmp_path: Path,
    daemon: tuple[RcloneClient, str],
    activate: Activate,
    names: tuple[str, str],
) -> None:
    """The missing fact is named; a later pass does not audit the same failure again."""
    library, _ = names
    await activate(_policy(library))
    access = ScriptedAccess()
    remotes = WorkspaceRemotes(daemon[1])
    async with transfer_pipeline(settings, ledger_engine, tmp_path, remotes, access) as pipeline:
        ids = await _ingest(pipeline, daemon, undated=True)
        since = await _high_water(ledger_engine)

        summary = await _reconcile(pipeline, *ids.values())
        await _reconcile(pipeline, *ids.values())

        undated = ids[_UNDATED]
        assert (summary.placed, summary.render_failed) == (1, 1)
        assert await _placements(ledger_engine, undated) == {}
        async with ledger_engine.connect() as connection:
            item = await ReviewRepository(connection).open_for(undated)
        assert item is not None
        assert item.reason is ReviewReason.RENDER_FAILED
        assert "captured_at" in item.detail
        [failed] = await _audit(ledger_engine, undated, AuditEntryType.RENDER_FAILED, since)
        assert failed.details["missing"] == "captured_at"


async def test_a_run_hands_over_after_its_bounded_batches(
    settings: Settings,
    ledger_engine: AsyncEngine,
    tmp_path: Path,
    daemon: tuple[RcloneClient, str],
    activate: Activate,
    names: tuple[str, str],
) -> None:
    """One content object per batch, one batch per part: two objects take two parts."""
    library, _ = names
    await activate(_policy(library))
    access = ScriptedAccess()
    remotes = WorkspaceRemotes(daemon[1])
    async with transfer_pipeline(
        settings,
        ledger_engine,
        tmp_path,
        remotes,
        access,
        reconcile_batch_size=1,
        reconcile_batches_per_run=1,
    ) as pipeline:
        ids = sorted((await _ingest(pipeline, daemon, undated=True)).values())
        run = f"bounded-{uuid4().hex[:8]}"
        await enqueue_reconcile(ReconcileRequest(run=run, content_object_ids=tuple(ids)))
        await pipeline.settle()

        parts: list[ReconcileSummary] = []
        for part in range(3):
            handle: WorkflowHandleAsync[ReconcileSummary] = await DBOS.retrieve_workflow_async(
                reconcile_workflow_id(run, part)
            )
            parts.append(await handle.get_result())
        assert [(p.evaluated, p.continued) for p in parts] == [(1, True), (1, True), (0, False)]


async def test_the_sweep_starts_a_run_on_each_scheduled_tick(
    settings: Settings,
    ledger_engine: AsyncEngine,
    tmp_path: Path,
    daemon: tuple[RcloneClient, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every six hours, as configured: each tick starts a run of its own over everything."""
    started: list[ReconcileRequest] = []

    async def record(request: ReconcileRequest) -> str:
        """Record the run instead of sweeping the shared ledger."""
        started.append(request)
        return request.run

    monkeypatch.setattr(reconcile, "enqueue_reconcile", record)
    sweep = reconcile.schedule(settings.model_copy(update={"reconcile_schedule": "0 */6 * * *"}))
    sweep["schedule_name"] = f"{sweep['schedule_name']}-{uuid4().hex[:8]}"
    remotes = WorkspaceRemotes(daemon[1])
    async with transfer_pipeline(
        settings, ledger_engine, tmp_path, remotes, ScriptedAccess()
    ) as pipeline:
        await _backfill(pipeline, sweep)

    backfilled = sorted(
        (r.run, r.part, r.content_object_ids) for r in started if r.run.startswith("sweep-2026")
    )
    assert backfilled == [
        ("sweep-20260101T060000", 0, ()),
        ("sweep-20260101T120000", 0, ()),
        ("sweep-20260101T180000", 0, ()),
    ]


async def test_a_scheduled_check_finds_a_lost_copy_and_re_establishes_it(
    settings: Settings,
    ledger_engine: AsyncEngine,
    tmp_path: Path,
    daemon: tuple[RcloneClient, str],
    activate: Activate,
    names: tuple[str, str],
) -> None:
    """A tick of the check schedule checks each configured destination."""
    library, _ = names
    await activate(_policy(library))
    rclone, workspace = daemon
    access = ScriptedAccess()
    check = reconcile.check_schedule(
        settings.model_copy(update={"destination_check_schedule": "0 2 * * *"})
    )
    check["schedule_name"] = f"{check['schedule_name']}-{uuid4().hex[:8]}"
    async with transfer_pipeline(
        settings, ledger_engine, tmp_path, WorkspaceRemotes(workspace), access
    ) as pipeline:
        content_id = (await _ingest(pipeline, daemon))[_DATED]
        await _reconcile(pipeline, content_id)
        placed = (await _placements(ledger_engine, content_id))[library]
        await rclone.delete("/", f"{workspace}/destination{placed.locator}")
        since = await _high_water(ledger_engine)

        await _backfill(pipeline, check)

        [absent] = await _audit(ledger_engine, content_id, AuditEntryType.PLACEMENT_ABSENT, since)
        assert absent.details["checked"] == "check 20260101T020000"
        restored = (await _placements(ledger_engine, content_id))[library]
        assert (restored.state, restored.attempts) == (PlacementState.PRESENT, 1)
        async with ledger_engine.connect() as connection:
            [recorded] = await ReconciliationStore(connection).checks([library])
        assert (recorded.reachable, recorded.absent) == (True, 1)
        assert access.copies == 2
