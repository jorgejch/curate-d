"""Verify staging construction, role authority, readiness and idle reclamation."""

import io
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, Mock

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from pydantic import HttpUrl, SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import QueuePool

from curate_d.config import Settings, StagingRole
from curate_d.staging import factories
from curate_d.staging.errors import (
    StagingNotConfiguredError,
    StagingNotReadyError,
    StagingObjectStoreError,
    StagingRoleMismatchError,
)
from curate_d.staging.keys import StagingKey
from curate_d.staging.object_store import S3ClientFactory
from curate_d.staging.objects import StagedObject, StagingDownloadStore, StagingObjectStore
from curate_d.staging.occupancy import CoordinatedStagingIndex, ReclaimReport, StagingOccupancy
from curate_d.staging.snapshots import StagingDownloadHold

if TYPE_CHECKING:
    from types_aiobotocore_s3.client import S3Client


def settings_for(role: StagingRole = StagingRole.WRITER) -> Settings:
    """Build explicit local staging configuration without ambient secrets."""
    return Settings(
        staging_s3_endpoint_url=HttpUrl("http://127.0.0.1:9000"),
        staging_s3_bucket="staging-test",
        staging_s3_access_key_id=SecretStr("access-secret"),
        staging_s3_secret_access_key=SecretStr("secret-secret"),
        staging_s3_verify_tls=False,
        staging_s3_role=role,
    )


@pytest.fixture(autouse=True)
def isolate_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep process singleton state local to each isolated configuration."""
    monkeypatch.setattr(factories, "_scratch_budget", None)


@pytest.fixture
def client() -> Mock:
    """Supply only the role readiness SDK operations."""
    result = Mock()
    result.list_objects_v2 = AsyncMock(return_value={})
    body = Mock()
    body.read = AsyncMock(return_value=b"")
    result.get_object = AsyncMock(return_value={"Body": body, "ContentLength": 0})
    result.put_object = AsyncMock(return_value={})
    result.delete_object = AsyncMock(return_value={})
    result.create_multipart_upload = AsyncMock(return_value={"UploadId": "probe-upload"})
    result.list_multipart_uploads = AsyncMock(return_value={})
    result.abort_multipart_upload = AsyncMock(return_value={})
    return result


def client_factory(client: Mock) -> S3ClientFactory:
    """Open the substituted client at the SDK boundary."""

    @asynccontextmanager
    async def open_client() -> AsyncGenerator["S3Client", None]:
        yield cast("S3Client", client)

    return open_client


@pytest.mark.parametrize(
    "builder", [factories.build_writer_backend, factories.build_reader_backend]
)
def test_no_configuration_has_no_filesystem_fallback(builder: object) -> None:
    """Reject absent S3 settings before constructing any backend."""
    assert callable(builder)
    with pytest.raises(StagingNotConfiguredError, match="no S3"):
        builder(Settings(staging_s3_endpoint_url=None))


@pytest.mark.parametrize("role", list(StagingRole))
def test_opposite_role_is_rejected_before_engine_creation(
    role: StagingRole, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refuse the opposite factory before creating resources."""
    engine = Mock(side_effect=AssertionError("must not create engine"))
    monkeypatch.setattr(factories, "_build_engine", engine)
    builder = (
        factories.build_reader_backend
        if role is StagingRole.WRITER
        else factories.build_writer_backend
    )
    with pytest.raises(StagingRoleMismatchError):
        builder(settings_for(role))
    engine.assert_not_called()


