"""Placement under DBOS: copy through the rclone daemon, verify, register.

Content arrives through the real scan and ingest workflows from the stub pCloud. The same
bytes are put in a directory of the local stack's rclone daemon standing in for that
account, and another directory stands in for the destination account. The daemon's local
backend reports every hash, so an honest copy verifies on the common-hash rung.
"""

import asyncio
import contextlib
import hashlib
from collections.abc import AsyncGenerator, AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Final
from uuid import uuid4

import pytest
from dbos import DBOS, SetWorkflowID, WorkflowHandleAsync
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from curate_d.config import Settings
from curate_d.domain import (
    AuditEntry,
    AuditEntryType,
    ContentObject,
    DestinationConfig,
    HashAlgorithm,
    Placement,
    PlacementState,
    Provider,
    SourceObject,
    SourceObjectState,
    VerificationLevel,
)
from curate_d.ledger import PlacementRepository
from curate_d.ledger.mapping import to_audit_entry
from curate_d.ledger.tables import audit_entries
from curate_d.placement.pipeline import PlacementStore, conclude, defer, start
from curate_d.placement.rclone import ObjectInfo, RcloneClient
from curate_d.placement.verification import Assessment, SideEvidence, Verdict
from curate_d.runtime.queues import QueueName
from curate_d.workflows.placement import (
    SUPERSEDED,
    PlacementRequest,
    PlaceOutcome,
    enqueue_placement,
    placement_workflow_for,
    verification_workflow_for,
    verify_placement,
)
from curate_d.workflows.scan import scan_root
from tests.integration.pipeline_harness import Pipeline
from tests.integration.transfer_harness import (
    DESTINATION_ACCOUNT,
    ScriptedAccess,
    WorkspaceRemotes,
    rclone_workspace,
    transfer_pipeline,
)
from tests.stubs import media

pytestmark = pytest.mark.integration

_DESTINATION_ACCOUNT: Final = DESTINATION_ACCOUNT
_SOURCE_PATH: Final = "/Photos/IMG_0001.JPG"
_LOCATOR: Final = "/Photos/2026/IMG_0001.JPG"
_ALL_BUT_VERIFY: Final = tuple(q.value for q in QueueName if q is not QueueName.VERIFY)
_LOCATION: Final = ("destination_provider", "destination_account", "destination_root")
_CORRUPT: Final = 'corrupted on transfer: sha1 hashes differ src(a:) "aa11" vs dst(b:) "bb22"'


@pytest.fixture
async def daemon() -> AsyncIterator[tuple[RcloneClient, str]]:
    """A client for the local daemon and a private directory on it, removed afterwards."""
    async with rclone_workspace() as workspace:
        yield workspace


@contextlib.asynccontextmanager
async def _pipeline(
    settings: Settings,
    engine: AsyncEngine,
    scratch: Path,
    workspace: str,
    access: ScriptedAccess,
    *,
    listen: tuple[str, ...] | None = None,
    directories: dict[str, str] | None = None,
) -> AsyncGenerator[Pipeline, None]:
    """A pipeline that transfers through `access` into `workspace`."""
    remotes = WorkspaceRemotes(workspace, directories=directories)
    async with transfer_pipeline(
        settings, engine, scratch, remotes, access, listen=listen
    ) as pipeline:
        yield pipeline


async def _ingested(
    pipeline: Pipeline, daemon: tuple[RcloneClient, str], content: bytes, *, at_source: bytes
) -> tuple[SourceObject, ContentObject]:
    """Put `content` on the stub account and `at_source` in the daemon; scan and ingest."""
    rclone, workspace = daemon
    pipeline.pcloud.file(_SOURCE_PATH, content)
    directory, name = _SOURCE_PATH.rsplit("/", 1)
    await rclone.upload("/", f"{workspace}/source{directory}", name, at_source)
    with SetWorkflowID(f"test-scan-{pipeline.account}-{uuid4().hex[:8]}"):
        await scan_root(Provider.PCLOUD.value, pipeline.account, "/", uuid4().hex[:8])
    await pipeline.settle()
    [source] = await pipeline.sources()
    [stored] = await pipeline.contents()
    assert source.state is SourceObjectState.FINGERPRINTED
    return source, stored


