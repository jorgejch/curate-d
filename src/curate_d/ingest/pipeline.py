"""The work each ingest step does, apart from DBOS.

The workflows in `workflows/ingest.py` place each function here in a step or a ledger
transaction; keeping them free of DBOS is what lets every branch be tested against
in-memory staging and a stub provider.

I/O profile: `stage_source` fetches a file and publishes it into staging, and rejects it
if what arrived is not what the scan listed. CPU profile: `inspect_staged` reads a
verified snapshot to sniff its media type and compute a perceptual fingerprint, and
`record_fingerprint` creates or finds the content object and links the source to it.
"""

import asyncio
from collections.abc import Awaitable, Coroutine
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from uuid import UUID

from pydantic import AwareDatetime, Field

from curate_d.content.decode import DEFAULT_MAX_PIXELS
from curate_d.content.perceptual import (
    ImageTooLargeError,
    ImageUndecodableError,
    is_image,
    perceptual_fingerprint,
)
from curate_d.content.sniff import sniff
from curate_d.domain import (
    AuditEntry,
    AuditEntryType,
    ContentObject,
    ContentObjectId,
    DomainModel,
    PerceptualFingerprint,
    SourceObject,
    SourceObjectId,
    SourceObjectState,
)
from curate_d.ingest.checks import content_fingerprint, source_change
from curate_d.ingest.fetch import fetched
from curate_d.ledger import AuditRepository, ContentObjectRepository, SourceObjectRepository
from curate_d.providers.access import ProviderFiles
from curate_d.staging.factories import StagingWriterBackend
from curate_d.staging.keys import StagingKey, derive_staging_key
from curate_d.staging.publish import publish
from curate_d.staging.snapshots import LocalScratchBudget, LocalSnapshotReader

DECIDED_BY = "ingest"


class PublishRunner(Protocol):
    """Runs staging work on the loop that owns the writer's connections."""

    def __call__[T](self, work: Coroutine[None, None, T], /) -> Awaitable[T]:
        """Run `work` and return its result."""
        ...


class StagedRef(DomainModel):
    """One staged version of a source's content, as the CPU profile is handed it."""

    key: str = Field(min_length=1)
    sha256: str = Field(min_length=64, max_length=64)
    sha1: str = Field(min_length=40, max_length=40)
    size_bytes: int = Field(ge=0)


class StageOutcome(StrEnum):
    """What fetching and staging a file produced."""

    STAGED = "staged"
    SOURCE_CHANGED = "source_changed"


class StageResult(DomainModel):
    """A staged version, or why the fetched bytes were rejected."""

    outcome: StageOutcome
    staged: StagedRef | None = None
    reason: str | None = None


class Inspection(DomainModel):
    """What reading a snapshot established about the content."""

    media_type: str
    perceptual: PerceptualFingerprint | None = None
    note: str | None = Field(
        default=None,
        description="Why an image has no perceptual fingerprint: it did not decode, or it "
        "exceeds the CPU worker's decode budget.",
    )


class FingerprintRecord(DomainModel):
    """The content object a source now resolves to."""

    content_object_id: ContentObjectId
    newly_created: bool


def observed_marker(source: SourceObject) -> str:
    """What a scan saw of a file's version: its marker, or its size and time."""
    if source.version_marker is not None:
        return f"v{source.version_marker}"
    return f"s{source.size_bytes}-t{int(source.modified_at.timestamp())}"


def is_current(source: SourceObject | None, observed: str) -> bool:
    """Whether a source is still the version an ingest was started for, and unfinished."""
    return (
        source is not None
        and observed_marker(source) == observed
        and source.state is SourceObjectState.DISCOVERED
    )