async def test_writer_backend_owns_and_disposes_created_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dispose only the engine created by the writer factory."""
    engine = Mock(spec=AsyncEngine)
    engine.dispose = AsyncMock()
    monkeypatch.setattr(factories, "_build_engine", lambda settings: engine)
    backend = factories.build_writer_backend(settings_for())
    assert isinstance(backend.store, StagingObjectStore)
    assert isinstance(backend.index, CoordinatedStagingIndex)
    assert backend.upload_max_age == timedelta(hours=24)
    await backend.aclose()
    engine.dispose.assert_awaited_once()


def test_a_reservation_outlives_two_transfer_deadlines() -> None:
    """The TTL derives from the transfer timeout, so raising one raises the other."""
    settings = settings_for().model_copy(update={"staging_transfer_timeout_seconds": 45.0})
    backend = factories.build_writer_backend(settings, engine=Mock(spec=AsyncEngine))
    assert backend.occupancy.reservation_ttl == timedelta(seconds=90)


async def test_reader_has_only_download_and_hold_surfaces() -> None:
    """Keep mutation methods structurally absent from the CPU reader's dependencies."""
    engine = create_async_engine(str(settings_for().ledger_database_url))
    try:
        backend = factories.build_reader_backend(settings_for(StagingRole.READER), engine=engine)
        assert not hasattr(backend, "store")
        assert not hasattr(backend, "index")
        assert isinstance(backend.reader._store, StagingDownloadStore)
        assert not isinstance(backend.reader._store, StagingObjectStore)
        assert isinstance(backend.reader._index, StagingDownloadHold)
        for operation in ("upload", "delete", "list_objects", "abort_incomplete_uploads"):
            assert not hasattr(backend.reader._store, operation)
        for operation in ("register", "release", "evict", "reconcile", "writer_lock"):
            assert not hasattr(backend.reader._index, operation)
        await backend.aclose()
    finally:
        await engine.dispose()


async def test_reader_disposes_owned_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dispose the reader's owned engine on shutdown."""
    engine = Mock(spec=AsyncEngine)
    engine.dispose = AsyncMock()
    monkeypatch.setattr(factories, "_build_engine", lambda settings: engine)
    backend = factories.build_reader_backend(settings_for(StagingRole.READER))
    await backend.aclose()
    engine.dispose.assert_awaited_once()


@pytest.mark.parametrize("role", list(StagingRole))
async def test_injected_engine_remains_owned_by_caller(role: StagingRole) -> None:
    """Leave an injected connection pool available to its owner."""
    engine = Mock(spec=AsyncEngine)
    engine.dispose = AsyncMock()
    builder = (
        factories.build_writer_backend
        if role is StagingRole.WRITER
        else factories.build_reader_backend
    )
    backend = builder(settings_for(role), engine=cast(AsyncEngine, engine))
    await backend.aclose()
    engine.dispose.assert_not_awaited()


async def test_narrow_adapters_delegate_only_download_and_hold() -> None:
    """Preserve verified download and shared-lock behavior through the narrow adapters."""
    download = AsyncMock()
    store = factories._DownloadStore(download)
    obj = Mock(spec=StagedObject)
    sink = io.BytesIO()
    await store.download(obj, sink)
    download.assert_awaited_once_with(obj, sink)

    @asynccontextmanager
    async def hold(key: StagingKey) -> AsyncGenerator[StagedObject | None, None]:
        assert key == StagingKey("0" * 32)
        yield None

    async with factories._DownloadHold(hold).hold_for_download(StagingKey("0" * 32)) as value:
        assert value is None


def test_scratch_budget_is_shared_and_rejects_changed_bound() -> None:
    """Enforce one reservation ceiling across independently constructed readers."""
    settings = settings_for(StagingRole.READER)
    first = factories.local_scratch_budget(settings)
    assert factories.local_scratch_budget(settings) is first
    with first.reserve(first.max_bytes):
        assert factories.local_scratch_budget(settings).reserved_bytes == first.max_bytes
    with pytest.raises(ValueError, match="different bound"):
        factories.local_scratch_budget(
            settings.model_copy(update={"staging_local_scratch_max_bytes": 1})
        )


async def test_writer_readiness_lists_and_publishes_marker(client: Mock) -> None:
    """Verify the writer's listing and marker publication with its own identity."""
    await factories.check_staging_ready(settings_for(), client_factory=client_factory(client))
    client.list_objects_v2.assert_awaited_once_with(
        Bucket="staging-test", Prefix="content/", MaxKeys=1
    )
    client.put_object.assert_any_await(
        Bucket="staging-test", Key="content/", Body=b"", StorageClass="STANDARD"
    )
    client.get_object.assert_any_await(Bucket="staging-test", Key="content/")
    assert client.put_object.await_count == 2
    assert client.get_object.await_count == 2
    client.delete_object.assert_awaited_once()
    assert client.delete_object.call_args.kwargs["Key"] != "content/"