def _request(
    content: ContentObject,
    destination: str,
    *,
    attempt: int = 0,
    policy_version: int | None = None,
    locator: str = _LOCATOR,
) -> PlacementRequest:
    """Place `content` at the test's destination."""
    return PlacementRequest(
        content_object_id=content.id,
        fingerprint=content.fingerprint,
        destination=DestinationConfig(
            name=destination,
            provider=Provider.PCLOUD,
            account=_DESTINATION_ACCOUNT,
            # A root of its own: the name alone no longer separates one test's placements
            # from another's, and the copy goes to the locator whatever the root.
            root_path=f"/{destination}",
        ),
        locator=locator,
        template_version="seed-1",
        size_bytes=content.size_bytes,
        attempt=attempt,
        originals={"Photos": "photos"},
        policy_version=policy_version,
    )


def _destination() -> str:
    """A destination name and root no other run shares, so workflow ids never collide."""
    return f"test-{uuid4().hex[:10]}"


async def _place(pipeline: Pipeline, request: PlacementRequest) -> PlaceOutcome:
    """Enqueue a placement, let everything settle, and return how it ended."""
    await enqueue_placement(request)
    await pipeline.settle()
    handle: WorkflowHandleAsync[str] = await DBOS.retrieve_workflow_async(
        placement_workflow_for(request)
    )
    return PlaceOutcome(await handle.get_result())


async def _placement(engine: AsyncEngine, content: ContentObject) -> Placement:
    """The one placement recorded for `content`."""
    async with engine.connect() as connection:
        [placement] = await PlacementRepository(connection).find_for_content_object(content.id)
    return placement


async def _audit(engine: AsyncEngine, placement: Placement) -> list[AuditEntry]:
    """Placement and verification audit entries for `placement`, oldest first.

    By placement rather than content: the same bytes from an earlier run are the same
    content object, and the audit trail keeps that run's entries.
    """
    async with engine.connect() as connection:
        rows = await connection.execute(
            select(audit_entries)
            .where(
                audit_entries.c.placement_id == placement.id,
                audit_entries.c.decided_by == "placement",
            )
            .order_by(audit_entries.c.id)
        )
        return [to_audit_entry(row) for row in rows.mappings()]


async def _source_state(
    pipeline: Pipeline, daemon: tuple[RcloneClient, str]
) -> tuple[ObjectInfo | None, bytes]:
    """The source as the daemon and the stub account each hold it."""
    rclone, workspace = daemon
    [stub] = [f for f in pipeline.pcloud.files.values() if f.name == _SOURCE_PATH.rsplit("/")[-1]]
    return await rclone.stat("/", f"{workspace}/source{_SOURCE_PATH}"), stub.content


async def _destination_sha256(daemon: tuple[RcloneClient, str]) -> str | None:
    """SHA-256 of what is at the locator, read back; None when nothing is."""
    rclone, workspace = daemon
    if await rclone.stat("/", f"{workspace}/destination{_LOCATOR}") is None:
        return None
    computed = await rclone.hashsum_file(
        "/", f"{workspace}/destination{_LOCATOR}", HashAlgorithm.SHA256, download=True
    )
    return computed.digest


