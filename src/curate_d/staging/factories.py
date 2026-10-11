"""Construct role-bound staging backends and check readiness using one identity.

Workflow callers wrap admission, upload and registration together in one DBOS step,
retaining the writer lock until publication commits; that step also releases a superseded
version. Reads are separate steps, and nothing releases an entry after its consumers
finish: it ages out through idle cleanup or ceiling admission. Keep decoder handles
inside the consuming step; step results must be serializable, so never return an open
file. Cache-index operations are retry-safe operational state. Source and placement
ledger mutations still use the DBOS datasource.
No staging effect is performed directly in a replayed workflow body.
"""

import threading
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import timedelta
from typing import BinaryIO, Final
from uuid import uuid4

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from curate_d.clock import utc_now
from curate_d.config import Settings, StagingRole
from curate_d.observability import get_logger
from curate_d.staging.cache_index import PostgresStagingCacheIndex
from curate_d.staging.errors import (
    StagingError,
    StagingNotConfiguredError,
    StagingNotReadyError,
    StagingRoleMismatchError,
)
from curate_d.staging.keys import StagingKey
from curate_d.staging.object_store import (
    S3ClientFactory,
    S3StagingObjectStore,
    build_client_factory,
)
from curate_d.staging.objects import StagedObject, StagingObjectStore
from curate_d.staging.occupancy import (
    Clock,
    CoordinatedStagingIndex,
    ReclaimReport,
    StagingOccupancy,
)
from curate_d.staging.snapshots import LocalScratchBudget, LocalSnapshotReader

logger = get_logger(__name__)

# Concurrent ledger connections this backend's own pool will open. Sized against the CPU
# profile's decode concurrency rather than against a library default: a reader holds one
# connection per in-flight snapshot, for the remote read alone, and the writer's sweep
# needs headroom beside them. Not a setting, because the number that would actually want
# tuning is the queue concurrency it is derived from.
_POOL_SIZE: Final = 5
_POOL_MAX_OVERFLOW: Final = 10

_scratch_budget_guard: Final = threading.Lock()
_scratch_budget: LocalScratchBudget | None = None


@dataclass(frozen=True)
class _DownloadStore:
    """Expose only the download operation to a CPU snapshot reader."""

    _download: Callable[[StagedObject, BinaryIO], Awaitable[None]]

    async def download(self, obj: StagedObject, destination: BinaryIO) -> None:
        """Download through the reader identity."""
        await self._download(obj, destination)


@dataclass(frozen=True)
class _DownloadHold:
    """Expose only the shared download lock to a CPU snapshot reader."""

    _hold: Callable[[StagingKey], AbstractAsyncContextManager[StagedObject | None]]

    def hold_for_download(
        self, key: StagingKey
    ) -> AbstractAsyncContextManager[StagedObject | None]:
        """Hold the registration while its bytes are downloaded."""
        return self._hold(key)


@dataclass(frozen=True)
class StagingWriterBackend:
    """Provide coordinated writes and cleanup to the I/O profile.

    `refresh_readiness` is public because the supervisor's idle tick is its only
    caller and calling it is part of this backend's contract, not an internal detail.
    It is absent on a backend assembled without a marker-publishing identity.
    """

    store: StagingObjectStore
    index: CoordinatedStagingIndex
    occupancy: StagingOccupancy
    upload_max_age: timedelta
    refresh_readiness: Callable[[], Awaitable[None]] | None = field(default=None, repr=False)
    _engine: AsyncEngine | None = field(default=None, repr=False)

    async def aclose(self) -> None:
        """Dispose the owned engine, leaving injected engines to their callers."""
        if self._engine is not None:
            await self._engine.dispose()


@dataclass(frozen=True)
class StagingReaderBackend:
    """Provide snapshots and one process-wide scratch budget to the CPU profile."""

    reader: LocalSnapshotReader
    budget: LocalScratchBudget
    _engine: AsyncEngine | None = field(default=None, repr=False)

    async def aclose(self) -> None:
        """Dispose the owned engine, leaving injected engines to their callers."""
        if self._engine is not None:
            await self._engine.dispose()


@dataclass(frozen=True)
class CleanupReport:
    """Report completed-object reclamation separately from residual multipart cleanup."""

    reclaimed: ReclaimReport
    uploads_aborted: int


