"""The organizer end to end over the fixture library, on the local engines.

Scan, stage through RustFS, fingerprint, classify, render, place through the rclone daemon
and reconcile, in one process playing both worker profiles. The same cycle in the
Kubernetes dev environment, against OVH staging, is run there.

Reconciliation is scoped to the library's content, which is what the scheduled sweep does
over every content object.
"""

import asyncio
import hashlib
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from dbos import DBOS, SetWorkflowID, WorkflowHandleAsync
from fastapi.testclient import TestClient
from sqlalchemy import ColumnElement, delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from curate_d.api.app import create_app
from curate_d.config import Settings
from curate_d.configuration.documents import parse_policy, parse_taxonomy
from curate_d.configuration.store import ConfigurationRepository, DocumentValidator
from curate_d.domain import (
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
    VerificationLevel,
)
from curate_d.ledger import PlacementRepository
from curate_d.ledger.tables import (
    audit_entries,
    classifications,
    dry_runs,
    placements,
    staged_objects,
)
from curate_d.placement.rclone import RcloneClient
from curate_d.rendering.templates import SEED_TEMPLATES, parse_templates
from curate_d.runtime.workflow_ids import classification_workflow_id, reconcile_workflow_id
from curate_d.staging.keys import derive_staging_key
from curate_d.workflows.classify import reclassification_workflow_id
from curate_d.workflows.reconcile import (
    ReconcileRequest,
    ReconcileSummary,
    check_destination,
    enqueue_reconcile,
)
from curate_d.workflows.scan import scan_root
from tests.integration import fixture_library as lib
from tests.integration.fixture_library import Library, LibraryRemote
from tests.integration.pipeline_harness import Pipeline
from tests.integration.rustfs_staging import rustfs_staging
from tests.integration.transfer_harness import (
    DESTINATION_ACCOUNT,
    ScriptedAccess,
    WorkspaceRemotes,
    rclone_workspace,
    transfer_pipeline,
)

pytestmark = pytest.mark.integration

type Activate = Callable[[ConfigurationKind, dict[str, object]], Awaitable[None]]
_PARSERS: dict[ConfigurationKind, DocumentValidator] = {
    ConfigurationKind.TAXONOMY: parse_taxonomy,
    ConfigurationKind.TEMPLATES: parse_templates,
    ConfigurationKind.POLICY: parse_policy,
}


@pytest.fixture
async def activate(ledger_engine: AsyncEngine) -> AsyncIterator[Activate]:
    """Activate configuration for the test; each kind's document in force is put back."""
    before: dict[ConfigurationKind, dict[str, object] | None] = {}
    async with ledger_engine.begin() as connection:
        repository = ConfigurationRepository(connection)
        for kind in _PARSERS:
            active = await repository.active(kind)
            before[kind] = None if active is None else dict(active[1])

    async def set_document(kind: ConfigurationKind, document: dict[str, object]) -> None:
        """Make `document` the one in force."""
        async with ledger_engine.begin() as connection:
            await ConfigurationRepository(connection).activate(
                kind, document, _PARSERS[kind], created_by="test-e2e"
            )

    yield set_document
    async with ledger_engine.begin() as connection:
        repository = ConfigurationRepository(connection)
        for kind, document in before.items():
            active = await repository.active(kind)
            if document is not None and active is not None and dict(active[1]) != document:
                await repository.activate(kind, document, _PARSERS[kind], created_by="test-e2e")


@pytest.fixture
async def daemon() -> AsyncIterator[tuple[RcloneClient, str]]:
    """A private directory on the local rclone daemon."""
    async with rclone_workspace() as workspace:
        yield workspace


@pytest.fixture
def destination() -> str:
    """A destination name no other run shares."""
    return f"library-{uuid.uuid4().hex[:8]}"


def _policy(destination: str, *, retention: tuple[RetentionRule, ...] = ()) -> dict[str, object]:
    """Photos, documents and audio all go to `destination`."""
    return PlacementPolicy(
        destinations=(
            DestinationConfig(
                name=destination,
                provider=Provider.PCLOUD,
                account=DESTINATION_ACCOUNT,
                root_path=f"/{destination}",
            ),
        ),
        rules=tuple(
            PlacementRule(
                name=category.lower(), when={"category": category}, destinations=(destination,)
            )
            for category in ("Photos", "Documents", "Audio")
        ),
        retention=retention,
    ).model_dump(mode="json")


