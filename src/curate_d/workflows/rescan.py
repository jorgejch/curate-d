"""Periodic full rescans: re-reading content regardless of version markers.

Each run takes the account's files not verified within `full_rescan_interval_days`,
oldest verification first and never-verified before those, and re-reads each one. A file
whose content still matches its recorded fingerprint gets its verification time stamped;
one whose content changed behind an unchanged marker is recorded as a discrepancy, reset
to discovered and ingested again; one with nothing recorded to compare against is simply
ingested again. The stamp is what makes a pass resumable: an interrupted run leaves the
files it reached stamped, and the next run starts from the ones it did not.
"""

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final

from dbos import DBOS, PropagateOtelContext, ScheduleInput, SetEnqueueOptions, SetWorkflowID
from pydantic import AwareDatetime

from curate_d.config import Settings
from curate_d.domain import (
    AuditEntry,
    AuditEntryType,
    DomainModel,
    Provider,
    SourceObject,
    SourceObjectId,
    SourceObjectState,
    StrongFingerprint,
)
from curate_d.ingest.hashing import ContentDigests, hash_content
from curate_d.ingest.pipeline import observed_marker
from curate_d.inventory.rescan import RescanVerdict, apply_discrepancy, assess_rescan
from curate_d.ledger import AuditRepository, ContentObjectRepository, SourceObjectRepository
from curate_d.observability import get_logger
from curate_d.providers.connections import ConnectionRepository
from curate_d.providers.errors import (
    ConnectionRequiresReauthenticationError,
    CredentialsRejectedError,
    ProviderError,
    ProviderRequestError,
)
from curate_d.providers.pacing import retrying_transient
from curate_d.runtime.queues import QueueName
from curate_d.runtime.services import current
from curate_d.runtime.workflow_ids import full_rescan_workflow_id
from curate_d.workflows.ingest import IngestRequest, enqueue_ingest

logger = get_logger(__name__)

SCHEDULE_NAME: Final = "full-rescan-all"
DECIDED_BY: Final = "full_rescan"


class RescanOutcome(StrEnum):
    """How one full-rescan run ended."""

    COMPLETE = "complete"
    CONTINUED = "continued"
    INCOMPLETE = "incomplete"


class RescanReport(DomainModel):
    """What one full-rescan run did."""

    outcome: RescanOutcome
    verdicts: dict[RescanVerdict, int]
    reason: str | None = None


class StaleSource(DomainModel):
    """A file due for re-reading, with the fingerprint it is expected to still have."""

    source: SourceObject
    recorded: StrongFingerprint | None


async def record_rescan(
    sources: SourceObjectRepository,
    audit: AuditRepository,
    stale: StaleSource,
    observed: ContentDigests,
    *,
    generation: str,
    now: AwareDatetime,
) -> tuple[RescanVerdict, IngestRequest | None]:
    """Record what re-reading one file established, returning any re-ingest it needs."""
    source = stale.source
    verdict = assess_rescan(source, stale.recorded, observed)
    if verdict is RescanVerdict.CONFIRMED or source.state is SourceObjectState.EXCLUDED:
        await sources.record_verification(source.id, now)
        return verdict, None
    if verdict is RescanVerdict.DISCREPANCY:
        await audit.append(
            AuditEntry(
                entry_type=AuditEntryType.SOURCE_DISCREPANCY_DETECTED,
                occurred_at=now,
                decided_by=DECIDED_BY,
                details={
                    "provider": source.provider.value,
                    "version_marker": source.version_marker,
                    "recorded_fingerprint": str(stale.recorded),
                    "observed_fingerprint": str(observed.fingerprint),
                    "reason": "content changed behind an unchanged version marker",
                },
                source_object_id=source.id,
                content_object_id=source.content_object_id,
            )
        )
    updated = await sources.upsert(apply_discrepancy(source, observed, now=now))
    return verdict, IngestRequest(
        source_object_id=updated.id,
        provider=updated.provider,
        observed=observed_marker(updated),
        generation=generation,
    )


# Steps.


@DBOS.step()
async def _rescan_now_step() -> datetime:
    """Read the clock once, so a replay uses the same time."""
    return current().clock()


@DBOS.step()
async def _stale_step(
    provider: Provider, account: str, before: datetime, limit: int
) -> list[StaleSource]:
    """The next files due for re-reading, with their recorded fingerprints."""
    async with current().datasource.engine.connect() as connection:
        sources = await SourceObjectRepository(connection).find_stale_for_full_rescan(
            provider=provider, account=account, before=before, limit=limit
        )
        contents = ContentObjectRepository(connection)
        stale: list[StaleSource] = []
        for source in sources:
            content = (
                None
                if source.content_object_id is None
                else await contents.get(source.content_object_id)
            )
            stale.append(
                StaleSource(
                    source=source, recorded=None if content is None else content.fingerprint
                )
            )
        return stale


@DBOS.step()
async def _hash_step(provider: Provider, account: str, native_id: str) -> ContentDigests:
    """Re-read one file's content and hash it."""
    services = current()
    files = await services.require_provider_access().files(provider, account)
    return await retrying_transient(
        lambda: hash_content(files, native_id),
        initial_seconds=services.settings.provider_retry_seconds,
    )