async def test_a_placement_is_copied_verified_and_only_then_registered(
    settings: Settings,
    ledger_engine: AsyncEngine,
    tmp_path: Path,
    daemon: tuple[RcloneClient, str],
) -> None:
    """Evidence with its algorithms is recorded; the placement is present at the strong rung."""
    access = ScriptedAccess()
    content = media.photo(seed=int(uuid4().int % 97))
    async with _pipeline(settings, ledger_engine, tmp_path, daemon[1], access) as pipeline:
        source, stored = await _ingested(pipeline, daemon, content, at_source=content)
        before = await _source_state(pipeline, daemon)
        request = _request(stored, _destination(), policy_version=7)

        assert await _place(pipeline, request) is PlaceOutcome.COPIED

        placement = await _placement(ledger_engine, stored)
        assert placement.state is PlacementState.PRESENT
        assert placement.verification_level is VerificationLevel.STRONG_COMMON_HASH
        assert placement.observed_source_digest == placement.observed_destination_digest
        async with ledger_engine.connect() as connection:
            evidence = await PlacementStore(connection).evidence(placement.id)
        assert evidence is not None
        sha256 = hashlib.sha256(content).hexdigest()
        for side in (evidence.source, evidence.destination):
            assert side.size_bytes == len(content)
            assert side.reported_as(HashAlgorithm.SHA256).digest == sha256  # type: ignore[union-attr]
            assert {HashAlgorithm.SHA1, HashAlgorithm.MD5} <= {h.algorithm for h in side.reported}
        assert await _destination_sha256(daemon) == sha256

        audit = await _audit(ledger_engine, placement)
        assert [e.entry_type for e in audit] == [
            AuditEntryType.PLACEMENT_STARTED,
            AuditEntryType.VERIFICATION_SUCCEEDED,
            AuditEntryType.PLACEMENT_REGISTERED,
        ]
        started, verified, _ = audit
        assert started.details["source_path"] == _SOURCE_PATH
        assert started.details["template_version"] == "seed-1"
        assert started.details["original_Photos"] == "photos"
        assert {key: started.details[key] for key in _LOCATION} == {
            "destination_provider": "pcloud",
            "destination_account": _DESTINATION_ACCOUNT,
            "destination_root": request.destination.root_path,
        }
        assert started.details["destination_id"] == str(request.destination.id)
        assert (started.details["policy_version"], started.details["attempt"]) == (7, 0)
        assert source.version_marker is not None
        assert started.details["source_version_marker"] == source.version_marker
        assert verified.details["source_version_marker"] == source.version_marker
        assert verified.details["attempt"] == 0
        assert verified.details["compared"] == "sha256"
        assert verified.details["source_value"] == verified.details["destination_value"] == sha256
        assert all(e.placement_id == placement.id for e in audit)
        assert await _source_state(pipeline, daemon) == before


async def test_running_a_registered_placement_again_copies_nothing(
    settings: Settings,
    ledger_engine: AsyncEngine,
    tmp_path: Path,
    daemon: tuple[RcloneClient, str],
) -> None:
    """A second request for a present placement leaves the record and the copy alone."""
    access = ScriptedAccess()
    content = media.photo(seed=int(uuid4().int % 97))
    async with _pipeline(settings, ledger_engine, tmp_path, daemon[1], access) as pipeline:
        _, stored = await _ingested(pipeline, daemon, content, at_source=content)
        destination = _destination()
        await _place(pipeline, _request(stored, destination))
        registered = await _placement(ledger_engine, stored)

        again = _request(stored, destination, attempt=1)
        assert await _place(pipeline, again) is PlaceOutcome.ALREADY_PRESENT

        assert access.copies == 1
        assert await _placement(ledger_engine, stored) == registered


async def test_a_source_changed_since_fingerprinting_is_sent_back_not_registered(
    settings: Settings,
    ledger_engine: AsyncEngine,
    tmp_path: Path,
    daemon: tuple[RcloneClient, str],
) -> None:
    """Different bytes at copy time: both digests recorded, the source re-fingerprinted."""
    access = ScriptedAccess()
    fingerprinted = media.photo(seed=int(uuid4().int % 97))
    edited = fingerprinted + b"edited"
    async with _pipeline(settings, ledger_engine, tmp_path, daemon[1], access) as pipeline:
        fingerprinted_source, stored = await _ingested(
            pipeline, daemon, fingerprinted, at_source=edited
        )
        before = await _source_state(pipeline, daemon)

        assert await _place(pipeline, _request(stored, _destination())) is PlaceOutcome.COPIED

        placement = await _placement(ledger_engine, stored)
        assert placement.state is PlacementState.FAILED
        assert placement.attempts == 1
        audit = await _audit(ledger_engine, placement)
        compared = str(audit[-1].details["compared"])
        assert placement.observed_source_digest == hashlib.new(compared, edited).hexdigest()
        assert placement.observed_destination_digest == (
            hashlib.new(compared, fingerprinted).hexdigest()
        )
        refingerprints = [
            status
            for workflow_id, status in await pipeline.workflows(
                f"ingest:{fingerprinted_source.id}:"
            )
            if workflow_id.endswith("@placement-0")
        ]
        assert refingerprints == ["SUCCESS"]
        assert [e.entry_type for e in audit] == [
            AuditEntryType.PLACEMENT_STARTED,
            AuditEntryType.VERIFICATION_FAILED,
            AuditEntryType.SOURCE_DISCREPANCY_DETECTED,
        ]
        assert await _source_state(pipeline, daemon) == before