async def stage_source(
    files: ProviderFiles,
    writer: StagingWriterBackend,
    source: SourceObject,
    *,
    scratch_root: Path,
    budget: LocalScratchBudget,
    publish_on_staging_loop: PublishRunner,
) -> StageResult:
    """Fetch a source's content and publish it into staging, unless it changed.

    The provider stream lands on local scratch first; the publish is handed to the
    staging loop, which owns the writer's connections. A size that differs from the
    listing is caught before anything is uploaded; reported digests that differ are
    caught after, and the staged entry is released so nothing downstream reads it.

    Raises:
        ProviderError: the provider could not deliver the content.
        StagingCapacityExhaustedError: local scratch or staging cannot admit it now.
        StagingError: the publish failed.
    """
    key = derive_staging_key(source.id)
    async with fetched(
        files,
        source.native_id,
        size_bytes=source.size_bytes,
        scratch_root=scratch_root,
        budget=budget,
    ) as content:
        if content.size_bytes != source.size_bytes:
            reason = source_change(source, size_bytes=content.size_bytes, sha1="", sha256="")
            return StageResult(outcome=StageOutcome.SOURCE_CHANGED, reason=reason)
        published = await publish_on_staging_loop(
            publish(writer, key, content.handle, size_bytes=content.size_bytes)
        )
    reason = source_change(
        source,
        size_bytes=published.staged.size_bytes,
        sha1=published.sha1,
        sha256=published.staged.sha256,
    )
    if reason is not None:
        await publish_on_staging_loop(writer.occupancy.release(key))
        return StageResult(outcome=StageOutcome.SOURCE_CHANGED, reason=reason)
    return StageResult(
        outcome=StageOutcome.STAGED,
        staged=StagedRef(
            key=key,
            sha256=published.staged.sha256,
            sha1=published.sha1,
            size_bytes=published.staged.size_bytes,
        ),
    )


async def inspect_staged(
    reader: LocalSnapshotReader, staged: StagedRef, *, max_pixels: int = DEFAULT_MAX_PIXELS
) -> Inspection:
    """Read a verified snapshot of exactly the staged version and inspect it.

    Decoding runs in a thread so the loop serving other snapshots is never blocked on it.
    An image no decoder accepts, or one that would decode to more than `max_pixels`
    pixels, keeps its sniffed type and gets no perceptual fingerprint, with a note saying
    why; the strong fingerprint does not depend on decoding.

    Raises:
        StagedEntryMissingError: the entry is gone (evicted before it was read).
        StagedEntrySupersededError: a newer version of the file replaced it.
        StagingError: the snapshot could not be read or failed verification.
    """
    async with reader.open(StagingKey(staged.key), expected_sha256=staged.sha256) as handle:
        media_type = await asyncio.to_thread(sniff, handle)
        if not is_image(media_type):
            return Inspection(media_type=media_type)
        try:
            perceptual = await asyncio.to_thread(
                perceptual_fingerprint, handle, max_pixels=max_pixels
            )
        except (ImageTooLargeError, ImageUndecodableError) as error:
            return Inspection(media_type=media_type, note=str(error))
        return Inspection(media_type=media_type, perceptual=perceptual)


async def record_fingerprint(
    sources: SourceObjectRepository,
    contents: ContentObjectRepository,
    *,
    source_object_id: UUID,
    observed: str,
    staged: StagedRef,
    inspection: Inspection,
) -> FingerprintRecord | None:
    """Create or find the content object and link the source to it.

    Returns None, writing nothing, when the source has moved on since this ingest began:
    a newer version's ingest will link it instead.
    """
    source = await sources.get(SourceObjectId(source_object_id))
    if source is None or not is_current(source, observed):
        return None
    candidate = ContentObject.build(
        fingerprint=content_fingerprint(staged.sha256),
        size_bytes=staged.size_bytes,
        media_type=inspection.media_type,
        perceptual_fingerprint=inspection.perceptual,
    )
    existing = await contents.get(candidate.id)
    stored = await contents.upsert(candidate)
    await sources.attach_content_object(source.id, stored.id)
    return FingerprintRecord(content_object_id=stored.id, newly_created=existing is None)


async def record_source_failure(
    sources: SourceObjectRepository,
    audit: AuditRepository,
    *,
    source_object_id: UUID,
    observed: str,
    reason: str,
    entry_type: AuditEntryType,
    now: AwareDatetime,
) -> bool:
    """Mark a source failed without creating a content object, and audit why.

    The audit entry is written whatever the source's state: the attempt happened. The
    source is only marked failed if it is still the version that failed.

    Returns:
        Whether the source was marked failed.
    """
    source = await sources.get(SourceObjectId(source_object_id))
    await audit.append(
        AuditEntry(
            entry_type=entry_type,
            occurred_at=now,
            decided_by=DECIDED_BY,
            details={"reason": reason, "observed": observed},
            source_object_id=SourceObjectId(source_object_id),
        )
    )
    if source is None or not is_current(source, observed):
        return False
    await sources.mark_failed(source.id, reason)
    return True
