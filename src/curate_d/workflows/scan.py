"""Incremental scans of configured subtrees.

A scan lists its frontier one folder at a time. For each folder, the listing and the
reported digests are steps (provider calls), and recording the folder's files, confirming
the folder and extending the frontier is one ledger datasource transaction. A replayed
scan therefore neither lists a confirmed folder again nor records it twice, and a folder
is only ever confirmed together with every file in it.

A provider failure that outlasts pacing and retries stops the scan as incomplete with the
reason; the next scan of the root resumes that frontier. A folder deleted after its parent
was listed is the exception: it is dropped from the frontier and the scan carries on, since
listing it again would fail the same way on every later scan. Credentials the provider rejects
also flag the connection for re-authentication, which stops every other scan of it too.

After `scan_folders_per_run` folders a scan hands over to a continuation workflow and
returns, so no single workflow accumulates an unbounded step history.
"""

from collections.abc import Mapping, Sequence
from datetime import datetime
from enum import StrEnum
from typing import Final
from urllib.parse import quote
from uuid import UUID

from dbos import DBOS, ScheduleInput, SetEnqueueOptions, SetWorkflowID
from pydantic import AwareDatetime, Field

from curate_d.config import Settings
from curate_d.domain import (
    DomainModel,
    PendingFolder,
    Provider,
    Scan,
    SourceObject,
    StrongFingerprint,
)
from curate_d.ingest.pipeline import observed_marker
from curate_d.inventory.decisions import FileAction, FileDecision, apply_decision, decide
from curate_d.inventory.scans import ScanRepository
from curate_d.ledger import SourceObjectRepository
from curate_d.observability import get_logger
from curate_d.providers.connections import ConnectionRepository
from curate_d.providers.errors import (
    ConnectionRequiresReauthenticationError,
    CredentialsRejectedError,
    ProviderError,
    ProviderNotFoundError,
)
from curate_d.providers.pacing import retrying_transient
from curate_d.providers.pcloud.files import FolderListing
from curate_d.runtime.queues import QueueName
from curate_d.runtime.services import current
from curate_d.runtime.workflow_ids import scan_workflow_id
from curate_d.workflows.ingest import IngestRequest, enqueue_ingest

logger = get_logger(__name__)

SCHEDULE_NAME: Final = "scan-all"


class ScanOutcome(StrEnum):
    """How one scan workflow ended."""

    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    CONTINUED = "continued"
    """Handed the rest of the frontier to a continuation workflow."""


class ScanReport(DomainModel):
    """What one scan workflow did."""

    scan_id: UUID | None
    outcome: ScanOutcome
    folders_listed: int = 0
    actions: Mapping[FileAction, int] = Field(default_factory=dict)
    reason: str | None = None


class FolderResult(DomainModel):
    """What recording one folder changed."""

    actions: Mapping[FileAction, int]
    ingest: tuple[IngestRequest, ...]


def needs_digests(decisions: Sequence[FileDecision]) -> list[str]:
    """Native ids of the files whose digests must be read: the ones becoming work."""
    return [d.listed.native_id for d in decisions if d.action.creates_work]


async def record_folder(
    sources: SourceObjectRepository,
    scans: ScanRepository,
    *,
    scan: Scan,
    folder: PendingFolder,
    listing: FolderListing,
    digests: Mapping[str, tuple[StrongFingerprint, ...]],
    now: AwareDatetime,
) -> FolderResult:
    """Record a listed folder's files, confirm the folder, and extend the frontier.

    Decisions are taken again against the rows as this transaction reads them, so a row
    changed since the listing step (fingerprinting attaching content, say) is not
    overwritten with a stale copy.
    """
    recorded = await sources.find_by_native_ids(
        provider=scan.provider,
        account=scan.account,
        native_ids=[f.native_id for f in listing.files],
    )
    actions: dict[FileAction, int] = {}
    ingest: list[IngestRequest] = []
    for listed in listing.files:
        existing = recorded.get(listed.native_id)
        decision = decide(listed, existing)
        actions[decision.action] = actions.get(decision.action, 0) + 1
        updated = apply_decision(
            decision,
            existing,
            provider=scan.provider,
            account=scan.account,
            reported_digests=digests.get(listed.native_id, ()),
        )
        if updated is None:
            continue
        stored = await sources.upsert(updated)
        if decision.action.creates_work:
            ingest.append(
                IngestRequest(
                    source_object_id=stored.id,
                    provider=scan.provider,
                    observed=observed_marker(stored),
                )
            )
    await scans.mark_listed(scan.id, folder, listing.folders, now=now)
    return FolderResult(actions=actions, ingest=tuple(ingest))