async def test_a_recorded_mismatch_leaves_the_copy_untrusted_with_both_values(
    settings: Settings,
    ledger_engine: AsyncEngine,
    tmp_path: Path,
    daemon: tuple[RcloneClient, str],
) -> None:
    """Verification assesses only what was recorded, and a disagreement is never registered."""
    access = ScriptedAccess()
    content = media.photo(seed=int(uuid4().int % 97))
    async with _pipeline(
        settings, ledger_engine, tmp_path, daemon[1], access, listen=_ALL_BUT_VERIFY
    ) as pipeline:
        _, stored = await _ingested(pipeline, daemon, content, at_source=content)
        before = await _source_state(pipeline, daemon)
        assert await _place(pipeline, _request(stored, _destination())) is PlaceOutcome.COPIED
        placement = await _placement(ledger_engine, stored)
        assert placement.state is PlacementState.VERIFYING
        async with ledger_engine.begin() as connection:
            store = PlacementStore(connection)
            evidence = await store.evidence(placement.id)
            assert evidence is not None
            tampered = tuple(
                h.model_copy(update={"digest": "0" * len(h.digest)})
                for h in evidence.destination.reported
            )
            await store.set_evidence(
                placement.id,
                evidence.model_copy(
                    update={"destination": SideEvidence(size_bytes=len(content), reported=tampered)}
                ),
            )

        with SetWorkflowID(f"test-verify-{uuid4().hex}"):
            assert await verify_placement(placement.id) == "mismatch"

        untrusted = await _placement(ledger_engine, stored)
        assert untrusted.state is PlacementState.UNTRUSTED
        assert untrusted.verified_at is None
        assert untrusted.observed_source_digest == hashlib.sha256(content).hexdigest()
        assert untrusted.observed_destination_digest == "0" * 64
        failed = (await _audit(ledger_engine, untrusted))[-1]
        assert failed.entry_type is AuditEntryType.VERIFICATION_FAILED
        assert failed.details["verdict"] == "mismatch"
        assert await _source_state(pipeline, daemon) == before


async def test_a_copy_rclone_rejects_as_corrupt_is_a_mismatch_from_its_report(
    settings: Settings,
    ledger_engine: AsyncEngine,
    tmp_path: Path,
    daemon: tuple[RcloneClient, str],
) -> None:
    """rclone's post-copy check deleted the copy; the two values it named are recorded."""
    access = ScriptedAccess()
    access.corrupt = _CORRUPT
    content = media.photo(seed=int(uuid4().int % 97))
    async with _pipeline(settings, ledger_engine, tmp_path, daemon[1], access) as pipeline:
        _, stored = await _ingested(pipeline, daemon, content, at_source=content)

        assert await _place(pipeline, _request(stored, _destination())) is PlaceOutcome.COPIED

        placement = await _placement(ledger_engine, stored)
        assert placement.state is PlacementState.UNTRUSTED
        assert (placement.observed_source_digest, placement.observed_destination_digest) == (
            "aa11",
            "bb22",
        )


async def test_an_interrupted_placement_is_not_registered_and_resumes_without_cleanup(
    settings: Settings,
    ledger_engine: AsyncEngine,
    tmp_path: Path,
    daemon: tuple[RcloneClient, str],
) -> None:
    """Stopped mid-copy with a partial file at the locator; recovery supersedes it."""
    access = ScriptedAccess()
    content = media.photo(seed=int(uuid4().int % 97))
    rclone, workspace = daemon
    async with _pipeline(settings, ledger_engine, tmp_path, workspace, access) as pipeline:
        _, stored = await _ingested(pipeline, daemon, content, at_source=content)
        request = _request(stored, _destination())
        workflow_id = placement_workflow_for(request)
        crash = access.crash = asyncio.Event()
        await enqueue_placement(request)
        await asyncio.wait_for(access.paused.wait(), timeout=30)
        await DBOS.cancel_workflow_async(workflow_id)
        access.crash = None
        crash.set()
        directory, name = _LOCATOR.rsplit("/", 1)
        await rclone.upload("/", f"{workspace}/destination{directory}", name, content[:100])

        interrupted = await _placement(ledger_engine, stored)
        assert interrupted.state is PlacementState.COPYING
        assert interrupted.verified_at is None

        await DBOS.resume_workflow_async(workflow_id)
        await pipeline.settle()

        placement = await _placement(ledger_engine, stored)
        assert placement.state is PlacementState.PRESENT
        assert await _destination_sha256(daemon) == hashlib.sha256(content).hexdigest()