@asynccontextmanager
async def _organizer(
    settings: Settings,
    ledger_engine: AsyncEngine,
    tmp_path: Path,
    daemon: tuple[RcloneClient, str],
    access: ScriptedAccess,
    remote: LibraryRemote,
    *,
    resume: Pipeline | None = None,
) -> AsyncGenerator[Pipeline, None]:
    """The organizer on RustFS staging and the rclone daemon."""
    async with (
        rustfs_staging(settings, tmp_path / "staging") as staging,
        transfer_pipeline(
            settings,
            ledger_engine,
            tmp_path,
            WorkspaceRemotes(daemon[1]),
            access,
            remote=remote,
            staging=staging,
            resume=resume,
        ) as pipeline,
    ):
        yield pipeline


async def _scan(pipeline: Pipeline) -> None:
    """Scan the account and let staging, fingerprinting and classification finish."""
    cycle = uuid.uuid4().hex[:8]
    with SetWorkflowID(f"test-scan-{pipeline.account}-{cycle}"):
        await scan_root(Provider.PCLOUD.value, pipeline.account, "/", cycle)
    await pipeline.settle()


async def _content_ids(pipeline: Pipeline) -> list[ContentObjectId]:
    """The library's content objects."""
    return sorted({s.content_object_id for s in await pipeline.sources() if s.content_object_id})


async def _reconcile(pipeline: Pipeline) -> ReconcileSummary:
    """Reconcile the library's content, let placement finish, return the first part."""
    run = f"e2e-{uuid.uuid4().hex[:10]}"
    await enqueue_reconcile(
        ReconcileRequest(run=run, content_object_ids=tuple(await _content_ids(pipeline)))
    )
    await pipeline.settle()
    handle: WorkflowHandleAsync[ReconcileSummary] = await DBOS.retrieve_workflow_async(
        reconcile_workflow_id(run, 0)
    )
    return await handle.get_result()


async def _cycle(pipeline: Pipeline, library: Library, daemon: tuple[RcloneClient, str]) -> None:
    """Load the library and organize it."""
    await lib.load(library, pipeline.pcloud, daemon)
    await _scan(pipeline)
    await _reconcile(pipeline)


async def _content_of(pipeline: Pipeline, path: str) -> ContentObjectId:
    """The content object the file at `path` resolved to."""
    [source] = [s for s in await pipeline.sources() if s.path == path]
    assert source.content_object_id is not None
    return source.content_object_id


async def _placed(
    engine: AsyncEngine, content: ContentObjectId, destination: str
) -> Placement | None:
    """The content's placement at `destination`, or None."""
    async with engine.connect() as connection:
        found = await PlacementRepository(connection).find_for_content_object(content)
    return next((p for p in found if p.destination_name == destination), None)


async def _held_sha256(daemon: tuple[RcloneClient, str], side: str, path: str) -> str | None:
    """The SHA-256 of a file in the daemon's workspace, or None if it is not there."""
    rclone, workspace = daemon
    if await rclone.stat("/", f"{workspace}/{side}{path}") is None:
        return None
    digest = await rclone.hashsum_file(
        "/", f"{workspace}/{side}{path}", HashAlgorithm.SHA256, download=True
    )
    return digest.digest


async def _count(
    engine: AsyncEngine, column: ColumnElement[uuid.UUID], content_ids: list[ContentObjectId]
) -> int:
    """Rows whose content object `column` names one of `content_ids`."""
    async with engine.connect() as connection:
        return int(
            await connection.scalar(select(func.count()).where(column.in_(content_ids))) or 0
        )


async def test_the_library_loads_into_the_test_provider(
    settings: Settings,
    ledger_engine: AsyncEngine,
    tmp_path: Path,
    daemon: tuple[RcloneClient, str],
) -> None:
    """13.1: every fixture is listed by a scan with its size."""
    async with _organizer(
        settings, ledger_engine, tmp_path, daemon, ScriptedAccess(), LibraryRemote()
    ) as pipeline:
        library = lib.library(pipeline.account)
        await lib.load(library, pipeline.pcloud, daemon)
        await _scan(pipeline)
        recorded = {s.path: s.size_bytes for s in await pipeline.sources()}

    assert recorded == {f.path: len(f.content) for f in library.fixtures}