# Steps: every provider call, and every read whose result the workflow branches on.


@DBOS.step()
async def _scan_now_step() -> datetime:
    """Read the clock once, so a replay records the same time."""
    return current().clock()


@DBOS.step()
async def _resolve_step(provider: Provider, account: str, root: str) -> PendingFolder:
    """Find the folder a configured root names."""
    files = await current().require_provider_access().files(provider, account)
    return await retrying_transient(
        lambda: files.resolve_folder(root),
        initial_seconds=current().settings.provider_retry_seconds,
    )


@DBOS.step()
async def _frontier_step(scan_id: UUID, limit: int) -> list[PendingFolder]:
    """The next folders to list."""
    async with current().datasource.engine.connect() as connection:
        return await ScanRepository(connection).frontier(scan_id, limit=limit)


@DBOS.step()
async def _list_step(
    provider: Provider, account: str, folder: PendingFolder
) -> FolderListing | None:
    """List one folder; None when it no longer exists."""
    files = await current().require_provider_access().files(provider, account)
    try:
        return await retrying_transient(
            lambda: files.list_folder(folder),
            initial_seconds=current().settings.provider_retry_seconds,
        )
    except ProviderNotFoundError:
        return None


@DBOS.step()
async def _recorded_step(
    provider: Provider, account: str, native_ids: list[str]
) -> dict[str, SourceObject]:
    """What the ledger already holds for a folder's files."""
    async with current().datasource.engine.connect() as connection:
        return await SourceObjectRepository(connection).find_by_native_ids(
            provider=provider, account=account, native_ids=native_ids
        )


@DBOS.step()
async def _digests_step(
    provider: Provider, account: str, native_ids: list[str]
) -> dict[str, tuple[StrongFingerprint, ...]]:
    """The digests the provider reports for the files becoming work."""
    files = await current().require_provider_access().files(provider, account)

    async def digests_of(native_id: str) -> tuple[StrongFingerprint, ...]:
        """One file's digests, through transient failures."""
        return await retrying_transient(
            lambda: files.reported_digests(native_id),
            initial_seconds=current().settings.provider_retry_seconds,
        )

    return {native_id: await digests_of(native_id) for native_id in native_ids}


# Transactions: every ledger write.


async def _open_tx(
    provider: Provider, account: str, root: PendingFolder, now: datetime
) -> tuple[Scan, bool]:
    """Resume the root's open scan or start one."""
    session = current().datasource.sql_session()
    return await ScanRepository(await session.connection()).open(provider, account, root, now=now)


async def _record_tx(
    scan: Scan,
    folder: PendingFolder,
    listing: FolderListing,
    digests: dict[str, tuple[StrongFingerprint, ...]],
    now: datetime,
) -> FolderResult:
    """Record one folder."""
    connection = await current().datasource.sql_session().connection()
    return await record_folder(
        SourceObjectRepository(connection),
        ScanRepository(connection),
        scan=scan,
        folder=folder,
        listing=listing,
        digests=digests,
        now=now,
    )


async def _retire_tx(scan_id: UUID, folder: PendingFolder) -> None:
    """Drop a folder that no longer exists from the frontier."""
    connection = await current().datasource.sql_session().connection()
    await ScanRepository(connection).retire(scan_id, folder)


async def _stop_tx(scan_id: UUID, error: ProviderError) -> None:
    """Mark the scan incomplete; flag the connection if the provider refused it."""
    services = current()
    connection = await services.datasource.sql_session().connection()
    await ScanRepository(connection).mark_incomplete(scan_id, str(error))
    if isinstance(error, CredentialsRejectedError):
        await ConnectionRepository(
            connection, services.require_cipher()
        ).mark_requires_reauthentication(error.provider, error.account, error.reason)


async def _complete_tx(scan_id: UUID, now: datetime) -> None:
    """Close the scan."""
    connection = await current().datasource.sql_session().connection()
    await ScanRepository(connection).complete(scan_id, now=now)