async def test_writer_readiness_exercises_the_multipart_grants(client: Mock) -> None:
    """Prove create, list and abort authority without storing a single part."""
    await factories.check_staging_ready(settings_for(), client_factory=client_factory(client))
    client.create_multipart_upload.assert_awaited_once()
    created = client.create_multipart_upload.call_args.kwargs
    assert created["Bucket"] == "staging-test"
    assert created["Key"].startswith("content/") and created["Key"] != "content/"
    client.list_multipart_uploads.assert_awaited_once_with(
        Bucket="staging-test", Prefix="content/", MaxUploads=1
    )
    client.abort_multipart_upload.assert_awaited_once_with(
        Bucket="staging-test", Key=created["Key"], UploadId="probe-upload"
    )
    client.upload_part.assert_not_called()
    client.complete_multipart_upload.assert_not_called()


@pytest.mark.parametrize(
    "operation", ["create_multipart_upload", "list_multipart_uploads", "abort_multipart_upload"]
)
async def test_missing_multipart_grant_fails_readiness(client: Mock, operation: str) -> None:
    """Refuse a writer that can move whole objects but cannot run multipart uploads."""
    getattr(client, operation).side_effect = ClientError(
        {"Error": {"Code": "AccessDenied"}}, operation
    )
    with pytest.raises(StagingNotReadyError, match="multipart probe"):
        await factories.check_staging_ready(settings_for(), client_factory=client_factory(client))
    # The marker is published only after every writer probe passes.
    assert all(call.kwargs["Key"] != "content/" for call in client.put_object.await_args_list)


async def test_denied_multipart_listing_still_aborts_the_probe_upload(client: Mock) -> None:
    """Leave no residual upload behind when the listing grant is the missing one."""
    client.list_multipart_uploads.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied"}}, "ListMultipartUploads"
    )
    with pytest.raises(StagingNotReadyError, match="multipart probe"):
        await factories.check_staging_ready(settings_for(), client_factory=client_factory(client))
    client.abort_multipart_upload.assert_awaited_once_with(
        Bucket="staging-test",
        Key=client.create_multipart_upload.call_args.kwargs["Key"],
        UploadId="probe-upload",
    )


@pytest.mark.parametrize("code", ["NoSuchKey", "404"])
async def test_reader_requires_existing_marker(client: Mock, code: str) -> None:
    """Fail readiness when the managed probe has not been provisioned."""
    client.get_object.side_effect = ClientError({"Error": {"Code": code}}, "GetObject")
    with pytest.raises(StagingNotReadyError, match="start I/O"):
        await factories.check_staging_ready(
            settings_for(StagingRole.READER), client_factory=client_factory(client)
        )
    client.list_objects_v2.assert_not_called()
    client.put_object.assert_not_called()


@pytest.mark.parametrize("role", list(StagingRole))
@pytest.mark.parametrize(
    "code",
    [
        "AccessDenied",
        "403",
        "NoSuchBucket",
        "InvalidAccessKeyId",
        "SignatureDoesNotMatch",
        "InternalError",
    ],
)
async def test_readiness_fails_closed_on_denial_or_missing_bucket(
    client: Mock, role: StagingRole, code: str
) -> None:
    """Refuse inaccessible buckets and rejected credentials without false readiness."""
    error = ClientError({"Error": {"Code": code}}, "Request")
    client.get_object.side_effect = client.list_objects_v2.side_effect = error
    with pytest.raises(StagingNotReadyError, match="no filesystem fallback"):
        await factories.check_staging_ready(
            settings_for(role), client_factory=client_factory(client)
        )


@pytest.mark.parametrize("role", list(StagingRole))
async def test_endpoint_failure_does_not_expose_sdk_exception_secrets(
    client: Mock, role: StagingRole
) -> None:
    """Omit credential-bearing SDK exception text from readiness failures."""
    error = EndpointConnectionError(endpoint_url="https://access-secret:secret-secret@host")
    client.get_object.side_effect = client.list_objects_v2.side_effect = error
    with pytest.raises(StagingNotReadyError) as caught:
        await factories.check_staging_ready(
            settings_for(role), client_factory=client_factory(client)
        )
    assert "access-secret" not in str(caught.value)
    assert "secret-secret" not in str(caught.value)
    assert caught.value.__suppress_context__