async def test_a_full_cycle_files_every_file_where_expected_and_verified(
    settings: Settings,
    ledger_engine: AsyncEngine,
    tmp_path: Path,
    daemon: tuple[RcloneClient, str],
    activate: Activate,
    destination: str,
) -> None:
    """13.2: each organizable file at its locator, verified on the common-hash rung, its
    bytes intact; the rest unresolved and placed nowhere; staged through S3."""
    await activate(ConfigurationKind.POLICY, _policy(destination))
    async with _organizer(
        settings, ledger_engine, tmp_path, daemon, ScriptedAccess(), LibraryRemote()
    ) as pipeline:
        library = lib.library(pipeline.account)
        await _cycle(pipeline, library, daemon)

        for fixture in library.placed:
            placement = await _placed(
                ledger_engine, await _content_of(pipeline, fixture.path), destination
            )
            assert placement is not None, fixture.path
            assert placement.state is PlacementState.PRESENT, fixture.path
            assert placement.locator == f"/{destination}/{fixture.locator}"
            assert placement.verification_level is VerificationLevel.STRONG_COMMON_HASH
            assert await _held_sha256(daemon, "destination", placement.locator) == (
                hashlib.sha256(fixture.content).hexdigest()
            )
        for fixture in library.unresolved:
            content = await _content_of(pipeline, fixture.path)
            assert await _placed(ledger_engine, content, destination) is None, fixture.path
        reviews = {f.path: f for f in library.unresolved}
        assert set(reviews) == {lib.PICNIC, lib.CLUB, lib.BLOB}
        keys = [str(derive_staging_key(s.id)) for s in await pipeline.sources()]
        async with ledger_engine.connect() as connection:
            staged = await connection.scalar(
                select(func.count())
                .select_from(staged_objects)
                .where(staged_objects.c.staging_key.in_(keys))
            )
        # Staged through RustFS: the S3 backend's index holds every fetched source, and the
        # in-memory staging the harness would otherwise install was never written to.
        assert staged == len(keys)
        assert pipeline.staging.index.registrations == {}


async def test_a_second_cycle_transfers_nothing_and_records_nothing_twice(
    settings: Settings,
    ledger_engine: AsyncEngine,
    tmp_path: Path,
    daemon: tuple[RcloneClient, str],
    activate: Activate,
    destination: str,
) -> None:
    """13.3: the same library organized again changes nothing."""
    await activate(ConfigurationKind.POLICY, _policy(destination))
    access, remote = ScriptedAccess(), LibraryRemote()
    async with _organizer(settings, ledger_engine, tmp_path, daemon, access, remote) as pipeline:
        library = lib.library(pipeline.account)
        await _cycle(pipeline, library, daemon)
        content_ids = await _content_ids(pipeline)

        async def snapshot() -> tuple[int, ...]:
            """Ledger rows for the library."""
            return (
                len(await pipeline.sources()),
                len(content_ids),
                await _count(ledger_engine, placements.c.content_object_id, content_ids),
                await _count(ledger_engine, classifications.c.content_object_id, content_ids),
                await _count(ledger_engine, audit_entries.c.content_object_id, content_ids),
            )

        before, copies, calls = await snapshot(), access.copies, len(remote.calls)
        await _scan(pipeline)
        summary = await _reconcile(pipeline)

        assert summary.placed == 0
        assert access.copies == copies
        assert len(remote.calls) == calls
        assert await _content_ids(pipeline) == content_ids
        assert await snapshot() == before