async def test_running_out_of_scratch_waits_and_retries_rather_than_abandoning(
    settings: Settings,
    ledger_engine: AsyncEngine,
    tmp_path: Path,
    daemon: tuple[RcloneClient, str],
) -> None:
    """Two resource errors are waited out; the third copy goes through."""
    access = ScriptedAccess()
    access.out_of_space = 2
    content = media.photo(seed=int(uuid4().int % 97))
    async with _pipeline(settings, ledger_engine, tmp_path, daemon[1], access) as pipeline:
        _, stored = await _ingested(pipeline, daemon, content, at_source=content)

        assert await _place(pipeline, _request(stored, _destination())) is PlaceOutcome.COPIED

        assert access.copies == 3
        placement = await _placement(ledger_engine, stored)
        assert (placement.state, placement.failure_reason) == (PlacementState.PRESENT, None)


async def test_scratch_that_stays_exhausted_fails_explicitly_and_a_later_attempt_completes(
    settings: Settings,
    ledger_engine: AsyncEngine,
    tmp_path: Path,
    daemon: tuple[RcloneClient, str],
) -> None:
    """The resource error is recorded; the next attempt supersedes the partial copy."""
    access = ScriptedAccess()
    access.out_of_space = 1_000
    content = media.photo(seed=int(uuid4().int % 97))
    rclone, workspace = daemon
    async with _pipeline(settings, ledger_engine, tmp_path, workspace, access) as pipeline:
        _, stored = await _ingested(pipeline, daemon, content, at_source=content)
        destination = _destination()
        before = await _source_state(pipeline, daemon)

        assert await _place(pipeline, _request(stored, destination)) is PlaceOutcome.FAILED

        failed = await _placement(ledger_engine, stored)
        assert failed.state is PlacementState.FAILED
        assert failed.attempts == 1
        assert failed.failure_reason is not None
        assert "scratch exhausted" in failed.failure_reason
        assert "no space left on device" in failed.failure_reason
        assert (await _audit(ledger_engine, failed))[-1].entry_type is (
            AuditEntryType.PLACEMENT_FAILED
        )
        assert await _source_state(pipeline, daemon) == before

        directory, name = _LOCATOR.rsplit("/", 1)
        await rclone.upload("/", f"{workspace}/destination{directory}", name, b"partial")
        access.out_of_space = 0
        retry = _request(stored, destination, attempt=failed.attempts)
        assert await _place(pipeline, retry) is PlaceOutcome.COPIED

        placement = await _placement(ledger_engine, stored)
        assert (placement.state, placement.attempts) == (PlacementState.PRESENT, 1)
        assert await _destination_sha256(daemon) == hashlib.sha256(content).hexdigest()
        listing = await rclone.call(
            "operations/list", {"fs": "/", "remote": f"{workspace}/destination{directory}"}
        )
        entries = listing["list"]
        assert isinstance(entries, list)
        assert [entry["Name"] for entry in entries] == [name]


async def test_a_rewound_failed_attempt_is_verified_under_the_attempt_it_records(
    settings: Settings,
    ledger_engine: AsyncEngine,
    tmp_path: Path,
    daemon: tuple[RcloneClient, str],
) -> None:
    """The re-copy gets a verification of its own and is registered, not left verifying."""
    access = ScriptedAccess()
    access.corrupt = _CORRUPT
    content = media.photo(seed=int(uuid4().int % 97))
    async with _pipeline(settings, ledger_engine, tmp_path, daemon[1], access) as pipeline:
        _, stored = await _ingested(pipeline, daemon, content, at_source=content)
        request = _request(stored, _destination())
        assert await _place(pipeline, request) is PlaceOutcome.COPIED
        untrusted = await _placement(ledger_engine, stored)
        assert (untrusted.state, untrusted.attempts) == (PlacementState.UNTRUSTED, 1)

        access.corrupt = None
        rewound = await DBOS.rewind_workflow_async(placement_workflow_for(request), start_step=1)
        assert PlaceOutcome(await rewound.get_result()) is PlaceOutcome.COPIED
        await pipeline.settle()

        placement = await _placement(ledger_engine, stored)
        assert (placement.state, placement.attempts) == (PlacementState.PRESENT, 1)
        assert access.copies == 2
        assert await pipeline.workflows(f"verify:{placement.id}:") == [
            (verification_workflow_for(placement.id, 0), "SUCCESS"),
            (verification_workflow_for(placement.id, 1), "SUCCESS"),
        ]
        audit = await _audit(ledger_engine, placement)
        assert [(e.entry_type, e.details.get("attempt")) for e in audit] == [
            (AuditEntryType.PLACEMENT_STARTED, 0),
            (AuditEntryType.VERIFICATION_FAILED, 0),
            (AuditEntryType.PLACEMENT_STARTED, 1),
            (AuditEntryType.VERIFICATION_SUCCEEDED, 1),
            (AuditEntryType.PLACEMENT_REGISTERED, None),
        ]