def local_scratch_budget(settings: Settings) -> LocalScratchBudget:
    """Return the process-wide reservation budget, creating it once.

    Raises:
        ValueError: A later caller requests a different ceiling. Replacing the budget
            would discard reservations held by existing readers.
    """
    global _scratch_budget
    with _scratch_budget_guard:
        if _scratch_budget is None:
            _scratch_budget = LocalScratchBudget(settings.staging_local_scratch_max_bytes)
        if _scratch_budget.max_bytes != settings.staging_local_scratch_max_bytes:
            raise ValueError("local scratch budget is already configured with a different bound")
        return _scratch_budget


def build_writer_backend(
    settings: Settings,
    *,
    client_factory: S3ClientFactory | None = None,
    engine: AsyncEngine | None = None,
) -> StagingWriterBackend:
    """Build coordinated staging for an explicitly configured writer.

    Args:
        client_factory: Substitute the SDK boundary; otherwise use explicit settings.
        engine: Inject a caller-owned pool; otherwise create and own one.

    Raises:
        StagingNotConfiguredError: S3 or its content prefix is unconfigured.
        StagingRoleMismatchError: The process declares the reader role.
    """
    _require_configured(settings)
    _require_role(settings, StagingRole.WRITER)
    resolved, owned = _resolve_engine(settings, engine)
    index = PostgresStagingCacheIndex(resolved)
    store = S3StagingObjectStore(settings, client_factory=client_factory)
    max_age = timedelta(hours=settings.staging_max_age_hours)

    async def refresh() -> None:
        """Refresh the marker with this worker's writer identity."""
        await refresh_readiness_marker(settings, client_factory=client_factory)

    return StagingWriterBackend(
        store=store,
        index=index,
        occupancy=StagingOccupancy(
            store, index, max_bytes=settings.staging_max_bytes, max_age=max_age
        ),
        upload_max_age=max_age,
        refresh_readiness=refresh,
        _engine=owned,
    )


def build_reader_backend(
    settings: Settings,
    *,
    client_factory: S3ClientFactory | None = None,
    engine: AsyncEngine | None = None,
) -> StagingReaderBackend:
    """Build a CPU snapshot reader whose dependencies expose no mutation methods.

    Args:
        client_factory: Substitute the SDK boundary; otherwise use explicit settings.
        engine: Inject a caller-owned pool; otherwise create and own one.

    Raises:
        StagingNotConfiguredError: S3 or its content prefix is unconfigured.
        StagingRoleMismatchError: The process declares the writer role.
    """
    _require_configured(settings)
    _require_role(settings, StagingRole.READER)
    budget = local_scratch_budget(settings)
    resolved, owned = _resolve_engine(settings, engine)
    hold = _DownloadHold(PostgresStagingCacheIndex(resolved).hold_for_download)
    return StagingReaderBackend(
        reader=LocalSnapshotReader(
            _DownloadStore(S3StagingObjectStore(settings, client_factory=client_factory).download),
            hold,
            scratch_root=settings.staging_local_scratch_root,
            budget=budget,
        ),
        budget=budget,
        _engine=owned,
    )


async def check_staging_ready(
    settings: Settings, *, client_factory: S3ClientFactory | None = None
) -> None:
    """Verify staging with the worker's own identity before queue startup.

    Writers check their occupancy listing, verify PUT/GET/DELETE on an isolated
    temporary attempt, verify the separate multipart grants every large upload and
    every cleanup tick needs, and create the zero-byte prefix marker. Readers require a
    successful GET of that marker. An absent-key denial cannot prove
    access, so provisioning or an I/O worker must seed it before CPU startup.

    Raises:
        StagingNotReadyError: Access, connectivity or marker verification failed.
        StagingNotConfiguredError: S3 or its content prefix is unconfigured.
    """
    _require_configured(settings)
    factory = client_factory or build_client_factory(settings)
    if settings.staging_s3_role is StagingRole.WRITER:
        await _probe_as_writer(settings, factory)
        await _probe_writer_mutations(settings, factory)
        await _probe_writer_multipart(settings, factory)
        await refresh_readiness_marker(settings, client_factory=factory)
    else:
        await _read_readiness_marker(settings, factory)
    logger.info(
        "staging.ready", bucket=settings.staging_s3_bucket, role=settings.staging_s3_role.value
    )