# Transactions.


async def _record_tx(
    stale: StaleSource, observed: ContentDigests, generation: str, now: datetime
) -> tuple[RescanVerdict, IngestRequest | None]:
    """Record one file's outcome."""
    connection = await current().datasource.sql_session().connection()
    return await record_rescan(
        SourceObjectRepository(connection),
        AuditRepository(connection),
        stale,
        observed,
        generation=generation,
        now=now,
    )


async def _unreadable_tx(source_object_id: SourceObjectId, reason: str, now: datetime) -> None:
    """Mark a file the provider no longer serves as failed, stamped so the pass moves on."""
    connection = await current().datasource.sql_session().connection()
    sources = SourceObjectRepository(connection)
    await AuditRepository(connection).append(
        AuditEntry(
            entry_type=AuditEntryType.SOURCE_READ_FAILED,
            occurred_at=now,
            decided_by=DECIDED_BY,
            details={"reason": reason},
            source_object_id=source_object_id,
        )
    )
    await sources.mark_failed(source_object_id, reason)
    await sources.record_verification(source_object_id, now)


async def _reauthenticate_tx(error: CredentialsRejectedError) -> None:
    """Flag the connection whose credentials the provider refused."""
    services = current()
    connection = await services.datasource.sql_session().connection()
    await ConnectionRepository(
        connection, services.require_cipher()
    ).mark_requires_reauthentication(error.provider, error.account, error.reason)


# Workflows.


@DBOS.workflow()
async def full_rescan(provider_value: str, account: str, cycle: str, part: int = 0) -> RescanReport:
    """Re-read the account's files that are due, in batches, confirming or re-ingesting each."""
    provider = Provider(provider_value)
    services = current()
    datasource = services.datasource
    per_run = services.settings.full_rescan_files_per_run
    now = await _rescan_now_step()
    before = now - timedelta(days=services.settings.full_rescan_interval_days)
    generation = f"rescan-{cycle}"
    verdicts: dict[RescanVerdict, int] = {}

    batch = await _stale_step(provider, account, before, per_run)
    for stale in batch:
        try:
            observed = await _hash_step(provider, account, stale.source.native_id)
        except CredentialsRejectedError as rejected:
            await datasource.run_tx_step_async(
                {"name": "flag_connection"}, _reauthenticate_tx, rejected
            )
            return RescanReport(
                outcome=RescanOutcome.INCOMPLETE, verdicts=verdicts, reason=str(rejected)
            )
        except ConnectionRequiresReauthenticationError as blocked:
            return RescanReport(
                outcome=RescanOutcome.INCOMPLETE, verdicts=verdicts, reason=str(blocked)
            )
        except ProviderRequestError as gone:
            await datasource.run_tx_step_async(
                {"name": "record_unreadable"},
                _unreadable_tx,
                stale.source.id,
                str(gone),
                await _rescan_now_step(),
            )
            continue
        except ProviderError as unavailable:
            # Files already re-read keep their stamps; the next run resumes after them.
            logger.warning("rescan.incomplete", account=account, reason=str(unavailable))
            return RescanReport(
                outcome=RescanOutcome.INCOMPLETE, verdicts=verdicts, reason=str(unavailable)
            )
        verdict, ingest = await datasource.run_tx_step_async(
            {"name": "record_rescan"},
            _record_tx,
            stale,
            observed,
            generation,
            await _rescan_now_step(),
        )
        verdicts[verdict] = verdicts.get(verdict, 0) + 1
        if ingest is not None:
            await enqueue_ingest([ingest])
    if len(batch) == per_run:
        await enqueue_full_rescan(provider, account, cycle, part + 1)
        return RescanReport(outcome=RescanOutcome.CONTINUED, verdicts=verdicts)
    return RescanReport(outcome=RescanOutcome.COMPLETE, verdicts=verdicts)


async def enqueue_full_rescan(provider: Provider, account: str, cycle: str, part: int = 0) -> None:
    """Enqueue one full-rescan run of an account on the provider's scan partition."""
    # Carries this span to the dequeued child; without it the child roots its own trace.
    with (
        PropagateOtelContext(),
        SetWorkflowID(full_rescan_workflow_id(provider.value, account, f"{cycle}+{part}")),
        SetEnqueueOptions(queue_partition_key=provider.value),
    ):
        await DBOS.enqueue_workflow_async(
            QueueName.SCAN.value, full_rescan, provider.value, account, cycle, part
        )


@DBOS.workflow()
async def full_rescan_all(scheduled_time: datetime, context: object) -> None:
    """Start a full-rescan run for every account with a configured root."""
    cycle = scheduled_time.strftime("%Y%m%dT%H%M%SZ")
    accounts = sorted({(r.provider, r.account) for r in current().settings.scan_roots})
    for provider, account in accounts:
        await enqueue_full_rescan(provider, account, cycle)


def schedule(settings: Settings) -> ScheduleInput:
    """The persisted schedule of full-rescan runs, independent of incremental scans."""
    return {
        "schedule_name": SCHEDULE_NAME,
        "workflow_fn": full_rescan_all,
        "schedule": settings.full_rescan_schedule,
        "context": None,
        "queue_name": QueueName.MAINTENANCE.value,
    }