async def test_an_old_verification_run_again_after_a_newer_attempt_started_changes_nothing(
    settings: Settings,
    ledger_engine: AsyncEngine,
    tmp_path: Path,
    daemon: tuple[RcloneClient, str],
) -> None:
    """The new copy discarded the old evidence; nothing is concluded, nothing audited."""
    access = ScriptedAccess()
    access.corrupt = _CORRUPT
    content = media.photo(seed=int(uuid4().int % 97))
    async with _pipeline(settings, ledger_engine, tmp_path, daemon[1], access) as pipeline:
        source, stored = await _ingested(pipeline, daemon, content, at_source=content)
        request = _request(stored, _destination())
        assert await _place(pipeline, request) is PlaceOutcome.COPIED
        untrusted = await _placement(ledger_engine, stored)
        async with ledger_engine.begin() as connection:
            old_evidence = await PlacementStore(connection).evidence(untrusted.id)
            assert old_evidence is not None
            _, starting = await start(
                connection,
                content_object_id=stored.id,
                fingerprint=stored.fingerprint,
                destination=request.destination,
                locator=_LOCATOR,
                template_version="seed-1",
                source=source,
                transformations={},
                attempt=untrusted.attempts,
                now=datetime.now(UTC),
            )
            assert starting
        copying = await _placement(ledger_engine, stored)
        assert (copying.state, copying.attempts) == (PlacementState.COPYING, 1)
        trail = await _audit(ledger_engine, copying)

        rewound = await DBOS.rewind_workflow_async(
            verification_workflow_for(copying.id, 0), start_step=1
        )
        assert await rewound.get_result() == SUPERSEDED

        assert await _placement(ledger_engine, stored) == copying
        assert await _audit(ledger_engine, copying) == trail
        async with ledger_engine.begin() as connection:
            store = PlacementStore(connection)
            assert await store.evidence(copying.id) is None
            assert not await store.set_evidence(copying.id, old_evidence, attempt=0)
            verified = Assessment(
                verdict=Verdict.VERIFIED, level=VerificationLevel.STRONG_COMMON_HASH
            )
            assert (
                await conclude(connection, copying.id, verified, attempt=0, now=datetime.now(UTC))
                is None
            )
            assert not await defer(connection, copying.id, "waiting late", attempt=0)
        assert await _placement(ledger_engine, stored) == copying
        assert await _audit(ledger_engine, copying) == trail


async def test_rewinding_a_registered_placement_copies_nothing(
    settings: Settings,
    ledger_engine: AsyncEngine,
    tmp_path: Path,
    daemon: tuple[RcloneClient, str],
) -> None:
    """The re-run finds the copy present at its locator and leaves record and trail alone."""
    access = ScriptedAccess()
    content = media.photo(seed=int(uuid4().int % 97))
    async with _pipeline(settings, ledger_engine, tmp_path, daemon[1], access) as pipeline:
        _, stored = await _ingested(pipeline, daemon, content, at_source=content)
        request = _request(stored, _destination())
        assert await _place(pipeline, request) is PlaceOutcome.COPIED
        registered = await _placement(ledger_engine, stored)
        trail = await _audit(ledger_engine, registered)

        rewound = await DBOS.rewind_workflow_async(placement_workflow_for(request), start_step=1)
        assert PlaceOutcome(await rewound.get_result()) is PlaceOutcome.ALREADY_PRESENT
        await pipeline.settle()

        assert access.copies == 1
        assert await _placement(ledger_engine, stored) == registered
        assert await _audit(ledger_engine, registered) == trail


async def _copy_step(workflow_id: str) -> int:
    """Return the step at which `workflow_id` copied."""
    steps = await DBOS.list_workflow_steps_async(workflow_id, load_output=False)
    [step] = [s["function_id"] for s in steps if s["function_name"].endswith("_copy_step")]
    return step