async def refresh_readiness_marker(
    settings: Settings, *, client_factory: S3ClientFactory | None = None
) -> None:
    """Create and verify the zero-byte content-prefix marker using writer authority.

    The adapter already excludes the exact prefix placeholder from content listings,
    so reconciliation cannot evict it. Refresh it each idle tick to keep the expiration
    backstop from aging it out during normal operation. After a prolonged outage the
    I/O profile must recreate it before CPU workers can pass readiness.

    Raises:
        StagingNotConfiguredError: S3 or its content prefix is unconfigured.
        StagingRoleMismatchError: The process declares the reader role.
        StagingNotReadyError: The marker cannot be written or verified.
    """
    _require_configured(settings)
    _require_role(settings, StagingRole.WRITER)
    factory = client_factory or build_client_factory(settings)
    try:
        async with factory() as client:
            await client.put_object(
                Bucket=settings.staging_s3_bucket,
                Key=settings.staging_s3_content_prefix,
                Body=b"",
                StorageClass="STANDARD",
            )
    except (BotoCoreError, ClientError) as error:
        raise _not_ready(settings, f"marker write failed ({type(error).__name__})") from None
    await _read_readiness_marker(settings, factory)


async def run_idle_cleanup(
    backend: StagingWriterBackend, *, clock: Clock = utc_now
) -> CleanupReport:
    """Refresh readiness, sweep completed content and abort abandoned multipart uploads.

    Completed bytes and incomplete uploads have independent cleanup mechanisms. Failures
    propagate to the supervisor for reporting and retry on the next tick.
    """
    if backend.refresh_readiness is not None:
        await backend.refresh_readiness()
    try:
        reclaimed = await backend.occupancy.sweep()
    except (StagingError, SQLAlchemyError):
        # Residual uploads remain billable even when completed-content cleanup fails.
        try:
            await backend.store.abort_incomplete_uploads(clock() - backend.upload_max_age)
        except StagingError as error:
            logger.warning("staging.residual_cleanup_failed", error_type=type(error).__name__)
        raise
    aborted = await backend.store.abort_incomplete_uploads(clock() - backend.upload_max_age)
    return CleanupReport(reclaimed=reclaimed, uploads_aborted=aborted)


async def _probe_as_writer(settings: Settings, factory: S3ClientFactory) -> None:
    """Verify the writer's bounded prefix listing without management operations."""
    try:
        async with factory() as client:
            await client.list_objects_v2(
                Bucket=settings.staging_s3_bucket,
                Prefix=settings.staging_s3_content_prefix,
                MaxKeys=1,
            )
    except (ClientError, BotoCoreError) as error:
        raise _not_ready(settings, f"prefix listing failed ({type(error).__name__})") from None


async def _probe_writer_mutations(settings: Settings, factory: S3ClientFactory) -> None:
    """Verify writer upload, read and deletion under an isolated temporary attempt key."""
    # Use the application's attempt shape so an uncertain failed DELETE stays eligible
    # for ordinary orphan reclamation; this probe never shares the persistent marker.
    key = f"{settings.staging_s3_content_prefix}{uuid4().hex}/{uuid4().hex}"
    try:
        async with factory() as client:
            try:
                await client.put_object(
                    Bucket=settings.staging_s3_bucket, Key=key, Body=b"", StorageClass="STANDARD"
                )
                await _read_readiness_marker(settings, factory, key=key)
            finally:
                # A lost PUT response can still leave completed content behind.
                await client.delete_object(Bucket=settings.staging_s3_bucket, Key=key)
    except (ClientError, BotoCoreError) as error:
        raise _not_ready(
            settings, f"writer mutation probe failed ({type(error).__name__})"
        ) from None