async def test_identical_content_at_two_paths_is_one_object_classified_once(
    settings: Settings,
    ledger_engine: AsyncEngine,
    tmp_path: Path,
    daemon: tuple[RcloneClient, str],
    activate: Activate,
    destination: str,
) -> None:
    """13.4: the photo and its backup copy share a content object, a classification and a
    placement."""
    await activate(ConfigurationKind.POLICY, _policy(destination))
    async with _organizer(
        settings, ledger_engine, tmp_path, daemon, ScriptedAccess(), LibraryRemote()
    ) as pipeline:
        library = lib.library(pipeline.account)
        await _cycle(pipeline, library, daemon)

        original = await _content_of(pipeline, lib.GPS_PHOTO)
        assert await _content_of(pipeline, lib.COPY_OF_GPS_PHOTO) == original
        [content] = [c for c in await pipeline.contents() if c.id == original]
        runs = await pipeline.workflows(classification_workflow_id(content.fingerprint))
        assert [status for _, status in runs] == ["SUCCESS"]
        assert await _count(ledger_engine, classifications.c.content_object_id, [original]) == 1
        assert await _count(ledger_engine, placements.c.content_object_id, [original]) == 1


async def test_a_failed_verification_releases_nothing_and_is_recorded(
    settings: Settings,
    ledger_engine: AsyncEngine,
    tmp_path: Path,
    daemon: tuple[RcloneClient, str],
    activate: Activate,
    destination: str,
) -> None:
    """13.5: every copy fails verification; no placement is registered, the originals are
    untouched, the failure is audited, and retention keeps every original."""
    release = RetentionRule(name="release-after-one", release_source=True, required_copies=1)
    await activate(ConfigurationKind.POLICY, _policy(destination, retention=(release,)))
    access = ScriptedAccess()
    access.corrupt = 'corrupted on transfer: sha1 hashes differ src(a:) "aa11" vs dst(b:) "bb22"'
    async with _organizer(
        settings, ledger_engine, tmp_path, daemon, access, LibraryRemote()
    ) as pipeline:
        library = lib.library(pipeline.account)
        await _cycle(pipeline, library, daemon)

        for fixture in library.placed:
            content = await _content_of(pipeline, fixture.path)
            placement = await _placed(ledger_engine, content, destination)
            assert placement is not None
            assert placement.state is PlacementState.UNTRUSTED, fixture.path
            assert await _held_sha256(daemon, "source", fixture.path) == (
                hashlib.sha256(fixture.content).hexdigest()
            )
            stub = next(f for f in pipeline.pcloud.files.values() if f.name == fixture.name)
            assert stub.content == fixture.content
            async with ledger_engine.connect() as connection:
                recorded = (
                    await connection.execute(
                        select(audit_entries.c.entry_type, audit_entries.c.details)
                        .where(audit_entries.c.content_object_id == content)
                        .order_by(audit_entries.c.id)
                    )
                ).all()
            types = [row[0] for row in recorded]
            assert AuditEntryType.VERIFICATION_FAILED.value in types, fixture.path
            assert AuditEntryType.PLACEMENT_REGISTERED.value not in types
            decisions = [row[1] for row in recorded if row[0] == "retention_decided"]
            assert decisions and all(d["outcome"] == "retain" for d in decisions), fixture.path


async def test_workers_killed_mid_transfer_finish_the_cycle_on_restart(
    settings: Settings,
    ledger_engine: AsyncEngine,
    tmp_path: Path,
    daemon: tuple[RcloneClient, str],
    activate: Activate,
    destination: str,
) -> None:
    """13.6: stopped while copying, restarted under the same identity; every file is placed
    once and none is lost."""
    await activate(ConfigurationKind.POLICY, _policy(destination))
    access, remote = ScriptedAccess(), LibraryRemote()
    async with _organizer(settings, ledger_engine, tmp_path, daemon, access, remote) as first:
        library = lib.library(first.account)
        await lib.load(library, first.pcloud, daemon)
        await _scan(first)
        access.crash = asyncio.Event()
        await enqueue_reconcile(
            ReconcileRequest(
                run=f"e2e-crash-{uuid.uuid4().hex[:8]}",
                content_object_ids=tuple(await _content_ids(first)),
            )
        )
        await asyncio.wait_for(access.paused.wait(), timeout=60)
        first.terminate()

    access.crash = None
    async with _organizer(
        settings, ledger_engine, tmp_path, daemon, access, remote, resume=first
    ) as second:
        await second.settle()
        placed = {}
        for fixture in library.placed:
            content = await _content_of(second, fixture.path)
            placement = await _placed(ledger_engine, content, destination)
            assert placement is not None, fixture.path
            assert placement.state is PlacementState.PRESENT, fixture.path
            assert await _held_sha256(daemon, "destination", placement.locator) == (
                hashlib.sha256(fixture.content).hexdigest()
            )
            placed[content] = placement.locator
        held = await daemon[0].list_files(f"/{daemon[1]}/destination", destination)
        assert sorted(f"/{destination}/{path}" for path in held) == sorted(placed.values())
        content_ids = await _content_ids(second)
        assert await _count(ledger_engine, placements.c.content_object_id, content_ids) == len(
            placed
        )