async def test_an_old_copy_that_completes_after_a_newer_attempt_started_records_nothing(
    settings: Settings,
    ledger_engine: AsyncEngine,
    tmp_path: Path,
    daemon: tuple[RcloneClient, str],
) -> None:
    """Rewound from its copy, the old attempt ends superseded: no evidence, no verification."""
    access = ScriptedAccess()
    access.corrupt = _CORRUPT
    content = media.photo(seed=int(uuid4().int % 97))
    async with _pipeline(settings, ledger_engine, tmp_path, daemon[1], access) as pipeline:
        source, stored = await _ingested(pipeline, daemon, content, at_source=content)
        request = _request(stored, _destination())
        assert await _place(pipeline, request) is PlaceOutcome.COPIED
        untrusted = await _placement(ledger_engine, stored)
        async with ledger_engine.begin() as connection:
            await start(
                connection,
                content_object_id=stored.id,
                fingerprint=stored.fingerprint,
                destination=request.destination,
                locator=_LOCATOR,
                template_version="seed-1",
                source=source,
                transformations={},
                attempt=untrusted.attempts,
                now=datetime.now(UTC),
            )
        copying = await _placement(ledger_engine, stored)
        assert (copying.state, copying.attempts) == (PlacementState.COPYING, 1)
        trail = await _audit(ledger_engine, copying)
        verifications = await pipeline.workflows(f"verify:{copying.id}:")

        access.corrupt = None
        workflow_id = placement_workflow_for(request)
        rewound = await DBOS.rewind_workflow_async(
            workflow_id, start_step=await _copy_step(workflow_id)
        )
        assert PlaceOutcome(await rewound.get_result()) is PlaceOutcome.SUPERSEDED
        await pipeline.settle()

        assert access.copies == 2
        assert await _placement(ledger_engine, stored) == copying
        assert await _audit(ledger_engine, copying) == trail
        assert await pipeline.workflows(f"verify:{copying.id}:") == verifications
        async with ledger_engine.connect() as connection:
            assert await PlacementStore(connection).evidence(copying.id) is None


async def test_an_old_copy_deferred_after_a_newer_attempt_started_stops_without_copying_again(
    settings: Settings,
    ledger_engine: AsyncEngine,
    tmp_path: Path,
    daemon: tuple[RcloneClient, str],
) -> None:
    """Rewound from its copy into a scratch wait, the old attempt ends superseded at once."""
    access = ScriptedAccess()
    access.corrupt = _CORRUPT
    content = media.photo(seed=int(uuid4().int % 97))
    async with _pipeline(settings, ledger_engine, tmp_path, daemon[1], access) as pipeline:
        source, stored = await _ingested(pipeline, daemon, content, at_source=content)
        request = _request(stored, _destination())
        assert await _place(pipeline, request) is PlaceOutcome.COPIED
        untrusted = await _placement(ledger_engine, stored)
        async with ledger_engine.begin() as connection:
            await start(
                connection,
                content_object_id=stored.id,
                fingerprint=stored.fingerprint,
                destination=request.destination,
                locator=_LOCATOR,
                template_version="seed-1",
                source=source,
                transformations={},
                attempt=untrusted.attempts,
                now=datetime.now(UTC),
            )
        copying = await _placement(ledger_engine, stored)
        assert (copying.state, copying.attempts) == (PlacementState.COPYING, 1)
        trail = await _audit(ledger_engine, copying)
        verifications = await pipeline.workflows(f"verify:{copying.id}:")

        access.corrupt = None
        access.out_of_space = 1_000
        workflow_id = placement_workflow_for(request)
        rewound = await DBOS.rewind_workflow_async(
            workflow_id, start_step=await _copy_step(workflow_id)
        )
        assert PlaceOutcome(await rewound.get_result()) is PlaceOutcome.SUPERSEDED
        await pipeline.settle()

        assert access.copies == 2
        assert await _placement(ledger_engine, stored) == copying
        assert await _audit(ledger_engine, copying) == trail
        assert await pipeline.workflows(f"verify:{copying.id}:") == verifications