async def _probe_writer_multipart(settings: Settings, factory: S3ClientFactory) -> None:
    """Verify the multipart grants that single-object permissions do not imply.

    `CreateMultipartUpload`, `UploadPart` and `CompleteMultipartUpload` all resolve to
    `s3:PutObject`, but stopping an upload needs `s3:AbortMultipartUpload` and listing
    uploads needs `s3:ListBucketMultipartUploads` — two actions a policy written against
    the single-object API surface omits. Without them every object past the part
    threshold dies un-abortable with its parts still billing, and every idle cleanup tick
    raises. Create one upload, list, and abort it; no part is ever sent, so the probe
    stores nothing and a denied abort leaves no billable residue of its own.
    """
    key = f"{settings.staging_s3_content_prefix}{uuid4().hex}/{uuid4().hex}"
    try:
        async with factory() as client:
            created = await client.create_multipart_upload(
                Bucket=settings.staging_s3_bucket, Key=key, StorageClass="STANDARD"
            )
            try:
                await client.list_multipart_uploads(
                    Bucket=settings.staging_s3_bucket,
                    Prefix=settings.staging_s3_content_prefix,
                    MaxUploads=1,
                )
            finally:
                # A denied listing must still leave nothing behind, so the abort runs
                # regardless; its own denial is exactly what this probe is here to catch.
                await client.abort_multipart_upload(
                    Bucket=settings.staging_s3_bucket, Key=key, UploadId=created["UploadId"]
                )
    except (ClientError, BotoCoreError) as error:
        raise _not_ready(settings, f"multipart probe failed ({type(error).__name__})") from None


async def _read_readiness_marker(
    settings: Settings, factory: S3ClientFactory, *, key: str | None = None
) -> None:
    """Require a successful GET of the known zero-byte marker, closing its body."""
    try:
        async with factory() as client:
            response = await client.get_object(
                Bucket=settings.staging_s3_bucket, Key=key or settings.staging_s3_content_prefix
            )
            body = response["Body"]
            try:
                if response.get("ContentLength") != 0 or await body.read(1) != b"":
                    raise _not_ready(settings, "readiness marker is not empty")
            finally:
                body.close()
    except (ClientError, BotoCoreError) as error:
        raise _not_ready(
            settings,
            f"marker GET failed ({type(error).__name__}); start I/O or run provisioning smoke",
        ) from None


def _not_ready(settings: Settings, reason: str) -> StagingNotReadyError:
    """Build a readiness failure without SDK messages or credentials."""
    return StagingNotReadyError(
        f"staging bucket {settings.staging_s3_bucket!r} is not ready for this "
        f"{settings.staging_s3_role.value}: {reason}. There is no filesystem fallback; "
        f"correct the endpoint, bucket or identity and restart."
    )


def _require_configured(settings: Settings) -> None:
    """Require explicit S3 settings and a prefix usable as a readiness object key."""
    if not settings.s3_staging_enabled:
        raise StagingNotConfiguredError(
            "no S3 staging endpoint is configured; set CURATE_D_STAGING_S3_ENDPOINT_URL "
            "and its identity. There is no filesystem staging fallback."
        )
    if not settings.staging_s3_content_prefix:
        raise StagingNotConfiguredError("staging requires a nonempty content prefix for readiness")


def _require_role(settings: Settings, expected: StagingRole) -> None:
    """Reject a factory whose role does not match the configured identity."""
    if settings.staging_s3_role is not expected:
        raise StagingRoleMismatchError(
            f"the configured staging {settings.staging_s3_role.value} identity "
            f"cannot build the {expected.value} side of staging"
        )


def _resolve_engine(
    settings: Settings, engine: AsyncEngine | None
) -> tuple[AsyncEngine, AsyncEngine | None]:
    """Return the selected engine and the engine this backend owns, if any."""
    if engine is not None:
        return engine, None
    created = _build_engine(settings)
    return created, created


def _build_engine(settings: Settings) -> AsyncEngine:
    """Create a lazily connected cache pool separate from DBOS ledger transactions.

    The pool bound is stated rather than inherited: it is the ceiling on concurrent
    snapshot downloads, because each one holds a connection for the length of its remote
    read, and exhaustion surfaces as a checkout timeout far from the code that caused it.
    A ceiling that moves with a library default would move that limit without anyone
    choosing to.
    """
    return create_async_engine(
        str(settings.ledger_database_url),
        future=True,
        pool_size=_POOL_SIZE,
        max_overflow=_POOL_MAX_OVERFLOW,
    )