async def test_a_new_layout_moves_placements_without_classifying_again(
    settings: Settings,
    ledger_engine: AsyncEngine,
    tmp_path: Path,
    daemon: tuple[RcloneClient, str],
    activate: Activate,
    destination: str,
) -> None:
    """13.7: photos move to the new layout; nothing is classified again."""
    await activate(ConfigurationKind.POLICY, _policy(destination))
    remote = LibraryRemote()
    async with _organizer(
        settings, ledger_engine, tmp_path, daemon, ScriptedAccess(), remote
    ) as pipeline:
        library = lib.library(pipeline.account)
        await _cycle(pipeline, library, daemon)
        content_ids = await _content_ids(pipeline)

        async def decisions() -> int:
            """Classification decisions recorded for the library."""
            async with ledger_engine.connect() as connection:
                return int(
                    await connection.scalar(
                        select(func.count()).where(
                            audit_entries.c.content_object_id.in_(content_ids),
                            audit_entries.c.entry_type.in_(
                                [
                                    AuditEntryType.CLASSIFICATION_DECIDED.value,
                                    AuditEntryType.CLASSIFICATION_REJECTED.value,
                                ]
                            ),
                        )
                    )
                    or 0
                )

        before, calls = await decisions(), len(remote.calls)
        layout = SEED_TEMPLATES.model_dump(mode="json")
        layout["version"] = f"e2e-{pipeline.account}"
        layout["templates"][0]["path"] = "Pictures/{captured_at:%Y}/{original_name}"
        await activate(ConfigurationKind.TEMPLATES, layout)
        await _reconcile(pipeline)

        photo = await _placed(
            ledger_engine, await _content_of(pipeline, lib.GPS_PHOTO), destination
        )
        invoice = await _placed(
            ledger_engine, await _content_of(pipeline, lib.INVOICE), destination
        )
        assert photo is not None and invoice is not None
        assert (photo.state, photo.locator) == (
            PlacementState.PRESENT,
            f"/{destination}/Pictures/2024/IMG_0001.JPG",
        )
        assert photo.template_version == layout["version"]
        assert await _held_sha256(daemon, "destination", photo.locator) == (
            hashlib.sha256(library[lib.GPS_PHOTO].content).hexdigest()
        )
        assert invoice.locator == f"/{destination}/Documents/Finance/electric.pdf"
        assert await decisions() == before
        assert len(remote.calls) == calls


async def _dry_run(http: TestClient, destination: dict[str, str]) -> dict[str, object]:
    """Start a dry run through the API, let the workers run it, and export it."""
    started = http.post("/dry-runs", json={"destinations": [destination]})
    assert started.status_code == 202
    handle: WorkflowHandleAsync[int] = await DBOS.retrieve_workflow_async(
        started.json()["workflow_id"]
    )
    await handle.get_result()
    exported: dict[str, object] = http.get(f"/dry-runs/{started.json()['run_id']}/export").json()
    return exported