async def test_an_old_copy_that_fails_after_the_placement_registered_leaves_it_registered(
    settings: Settings,
    ledger_engine: AsyncEngine,
    tmp_path: Path,
    daemon: tuple[RcloneClient, str],
) -> None:
    """Rewound from its copy, the old attempt ends superseded: no failure, no audit entry."""
    access = ScriptedAccess()
    content = media.photo(seed=int(uuid4().int % 97))
    async with _pipeline(settings, ledger_engine, tmp_path, daemon[1], access) as pipeline:
        _, stored = await _ingested(pipeline, daemon, content, at_source=content)
        request = _request(stored, _destination())
        assert await _place(pipeline, request) is PlaceOutcome.COPIED
        registered = await _placement(ledger_engine, stored)
        assert registered.state is PlacementState.PRESENT
        trail = await _audit(ledger_engine, registered)

        access.out_of_space = 1_000
        workflow_id = placement_workflow_for(request)
        rewound = await DBOS.rewind_workflow_async(
            workflow_id, start_step=await _copy_step(workflow_id)
        )
        assert PlaceOutcome(await rewound.get_result()) is PlaceOutcome.SUPERSEDED
        await pipeline.settle()

        assert access.copies > 1
        assert await _placement(ledger_engine, stored) == registered
        assert await _audit(ledger_engine, registered) == trail


async def test_an_old_attempt_rewound_over_a_copy_registered_elsewhere_is_verified_anew(
    settings: Settings,
    ledger_engine: AsyncEngine,
    tmp_path: Path,
    daemon: tuple[RcloneClient, str],
) -> None:
    """The re-copy records the next attempt rather than collapse onto a finished verification."""
    access = ScriptedAccess()
    content = media.photo(seed=int(uuid4().int % 97))
    async with _pipeline(settings, ledger_engine, tmp_path, daemon[1], access) as pipeline:
        _, stored = await _ingested(pipeline, daemon, content, at_source=content)
        destination = _destination()
        first = _request(stored, destination)
        assert await _place(pipeline, first) is PlaceOutcome.COPIED
        moved = _request(stored, destination, attempt=1, locator="/Photos/moved/IMG_0001.JPG")
        assert await _place(pipeline, moved) is PlaceOutcome.COPIED
        elsewhere = await _placement(ledger_engine, stored)
        assert (elsewhere.state, elsewhere.locator, elsewhere.attempts) == (
            PlacementState.PRESENT,
            moved.locator,
            1,
        )

        rewound = await DBOS.rewind_workflow_async(placement_workflow_for(first), start_step=1)
        assert PlaceOutcome(await rewound.get_result()) is PlaceOutcome.COPIED
        await pipeline.settle()

        placement = await _placement(ledger_engine, stored)
        assert (placement.state, placement.locator, placement.attempts) == (
            PlacementState.PRESENT,
            _LOCATOR,
            2,
        )
        assert await pipeline.workflows(f"verify:{placement.id}:") == [
            (verification_workflow_for(placement.id, attempt), "SUCCESS") for attempt in range(3)
        ]


async def test_one_name_at_a_second_account_is_a_second_placement_and_workflow(
    settings: Settings,
    ledger_engine: AsyncEngine,
    tmp_path: Path,
    daemon: tuple[RcloneClient, str],
) -> None:
    """Placed under one name at two accounts: two copies, two rows, two workflows."""
    access = ScriptedAccess()
    content = media.photo(seed=int(uuid4().int % 97))
    second_account = f"acct-{uuid4().hex[:8]}"
    async with _pipeline(
        settings,
        ledger_engine,
        tmp_path,
        daemon[1],
        access,
        directories={second_account: "second"},
    ) as pipeline:
        _, stored = await _ingested(pipeline, daemon, content, at_source=content)
        first = _request(stored, _destination())
        moved = first.model_copy(
            update={"destination": first.destination.model_copy(update={"account": second_account})}
        )

        assert await _place(pipeline, first) is PlaceOutcome.COPIED
        assert await _place(pipeline, moved) is PlaceOutcome.COPIED

        assert placement_workflow_for(first) != placement_workflow_for(moved)
        async with ledger_engine.connect() as connection:
            recorded = await PlacementRepository(connection).find_for_content_object(stored.id)
        at = {p.destination_id: p for p in recorded}
        assert first.destination.id in at
        assert moved.destination.id in at
        assert at[first.destination.id].id != at[moved.destination.id].id
        assert {at[d.id].state for d in (first.destination, moved.destination)} == {
            PlacementState.PRESENT
        }
        assert access.copies == 2