@DBOS.workflow()
async def scan_root(
    provider_value: str, account: str, root: str, cycle: str, part: int = 0
) -> ScanReport:
    """Enumerate a configured subtree, resuming its open scan if there is one."""
    provider = Provider(provider_value)
    services = current()
    datasource = services.datasource
    try:
        root_folder = await _resolve_step(provider, account, root)
    except ProviderError as error:
        # Nothing was enumerated, so there is no frontier to keep; the next run retries.
        logger.warning("scan.root_unresolved", provider=provider_value, account=account, root=root)
        return ScanReport(scan_id=None, outcome=ScanOutcome.INCOMPLETE, reason=str(error))

    scan, resumed = await datasource.run_tx_step_async(
        {"name": "open_scan"}, _open_tx, provider, account, root_folder, await _scan_now_step()
    )
    logger.info("scan.started", scan_id=str(scan.id), resumed=resumed, part=part)
    per_run = services.settings.scan_folders_per_run
    actions: dict[FileAction, int] = {}
    listed = 0

    def report(outcome: ScanOutcome, reason: str | None = None) -> ScanReport:
        """This run's report."""
        return ScanReport(
            scan_id=scan.id,
            outcome=outcome,
            folders_listed=listed,
            actions=actions,
            reason=reason,
        )

    while True:
        frontier = await _frontier_step(scan.id, per_run)
        if not frontier:
            await datasource.run_tx_step_async(
                {"name": "complete_scan"}, _complete_tx, scan.id, await _scan_now_step()
            )
            return report(ScanOutcome.COMPLETE)
        for folder in frontier:
            if listed >= per_run:
                await enqueue_scan(provider, account, root, cycle, part + 1)
                return report(ScanOutcome.CONTINUED)
            try:
                listing = await _list_step(provider, account, folder)
                if listing is None:
                    await datasource.run_tx_step_async(
                        {"name": "retire_folder"}, _retire_tx, scan.id, folder
                    )
                    logger.warning("scan.folder_gone", scan_id=str(scan.id), path=folder.path)
                    continue
                recorded = await _recorded_step(
                    provider, account, [f.native_id for f in listing.files]
                )
                wanted = needs_digests(
                    [decide(f, recorded.get(f.native_id)) for f in listing.files]
                )
                digests = await _digests_step(provider, account, wanted) if wanted else {}
            except (ProviderError, ConnectionRequiresReauthenticationError) as error:
                await datasource.run_tx_step_async({"name": "stop_scan"}, _stop_tx, scan.id, error)
                logger.warning("scan.incomplete", scan_id=str(scan.id), reason=str(error))
                return report(ScanOutcome.INCOMPLETE, str(error))
            result = await datasource.run_tx_step_async(
                {"name": "record_folder"},
                _record_tx,
                scan,
                folder,
                listing,
                digests,
                await _scan_now_step(),
            )
            for action, count in result.actions.items():
                actions[action] = actions.get(action, 0) + count
            await enqueue_ingest(result.ingest)
            listed += 1


async def enqueue_scan(
    provider: Provider, account: str, root: str, cycle: str, part: int = 0
) -> None:
    """Enqueue one scan run of a root, on the provider's partition of the scan queue."""
    workflow_id = scan_workflow_id(
        provider.value, account, quote(root, safe="/"), f"{cycle}+{part}"
    )
    with SetWorkflowID(workflow_id), SetEnqueueOptions(queue_partition_key=provider.value):
        await DBOS.enqueue_workflow_async(
            QueueName.SCAN.value, scan_root, provider.value, account, root, cycle, part
        )


@DBOS.workflow()
async def scan_all(scheduled_time: datetime, context: object) -> None:
    """Start a scan of every configured root; each resumes its own open scan if any."""
    cycle = scheduled_time.strftime("%Y%m%dT%H%M%SZ")
    for configured in current().settings.scan_roots:
        await enqueue_scan(configured.provider, configured.account, configured.root, cycle)


def schedule(settings: Settings) -> ScheduleInput:
    """The persisted schedule that scans every configured root."""
    return {
        "schedule_name": SCHEDULE_NAME,
        "workflow_fn": scan_all,
        "schedule": settings.scan_schedule,
        "context": None,
        "queue_name": QueueName.MAINTENANCE.value,
    }