async def test_the_taxonomy_is_bootstrapped_from_dry_runs_without_writing_anywhere(
    settings: Settings,
    ledger_engine: AsyncEngine,
    tmp_path: Path,
    daemon: tuple[RcloneClient, str],
    activate: Activate,
) -> None:
    """13.8: a dry run surfaces Receipts twice; promoting it lets the second run propose
    both minutes; neither run writes to any destination."""
    await activate(ConfigurationKind.POLICY, PlacementPolicy().model_dump(mode="json"))
    remote = LibraryRemote()
    run_ids: list[str] = []
    async with _organizer(
        settings, ledger_engine, tmp_path, daemon, ScriptedAccess(), remote
    ) as pipeline:
        library = lib.library(pipeline.account)
        await lib.load(library, pipeline.pcloud, daemon)
        await _scan(pipeline)
        minutes = {await _content_of(pipeline, path): path for path in (lib.PICNIC, lib.CLUB)}
        root = f"/dry-{pipeline.account}"
        target = {
            "name": f"dry-{pipeline.account}",
            "provider": "pcloud",
            "account": DESTINATION_ACCOUNT,
            "root_path": root,
        }
        with TestClient(create_app(settings)) as http:
            try:
                first = await _dry_run(http, target)
                run_ids.append(str(first["run"]["run_id"]))  # type: ignore[index]
                items = [i for i in first["items"] if i["content_object_id"] in map(str, minutes)]  # type: ignore[attr-defined]
                refused = [v for i in items for v in i["rejected_values"]]
                assert refused == [lib.RECEIPTS.model_dump(mode="json")] * 2

                [entry, *_] = [
                    e
                    for e in http.get("/review").json()
                    if e["content_object_id"] in map(str, minutes)
                ]
                promoted = http.post(f"/review/{entry['id']}/taxonomy", json={"values": []})
                assert promoted.status_code == 200
                version = promoted.json()["taxonomy_version"]
                handle: WorkflowHandleAsync[int] = await DBOS.retrieve_workflow_async(
                    reclassification_workflow_id(version)
                )
                assert await handle.get_result() == 2
                await pipeline.settle()

                second = await _dry_run(http, target)
                run_ids.append(str(second["run"]["run_id"]))  # type: ignore[index]
            finally:
                async with ledger_engine.begin() as connection:
                    await connection.execute(
                        delete(dry_runs).where(dry_runs.c.id.in_([uuid.UUID(r) for r in run_ids]))
                    )

        resolved = {
            i["content_object_id"]: i
            for i in second["items"]  # type: ignore[attr-defined]
            if i["content_object_id"] in map(str, minutes)
        }
        for content, path in minutes.items():
            item = resolved[str(content)]
            assert item["outcome"] == "proposed", path
            assert item["locator"] == f"{root}/{library[path].after_promotion}"
        blob = await _content_of(pipeline, lib.BLOB)
        assert [i["outcome"] for i in second["items"] if i["content_object_id"] == str(blob)] == [  # type: ignore[attr-defined]
            "unclassified"
        ]
        assert await daemon[0].list_files(f"/{daemon[1]}/destination", root.strip("/")) == []
        assert (
            await _count(
                ledger_engine, placements.c.content_object_id, await _content_ids(pipeline)
            )
            == 0
        )


async def test_a_destination_check_finds_only_the_copy_that_went_missing(
    settings: Settings,
    ledger_engine: AsyncEngine,
    tmp_path: Path,
    daemon: tuple[RcloneClient, str],
    activate: Activate,
    destination: str,
) -> None:
    """With several copies at one destination, the check marks only the deleted one."""
    policy = _policy(destination)
    await activate(ConfigurationKind.POLICY, policy)
    async with _organizer(
        settings, ledger_engine, tmp_path, daemon, ScriptedAccess(), LibraryRemote()
    ) as pipeline:
        library = lib.library(pipeline.account)
        await _cycle(pipeline, library, daemon)
        config = PlacementPolicy.model_validate(policy, strict=False).destinations[0]

        with SetWorkflowID(f"test-check-{uuid.uuid4().hex[:8]}"):
            assert await check_destination(config, "intact") == 0
        lost = await _placed(ledger_engine, await _content_of(pipeline, lib.INVOICE), destination)
        assert lost is not None
        await daemon[0].delete("/", f"{daemon[1]}/destination{lost.locator}")
        with SetWorkflowID(f"test-check-{uuid.uuid4().hex[:8]}"):
            assert await check_destination(config, "one-lost") == 1
        await pipeline.settle()

        for fixture in library.placed:
            placement = await _placed(
                ledger_engine, await _content_of(pipeline, fixture.path), destination
            )
            assert placement is not None
            assert placement.state is PlacementState.PRESENT, fixture.path
            expected = 1 if fixture.path == lib.INVOICE else 0
            assert placement.attempts == expected, fixture.path