async def test_successful_reader_probe_closes_body(client: Mock) -> None:
    """Close an unexpected successful GET response without leaking its connection."""
    body = Mock()
    body.read = AsyncMock(return_value=b"")
    client.get_object.return_value = {"Body": body, "ContentLength": 0}
    await factories.check_staging_ready(
        settings_for(StagingRole.READER), client_factory=client_factory(client)
    )
    body.close.assert_called_once()


async def test_default_readiness_client_uses_explicit_settings(
    client: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Build the SDK client from the worker's own settings when none is substituted."""
    settings = settings_for()
    build = Mock(return_value=client_factory(client))
    monkeypatch.setattr(factories, "build_client_factory", build)
    await factories.check_staging_ready(settings)
    build.assert_called_once_with(settings)


async def test_idle_cleanup_sweeps_completed_and_aborts_residual_uploads() -> None:
    """Run both independent cleanup mechanisms without any incoming job."""
    occupancy = Mock(spec=StagingOccupancy)
    report = ReclaimReport((), (), 2, 40)
    occupancy.sweep = AsyncMock(return_value=report)
    store = Mock(spec=StagingObjectStore)
    store.abort_incomplete_uploads = AsyncMock(return_value=3)
    backend = factories.StagingWriterBackend(store, Mock(), occupancy, timedelta(hours=24))
    now = datetime(2026, 9, 15, tzinfo=UTC)
    result = await factories.run_idle_cleanup(backend, clock=lambda: now)
    assert result == factories.CleanupReport(report, 3)
    occupancy.sweep.assert_awaited_once()
    store.abort_incomplete_uploads.assert_awaited_once_with(now - timedelta(hours=24))


async def test_cleanup_failure_is_visible_and_retryable() -> None:
    """Propagate sweep failures so the supervisor can report and retry them."""
    occupancy = Mock(spec=StagingOccupancy)
    occupancy.sweep = AsyncMock(side_effect=StagingObjectStoreError("listing failed"))
    store = Mock(spec=StagingObjectStore)
    backend = factories.StagingWriterBackend(store, Mock(), occupancy, timedelta(hours=24))
    with pytest.raises(StagingObjectStoreError):
        await factories.run_idle_cleanup(backend)
    store.abort_incomplete_uploads.assert_awaited_once()


@pytest.mark.parametrize("length,payload", [(1, b"x"), (0, b"x"), (None, b"")])
async def test_reader_rejects_corrupt_or_unverifiable_marker(
    client: Mock, length: int | None, payload: bytes
) -> None:
    """Reject unexpected marker contents and always release the response connection."""
    body = Mock()
    body.read = AsyncMock(return_value=payload)
    client.get_object.return_value = {"Body": body, "ContentLength": length}
    with pytest.raises(StagingNotReadyError, match="not empty"):
        await factories.check_staging_ready(
            settings_for(StagingRole.READER), client_factory=client_factory(client)
        )
    body.close.assert_called_once()


async def test_reader_cannot_refresh_marker(client: Mock) -> None:
    """Refuse marker publication before any request from the CPU identity."""
    with pytest.raises(StagingRoleMismatchError):
        await factories.refresh_readiness_marker(
            settings_for(StagingRole.READER), client_factory=client_factory(client)
        )
    client.put_object.assert_not_called()


async def test_marker_write_failure_is_fail_closed(client: Mock) -> None:
    """Reject readiness when writer marker creation is denied."""
    client.put_object.side_effect = ClientError({"Error": {"Code": "AccessDenied"}}, "PutObject")
    with pytest.raises(StagingNotReadyError, match="marker write"):
        await factories.refresh_readiness_marker(
            settings_for(), client_factory=client_factory(client)
        )
    client.get_object.assert_not_called()


async def test_idle_cleanup_refreshes_marker_before_sweep(
    client: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Renew the expiration timestamp on every idle pass, independently of jobs."""
    engine = Mock(spec=AsyncEngine)
    backend = factories.build_writer_backend(
        settings_for(), client_factory=client_factory(client), engine=engine
    )
    assert backend.refresh_readiness is not None
    swept = AsyncMock(return_value=ReclaimReport((), (), 0, 0))
    monkeypatch.setattr(backend.occupancy, "sweep", swept)
    monkeypatch.setattr(backend.store, "abort_incomplete_uploads", AsyncMock(return_value=0))
    await factories.run_idle_cleanup(backend)
    await factories.run_idle_cleanup(backend)
    assert client.put_object.await_count == 2
    assert client.get_object.await_count == 2
    assert swept.await_count == 2


async def test_marker_is_excluded_from_application_content_listing(client: Mock) -> None:
    """Keep the exact prefix marker outside occupancy and orphan reconciliation."""
    now = datetime(2026, 9, 15, tzinfo=UTC)

    async def pages(**request: object) -> AsyncIterator[dict[str, object]]:
        yield {"Contents": [{"Key": "content/", "Size": 0, "LastModified": now}]}

    client.get_paginator.return_value.paginate = pages
    backend = factories.build_writer_backend(settings_for(), client_factory=client_factory(client))
    try:
        assert [obj async for obj in backend.store.list_objects()] == []
    finally:
        await backend.aclose()


def test_empty_prefix_cannot_support_managed_readiness() -> None:
    """Reject an empty S3 key before constructing worker resources."""
    with pytest.raises(StagingNotConfiguredError, match="nonempty"):
        factories.build_writer_backend(
            settings_for().model_copy(update={"staging_s3_content_prefix": ""})
        )


async def test_writer_missing_delete_permission_fails_readiness(client: Mock) -> None:
    """Refuse a writer that can upload but cannot perform cleanup."""
    client.delete_object.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied"}}, "DeleteObject"
    )
    with pytest.raises(StagingNotReadyError, match="mutation probe"):
        await factories.check_staging_ready(settings_for(), client_factory=client_factory(client))
    assert client.put_object.await_count == 1
    assert client.put_object.call_args.kwargs["Key"] != "content/"


async def test_failed_writer_probe_read_still_deletes_temporary_object(client: Mock) -> None:
    """Delete the isolated attempt even if its GET verification fails."""
    client.get_object.side_effect = ClientError({"Error": {"Code": "AccessDenied"}}, "GetObject")
    with pytest.raises(StagingNotReadyError):
        await factories.check_staging_ready(settings_for(), client_factory=client_factory(client))
    client.delete_object.assert_awaited_once()
    assert client.delete_object.call_args.kwargs["Key"] == client.put_object.call_args.kwargs["Key"]


async def test_both_cleanup_failures_remain_visible(monkeypatch: pytest.MonkeyPatch) -> None:
    """Report residual failure while preserving the failed completed-content sweep."""
    occupancy = Mock(spec=StagingOccupancy)
    occupancy.sweep = AsyncMock(side_effect=StagingObjectStoreError("sweep failed"))
    store = Mock(spec=StagingObjectStore)
    store.abort_incomplete_uploads = AsyncMock(side_effect=StagingObjectStoreError("abort failed"))
    log = Mock()
    monkeypatch.setattr(factories, "logger", log)
    backend = factories.StagingWriterBackend(store, Mock(), occupancy, timedelta(hours=24))
    with pytest.raises(StagingObjectStoreError, match="sweep failed"):
        await factories.run_idle_cleanup(backend)
    log.warning.assert_called_once_with(
        "staging.residual_cleanup_failed", error_type="StagingObjectStoreError"
    )


async def test_the_cache_pool_states_its_own_bound() -> None:
    """Pin the connection ceiling: it caps concurrent snapshot reads, not a library default."""
    engine = factories._build_engine(settings_for())
    try:
        pool = engine.pool
        assert isinstance(pool, QueuePool)
        assert pool.size() == factories._POOL_SIZE
        assert pool._max_overflow == factories._POOL_MAX_OVERFLOW
    finally:
        await engine.dispose()
