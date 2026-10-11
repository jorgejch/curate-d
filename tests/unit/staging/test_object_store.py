"""The S3 staging adapter, against a substituted SDK client.

Everything here stops at the boundary the adapter actually talks to: a fake client that
records requests and can be told to fail. Real RustFS behaviour is the integration
suite's job, and a local pass there is never evidence about OVH either way.
"""

import asyncio
import hashlib
import io
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast
from uuid import UUID

import aioboto3
import pytest
from aiobotocore.config import AioConfig
from botocore.exceptions import ClientError, EndpointConnectionError, NoRegionError
from pydantic import HttpUrl, SecretStr
from structlog.testing import capture_logs

from curate_d.config import S3AddressingStyle, Settings, StagingRole
from curate_d.staging.errors import (
    StagedEntryMissingError,
    StagingAccessDeniedError,
    StagingBucketMissingError,
    StagingIntegrityError,
    StagingObjectStoreError,
    StagingTransientError,
)
from curate_d.staging.keys import StagingKey
from curate_d.staging.object_store import (
    _MAX_PART_SIZE,
    _MAX_PARTS,
    S3ClientFactory,
    S3StagingObjectStore,
    build_client_factory,
)
from curate_d.staging.objects import StagedObject, StagingObjectStore

if TYPE_CHECKING:
    from types_aiobotocore_s3.client import S3Client

type Response = dict[str, str | int | datetime | FakeBody]
type ListItem = dict[str, str | int | datetime]
type ListPage = dict[str, list[ListItem]]

_WHEN = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)
_KEY = StagingKey("0" * 32)
_ATTEMPT = UUID("11111111111111111111111111111111")
_OBJECT_KEY = f"{_KEY}/{_ATTEMPT.hex}"
_REMOTE_KEY = f"content/{_OBJECT_KEY}"
_SECRET = "writer-development-secret"

# Small enough that a handful of bytes crosses the multipart boundary. Real backends
# reject a non-final part below 5 MiB; only the fake tolerates this.
_PART_SIZE = 4


def _text(value: object) -> str:
    """Narrow one request argument to the string the SDK would have sent."""
    assert isinstance(value, str)
    return value


def _data(value: object) -> bytes:
    """Narrow one request argument to the body bytes the SDK would have sent."""
    assert isinstance(value, bytes)
    return value


def _number(value: object) -> int:
    """Narrow one request argument to the part number the SDK would have sent."""
    assert isinstance(value, int)
    return value


def _completed_parts(value: object) -> list[tuple[int, str]]:
    """Narrow a completion request's part list to the (number, ETag) pairs it names."""
    assert isinstance(value, dict)
    supplied = value["Parts"]
    assert isinstance(supplied, list)
    pairs: list[tuple[int, str]] = []
    for part in supplied:
        assert isinstance(part, dict)
        pairs.append((_number(part["PartNumber"]), _text(part["ETag"])))
    return pairs


def _client_error(code: str, operation: str) -> ClientError:
    """A botocore error carrying one error code, as either backend would raise it."""
    return ClientError({"Error": {"Code": code, "Message": code}}, operation)


class FakeBody:
    """A GET response body, handed out in bounded chunks."""

    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.offset = 0
        self.largest_request = 0
        self.closed = False

    async def read(self, amt: int) -> bytes:
        self.largest_request = max(self.largest_request, amt)
        chunk = self.payload[self.offset : self.offset + amt]
        self.offset += len(chunk)
        return chunk

    def close(self) -> None:
        self.closed = True


class FakePaginator:
    """One operation's pages, or the failure the SDK would raise instead."""

    def __init__(self, pages: list[ListPage], error: BaseException | None) -> None:
        self.pages = pages
        self.error = error

    def paginate(self, **request: object) -> AsyncIterator[ListPage]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[ListPage]:
        if self.error is not None:
            raise self.error
        for page in self.pages:
            yield page


class FakeS3Client:
    """The SDK surface the adapter uses, recording requests and failing on demand."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.storage_classes: dict[str, str] = {}
        # Keyed by part number, as the service holds them: assembling in call order
        # would let a numbering or ordering defect through unnoticed.
        self.parts: dict[str, dict[int, bytes]] = {}
        self.completed: list[tuple[int, str]] = []
        self.operations: list[str] = []
        self.errors: dict[str, BaseException] = {}
        self.aborted: list[str] = []
        self.missing_uploads: set[str] = set()
        self.pages: dict[str, list[ListPage]] = {}
        self.page_errors: dict[str, BaseException] = {}
        self.head_length: int | None = None
        self.head_storage_class: str | None = None
        self.part_etag = '"9f2c4e6a8b0d1357"'
        self.bodies: list[FakeBody] = []
        self.stalls: dict[str, float] = {}
        self.on_abort: Callable[[], object] | None = None
        self._upload_count = 0

    async def __aenter__(self) -> "FakeS3Client":
        return self

    async def __aexit__(self, *details: object) -> None:
        return None

    def _record(self, operation: str) -> None:
        """Log one request and raise whatever this operation was told to fail with."""
        self.operations.append(operation)
        error = self.errors.get(operation)
        if error is not None:
            raise error

    async def _stall(self, operation: str) -> None:
        """Hold one operation open, so a transfer ceiling has something to expire on."""
        delay = self.stalls.get(operation)
        if delay is not None:
            await asyncio.sleep(delay)

    async def put_object(self, **request: object) -> Response:
        await self._stall("put_object")
        self._record("put_object")
        key = _text(request["Key"])
        self.objects[key] = _data(request["Body"])
        self.storage_classes[key] = _text(request["StorageClass"])
        return {"ETag": '"d41d8cd98f00b204e9800998ecf8427e"'}

    async def create_multipart_upload(self, **request: object) -> Response:
        self._record("create_multipart_upload")
        self._upload_count += 1
        upload_id = f"upload-{self._upload_count}"
        self.parts[upload_id] = {}
        self.storage_classes[_text(request["Key"])] = _text(request["StorageClass"])
        return {"UploadId": upload_id}

    async def upload_part(self, **request: object) -> Response:
        await self._stall("upload_part")
        self._record("upload_part")
        number = _number(request["PartNumber"])
        if number < 1 or number > _MAX_PARTS:
            raise _client_error("InvalidArgument", "UploadPart")
        self.parts[_text(request["UploadId"])][number] = _data(request["Body"])
        return {"ETag": self.part_etag}

    async def complete_multipart_upload(self, **request: object) -> Response:
        self._record("complete_multipart_upload")
        upload_id = _text(request["UploadId"])
        uploaded = self.parts.pop(upload_id)
        self.completed = _completed_parts(request["MultipartUpload"])
        numbers = [number for number, _ in self.completed]
        if numbers != list(range(1, len(uploaded) + 1)):
            # The service assembles from the numbers the completion names, so a request
            # that names them out of order, with a gap, or not at all is rejected here
            # rather than silently reassembled from call order.
            raise _client_error("InvalidPartOrder", "CompleteMultipartUpload")
        self.objects[_text(request["Key"])] = b"".join(uploaded[number] for number in numbers)
        return {"ETag": self.part_etag}

    async def abort_multipart_upload(self, **request: object) -> Response:
        if self.on_abort is not None:
            self.on_abort()
        await self._stall("abort_multipart_upload")
        self._record("abort_multipart_upload")
        upload_id = _text(request["UploadId"])
        if upload_id in self.missing_uploads:
            raise _client_error("NoSuchUpload", "AbortMultipartUpload")
        self.aborted.append(upload_id)
        self.parts.pop(upload_id, None)
        return {}

    async def head_object(self, **request: object) -> Response:
        self._record("head_object")
        key = _text(request["Key"])
        if key not in self.objects:
            raise _client_error("404", "HeadObject")
        length = self.head_length if self.head_length is not None else len(self.objects[key])
        head: Response = {"ContentLength": length, "LastModified": _WHEN}
        # Left out by default: a HEAD that omits the class means STANDARD, and the
        # adapter must accept that rather than demand one backend's spelling.
        if self.head_storage_class is not None:
            head["StorageClass"] = self.head_storage_class
        return head

    async def get_object(self, **request: object) -> Response:
        await self._stall("get_object")
        self._record("get_object")
        key = _text(request["Key"])
        if key not in self.objects:
            raise _client_error("NoSuchKey", "GetObject")
        body = FakeBody(self.objects[key])
        self.bodies.append(body)
        return {"Body": body}

    async def delete_object(self, **request: object) -> Response:
        self._record("delete_object")
        self.objects.pop(_text(request["Key"]), None)
        return {}

    def get_paginator(self, operation_name: str) -> FakePaginator:
        self._record(f"paginate:{operation_name}")
        return FakePaginator(
            self.pages.get(operation_name, []), self.page_errors.get(operation_name)
        )


def _settings(
    *,
    role: StagingRole = StagingRole.WRITER,
    prefix: str = "content/",
    transfer_timeout: float = 300.0,
) -> Settings:
    """Configuration for a local plaintext fixture endpoint with explicit credentials."""
    return Settings(
        staging_s3_endpoint_url=HttpUrl("http://127.0.0.1:9000"),
        staging_s3_bucket="test-bucket",
        staging_s3_region="US-EAST-VA",
        staging_s3_role=role,
        staging_s3_access_key_id=SecretStr("writer-key"),
        staging_s3_secret_access_key=SecretStr(_SECRET),
        staging_s3_addressing_style=S3AddressingStyle.PATH,
        staging_s3_verify_tls=False,
        staging_s3_content_prefix=prefix,
        staging_transfer_timeout_seconds=transfer_timeout,
    )


def _factory(client: FakeS3Client) -> S3ClientFactory:
    """Hand the adapter the fake wherever it would open a real client."""

    def _open() -> AbstractAsyncContextManager["S3Client"]:
        return cast("AbstractAsyncContextManager[S3Client]", client)

    return _open


class CountingFactory:
    """The same fake, counting how many clients the adapter asked for."""

    def __init__(self, client: FakeS3Client) -> None:
        self.client = client
        self.opened = 0

    def __call__(self) -> AbstractAsyncContextManager["S3Client"]:
        self.opened += 1
        return cast("AbstractAsyncContextManager[S3Client]", self.client)


def _store(
    client: FakeS3Client,
    *,
    role: StagingRole = StagingRole.WRITER,
    prefix: str = "content/",
    transfer_timeout: float = 300.0,
    client_factory: S3ClientFactory | None = None,
) -> S3StagingObjectStore:
    """The adapter under test, wired to `client`."""
    return S3StagingObjectStore(
        _settings(role=role, prefix=prefix, transfer_timeout=transfer_timeout),
        client_factory=client_factory or _factory(client),
        part_size=_PART_SIZE,
    )


def _target(size_bytes: int, *, sha256: str = "") -> StagedObject:
    """An upload target declaring `size_bytes`, and a digest only when known."""
    return StagedObject(
        key=_KEY,
        attempt_id=_ATTEMPT,
        size_bytes=size_bytes,
        sha256=sha256,
        created_at=_WHEN,
    )


def _registered(payload: bytes) -> StagedObject:
    """A registered object describing `payload` exactly, for download tests."""
    return _target(len(payload), sha256=hashlib.sha256(payload).hexdigest())


@pytest.fixture
def client() -> FakeS3Client:
    return FakeS3Client()


@pytest.fixture
def store(client: FakeS3Client) -> S3StagingObjectStore:
    return _store(client)


def test_the_adapter_satisfies_the_object_store_protocol(store: S3StagingObjectStore) -> None:
    """Coordination depends on the protocol, not on this class."""
    assert isinstance(store, StagingObjectStore)


async def test_an_empty_object_stages_as_a_single_put(
    store: S3StagingObjectStore, client: FakeS3Client
) -> None:
    """Zero-length files are ordinary content; the spec requires they work."""
    staged = await store.upload(_target(0), io.BytesIO(b""), declared_max_bytes=0)

    assert client.objects[_REMOTE_KEY] == b""
    assert staged.size_bytes == 0
    assert staged.sha256 == hashlib.sha256(b"").hexdigest()
    assert "create_multipart_upload" not in client.operations


async def test_a_small_object_avoids_multipart_entirely(
    store: S3StagingObjectStore, client: FakeS3Client
) -> None:
    """One request, one object: multipart machinery is not worth its abort surface."""
    await store.upload(_target(3), io.BytesIO(b"abc"), declared_max_bytes=16)

    assert client.operations == ["put_object", "head_object"]
    assert client.objects[_REMOTE_KEY] == b"abc"


async def test_a_large_object_streams_in_bounded_parts(
    store: S3StagingObjectStore, client: FakeS3Client
) -> None:
    """Parts are capped at the configured size, so memory never tracks object size."""
    payload = b"0123456789"

    staged = await store.upload(_target(len(payload)), io.BytesIO(payload), declared_max_bytes=16)

    assert client.parts == {}  # popped on completion
    assert client.objects[_REMOTE_KEY] == payload
    assert client.operations.count("upload_part") == 3
    assert staged.sha256 == hashlib.sha256(payload).hexdigest()


async def test_upload_requests_the_standard_storage_class_explicitly(
    store: S3StagingObjectStore, client: FakeS3Client
) -> None:
    """Neither backend's default may place staged media in a retention tier."""
    await store.upload(_target(3), io.BytesIO(b"abc"), declared_max_bytes=3)
    await store.upload(_target(10), io.BytesIO(b"0123456789"), declared_max_bytes=10)

    assert client.storage_classes[_REMOTE_KEY] == "STANDARD"


async def test_a_head_naming_standard_explicitly_is_also_accepted(
    store: S3StagingObjectStore, client: FakeS3Client
) -> None:
    """The two backends differ on whether HEAD echoes the class; both are correct."""
    client.head_storage_class = "STANDARD"

    staged = await store.upload(_target(3), io.BytesIO(b"abc"), declared_max_bytes=3)

    assert staged.created_at == _WHEN


async def test_an_unrequested_storage_class_fails_verification(
    store: S3StagingObjectStore, client: FakeS3Client
) -> None:
    """A tier with a minimum-duration charge must not pass as the one we asked for."""
    client.head_storage_class = "GLACIER"

    with pytest.raises(StagingObjectStoreError, match="GLACIER"):
        await store.upload(_target(3), io.BytesIO(b"abc"), declared_max_bytes=3)


async def test_a_multipart_etag_is_never_used_as_the_content_digest(
    store: S3StagingObjectStore, client: FakeS3Client
) -> None:
    """The ETag is a completion token; treating it as a hash is the ruled-out mistake."""
    payload = b"0123456789"
    client.part_etag = '"11111111111111111111111111111111-3"'

    staged = await store.upload(_target(len(payload)), io.BytesIO(payload), declared_max_bytes=16)

    assert staged.sha256 == hashlib.sha256(payload).hexdigest()
    assert staged.sha256 not in client.part_etag


async def test_a_stream_past_its_reservation_is_refused_before_any_request(
    client: FakeS3Client,
) -> None:
    """Excess bytes would consume capacity admission never reserved."""
    store = _store(client)

    with pytest.raises(StagingObjectStoreError, match="reservation"):
        await store.upload(_target(3), io.BytesIO(b"far too many bytes"), declared_max_bytes=3)

    assert client.operations == []


async def test_a_stream_outgrowing_its_reservation_mid_upload_aborts(
    store: S3StagingObjectStore, client: FakeS3Client
) -> None:
    """Detected one bounded block late at worst, and the parts are not left behind."""
    with pytest.raises(StagingObjectStoreError, match="reservation"):
        await store.upload(_target(10), io.BytesIO(b"0123456789abcd"), declared_max_bytes=10)

    assert client.aborted == ["upload-1"]
    assert _REMOTE_KEY not in client.objects


async def test_a_truncated_stream_is_refused_before_a_single_put(
    store: S3StagingObjectStore, client: FakeS3Client
) -> None:
    """A short source must not be published under a complete object's name."""
    with pytest.raises(StagingIntegrityError, match="declared 9 bytes"):
        await store.upload(_target(9), io.BytesIO(b"ab"), declared_max_bytes=16)

    assert client.operations == []


async def test_a_truncated_multipart_stream_aborts_instead_of_completing(
    store: S3StagingObjectStore, client: FakeS3Client
) -> None:
    """The completion is never issued, so no partial object becomes readable."""
    with pytest.raises(StagingIntegrityError, match="declared 12 bytes"):
        await store.upload(_target(12), io.BytesIO(b"0123456789"), declared_max_bytes=16)

    assert "complete_multipart_upload" not in client.operations
    assert client.aborted == ["upload-1"]


async def test_a_failed_completion_aborts_the_upload(
    store: S3StagingObjectStore, client: FakeS3Client
) -> None:
    """An upload that cannot complete leaves parts billing until they are aborted."""
    client.errors["complete_multipart_upload"] = _client_error(
        "InternalError", "CompleteMultipartUpload"
    )

    with pytest.raises(StagingObjectStoreError, match="completing the multipart upload"):
        await store.upload(_target(10), io.BytesIO(b"0123456789"), declared_max_bytes=16)

    assert client.aborted == ["upload-1"]


async def test_a_failed_abort_reports_the_residue_and_keeps_the_original_failure(
    store: S3StagingObjectStore, client: FakeS3Client
) -> None:
    """Both halves matter: one explains the upload, the other is what still bills."""
    client.errors["complete_multipart_upload"] = _client_error(
        "InternalError", "CompleteMultipartUpload"
    )
    client.errors["abort_multipart_upload"] = _client_error("AccessDenied", "AbortMultipartUpload")

    with pytest.raises(StagingObjectStoreError, match="could not be aborted") as failure:
        await store.upload(_target(10), io.BytesIO(b"0123456789"), declared_max_bytes=16)

    assert "upload-1" in str(failure.value)
    assert isinstance(failure.value.__cause__, StagingObjectStoreError)
    assert "completing the multipart upload" in str(failure.value.__cause__)


async def test_a_connection_failure_is_named_rather_than_leaking_the_sdk(
    store: S3StagingObjectStore, client: FakeS3Client
) -> None:
    """Callers depend on this boundary, not on botocore's exception hierarchy."""
    client.errors["put_object"] = EndpointConnectionError(endpoint_url="http://127.0.0.1:9000")

    with pytest.raises(StagingObjectStoreError, match="uploading"):
        await store.upload(_target(3), io.BytesIO(b"abc"), declared_max_bytes=3)


async def test_incorrect_remote_metadata_after_upload_blocks_registration(
    store: S3StagingObjectStore, client: FakeS3Client
) -> None:
    """Verification is what stands between a completed upload and a published one."""
    client.head_length = 2

    with pytest.raises(StagingIntegrityError, match="not registerable"):
        await store.upload(_target(3), io.BytesIO(b"abc"), declared_max_bytes=3)


async def test_a_declared_digest_that_disagrees_fails_integrity(
    store: S3StagingObjectStore,
) -> None:
    """A caller that already knows the digest has it checked against the stream."""
    with pytest.raises(StagingIntegrityError, match="declared as"):
        await store.upload(_target(3, sha256="b" * 64), io.BytesIO(b"abc"), declared_max_bytes=3)


async def test_a_declaration_larger_than_the_reservation_is_refused(
    store: S3StagingObjectStore, client: FakeS3Client
) -> None:
    """Admission reserved less than the upload needs; reading it anyway overruns."""
    with pytest.raises(StagingObjectStoreError, match="less capacity"):
        await store.upload(_target(9), io.BytesIO(b"012345678"), declared_max_bytes=4)

    assert client.operations == []


async def test_a_negative_reservation_is_a_programming_error(
    store: S3StagingObjectStore,
) -> None:
    """Nonsense bounds fail at the call, not as a confusing store failure."""
    with pytest.raises(ValueError, match="nonnegative"):
        await store.upload(_target(0), io.BytesIO(b""), declared_max_bytes=-1)


async def test_download_streams_in_bounded_chunks_and_verifies(
    store: S3StagingObjectStore, client: FakeS3Client
) -> None:
    """The reader's memory is bounded by the part size, never by the object."""
    payload = b"0123456789"
    client.objects[_REMOTE_KEY] = payload
    destination = io.BytesIO()

    await store.download(_registered(payload), destination)

    assert destination.getvalue() == payload
    assert client.bodies[0].largest_request == _PART_SIZE


async def test_a_reader_may_download_without_write_authority(client: FakeS3Client) -> None:
    """The decoding profile's whole grant is GetObject on the content prefix."""
    payload = b"decodable"
    client.objects[_REMOTE_KEY] = payload
    reader = _store(client, role=StagingRole.READER)
    destination = io.BytesIO()

    await reader.download(_registered(payload), destination)

    assert destination.getvalue() == payload


async def test_a_missing_object_invalidates_its_registration(
    store: S3StagingObjectStore,
) -> None:
    """A stale registration is a recoverable cache miss, not a transport failure."""
    with pytest.raises(StagedEntryMissingError, match="found nothing at that key"):
        await store.download(_registered(b"gone"), io.BytesIO())


async def test_a_denial_stays_distinguishable_from_a_missing_object(
    store: S3StagingObjectStore, client: FakeS3Client
) -> None:
    """Reading a denial as absence turns a bad policy into an endless re-staging loop."""
    client.errors["get_object"] = _client_error("AccessDenied", "GetObject")

    with pytest.raises(StagingAccessDeniedError, match="denied"):
        await store.download(_registered(b"denied"), io.BytesIO())


async def test_a_short_download_fails_rather_than_returning_partial_bytes(
    store: S3StagingObjectStore, client: FakeS3Client
) -> None:
    """The registration describes the object; fewer bytes means it no longer holds."""
    client.objects[_REMOTE_KEY] = b"01234"

    with pytest.raises(StagingIntegrityError, match="registered at 10 bytes"):
        await store.download(_registered(b"0123456789"), io.BytesIO())


async def test_download_rejects_bytes_that_differ_from_the_registered_digest(
    store: S3StagingObjectStore, client: FakeS3Client
) -> None:
    """Same length, different content: only the digest catches this."""
    client.objects[_REMOTE_KEY] = b"9876543210"

    with pytest.raises(StagingIntegrityError, match="hashes to"):
        await store.download(_registered(b"0123456789"), io.BytesIO())


async def test_deleting_an_absent_object_succeeds(
    store: S3StagingObjectStore, client: FakeS3Client
) -> None:
    """Release is retried after partial cleanup, so it must be safe to repeat."""
    client.errors["delete_object"] = _client_error("NoSuchKey", "DeleteObject")

    await store.delete(_OBJECT_KEY)

    assert client.operations == ["delete_object"]


async def test_delete_surfaces_a_denial(store: S3StagingObjectStore, client: FakeS3Client) -> None:
    """A policy that cannot delete leaves billed residue; it must not look like success."""
    client.errors["delete_object"] = _client_error("AccessDenied", "DeleteObject")

    with pytest.raises(StagingAccessDeniedError):
        await store.delete(_OBJECT_KEY)


async def test_listing_pages_and_returns_prefix_relative_keys(
    store: S3StagingObjectStore, client: FakeS3Client
) -> None:
    """A truncated listing would understate occupancy and admit work that does not fit."""
    client.pages["list_objects_v2"] = [
        {"Contents": [{"Key": "content/", "Size": 0, "LastModified": _WHEN}]},
        {"Contents": [{"Key": f"{_REMOTE_KEY}", "Size": 7, "LastModified": _WHEN}]},
        {"Contents": [{"Key": "content/other", "Size": 3, "LastModified": _WHEN}]},
    ]

    listed = [ref async for ref in store.list_objects()]

    assert [(ref.object_key, ref.size_bytes) for ref in listed] == [(_OBJECT_KEY, 7), ("other", 3)]
    assert listed[0].last_modified == _WHEN


async def test_a_key_listed_twice_is_counted_once(
    store: S3StagingObjectStore, client: FakeS3Client
) -> None:
    """A repeated listing entry would double-count occupancy; RustFS repeats the marker."""
    client.pages["list_objects_v2"] = [
        {
            "Contents": [
                {"Key": "content/", "Size": 0, "LastModified": _WHEN},
                {"Key": "content/", "Size": 0, "LastModified": _WHEN},
                {"Key": _REMOTE_KEY, "Size": 7, "LastModified": _WHEN},
            ]
        },
        {"Contents": [{"Key": _REMOTE_KEY, "Size": 7, "LastModified": _WHEN}]},
    ]

    listed = [ref async for ref in store.list_objects()]

    assert [(ref.object_key, ref.size_bytes) for ref in listed] == [(_OBJECT_KEY, 7)]


async def test_a_listing_entry_without_a_size_fails_rather_than_understating_occupancy(
    store: S3StagingObjectStore, client: FakeS3Client
) -> None:
    """Skipping an unaccountable object would report capacity that does not exist."""
    client.pages["list_objects_v2"] = [{"Contents": [{"Key": _REMOTE_KEY}]}]

    with pytest.raises(StagingObjectStoreError, match="cannot be accounted for"):
        [ref async for ref in store.list_objects()]


async def test_a_failed_listing_is_reported_rather_than_returned_empty(
    store: S3StagingObjectStore, client: FakeS3Client
) -> None:
    """An empty result and an unreadable bucket must never look the same."""
    client.page_errors["list_objects_v2"] = _client_error("InternalError", "ListObjectsV2")

    with pytest.raises(StagingObjectStoreError, match="listing staged objects"):
        [ref async for ref in store.list_objects()]


async def test_only_uploads_older_than_the_cutoff_are_aborted(
    store: S3StagingObjectStore, client: FakeS3Client
) -> None:
    """An upload still in flight elsewhere must survive another worker's cleanup."""
    client.pages["list_multipart_uploads"] = [
        {
            "Uploads": [
                {"Key": _REMOTE_KEY, "UploadId": "stale", "Initiated": _WHEN - timedelta(days=1)},
                {"Key": _REMOTE_KEY, "UploadId": "live", "Initiated": _WHEN},
            ]
        }
    ]

    aborted = await store.abort_incomplete_uploads(_WHEN - timedelta(hours=1))

    assert aborted == 1
    assert client.aborted == ["stale"]


async def test_an_unabortable_listed_upload_fails_rather_than_billing_silently(
    store: S3StagingObjectStore, client: FakeS3Client
) -> None:
    """An entry without an identifier is residue nobody can reclaim; say so."""
    client.pages["list_multipart_uploads"] = [{"Uploads": [{"Key": _REMOTE_KEY}]}]

    with pytest.raises(StagingObjectStoreError, match="cannot be aborted from this response"):
        await store.abort_incomplete_uploads(_WHEN)


async def test_a_naive_cutoff_is_refused(store: S3StagingObjectStore) -> None:
    """A naive cutoff cannot be ordered against the SDK's aware timestamps."""
    with pytest.raises(ValueError, match="timezone-aware"):
        await store.abort_incomplete_uploads(datetime(2026, 9, 12, 12, 0))  # noqa: DTZ001


async def test_a_reader_refuses_every_write_operation(client: FakeS3Client) -> None:
    """A CPU worker handed writer credentials still refuses to use them."""
    reader = _store(client, role=StagingRole.READER)

    with pytest.raises(StagingAccessDeniedError, match="may not upload"):
        await reader.upload(_target(0), io.BytesIO(b""), declared_max_bytes=0)
    with pytest.raises(StagingAccessDeniedError, match="may not delete"):
        await reader.delete(_OBJECT_KEY)
    with pytest.raises(StagingAccessDeniedError, match="may not list"):
        reader.list_objects()
    with pytest.raises(StagingAccessDeniedError, match="may not abort"):
        await reader.abort_incomplete_uploads(_WHEN)

    assert client.operations == []


async def test_an_alternate_content_prefix_is_applied_to_every_key(
    client: FakeS3Client,
) -> None:
    """The reader's grant is scoped to this prefix, so the adapter must honour it."""
    store = _store(client, prefix="media/")

    await store.upload(_target(3), io.BytesIO(b"abc"), declared_max_bytes=3)

    assert f"media/{_OBJECT_KEY}" in client.objects


def test_an_unconfigured_endpoint_fails_explicitly() -> None:
    """Staging must not guess an endpoint or fall back to another backend."""
    with pytest.raises(StagingObjectStoreError, match="no configured endpoint"):
        S3StagingObjectStore(Settings())


def test_a_nonpositive_part_size_is_refused() -> None:
    """A zero part size would loop forever rather than bounding anything."""
    with pytest.raises(ValueError, match="part_size must be positive"):
        S3StagingObjectStore(_settings(), client_factory=_factory(FakeS3Client()), part_size=0)


def test_the_client_is_built_from_configuration_not_an_ambient_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The SDK's own chain would pick up an instance role or a developer's ~/.aws."""
    recorded: dict[str, object] = {}

    class RecordingSession:
        def __init__(self, **credentials: str) -> None:
            recorded.update(credentials)

        def client(self, service_name: str, **options: object) -> object:
            recorded["service_name"] = service_name
            recorded.update(options)
            return object()

    monkeypatch.setattr(aioboto3, "Session", RecordingSession)

    build_client_factory(_settings())()

    assert recorded["aws_access_key_id"] == "writer-key"
    assert recorded["aws_secret_access_key"] == _SECRET
    assert recorded["region_name"] == "US-EAST-VA"
    assert recorded["service_name"] == "s3"
    assert recorded["endpoint_url"] == "http://127.0.0.1:9000/"
    assert recorded["verify"] is False


def test_the_store_retains_no_credential_anything_can_render() -> None:
    """Secrets reach the SDK session once and are never held where a log could see them."""
    store = S3StagingObjectStore(_settings())

    assert _SECRET not in repr(store)
    assert _SECRET not in str(vars(store))


def test_a_factory_without_credentials_is_refused() -> None:
    """The validator normally catches this; the SDK must never get a second chance to."""
    with pytest.raises(StagingObjectStoreError, match="ambient credential fallback"):
        build_client_factory(Settings())


async def test_a_vanished_bucket_is_not_reported_as_a_deleted_object(
    store: S3StagingObjectStore, client: FakeS3Client
) -> None:
    """Swallowing it would hand eviction freed bytes nothing ever freed."""
    client.errors["delete_object"] = _client_error("NoSuchBucket", "DeleteObject")

    with pytest.raises(StagingBucketMissingError, match="does not exist at this endpoint"):
        await store.delete(_OBJECT_KEY)


async def test_a_vanished_bucket_never_reads_as_a_missing_entry(
    store: S3StagingObjectStore, client: FakeS3Client
) -> None:
    """The smoke check reads a missing entry as "the reader wrote nothing"; it must not."""
    client.errors["get_object"] = _client_error("NoSuchBucket", "GetObject")

    with pytest.raises(StagingBucketMissingError) as failure:
        await store.download(_registered(b"gone"), io.BytesIO())

    assert not isinstance(failure.value, StagedEntryMissingError)


async def test_a_throttle_is_named_as_retryable_rather_than_flattened(
    store: S3StagingObjectStore, client: FakeS3Client
) -> None:
    """A caller cannot back off on a SlowDown it cannot tell from a bad signature."""
    client.errors["get_object"] = _client_error("SlowDown", "GetObject")

    with pytest.raises(StagingTransientError, match="later attempt"):
        await store.download(_registered(b"throttled"), io.BytesIO())


async def test_an_expired_token_is_a_denial_rather_than_a_transient_failure(
    store: S3StagingObjectStore, client: FakeS3Client
) -> None:
    """Retrying an aged-out credential against the same token cannot resolve it."""
    client.errors["get_object"] = _client_error("ExpiredToken", "GetObject")

    with pytest.raises(StagingAccessDeniedError, match="ExpiredToken"):
        await store.download(_registered(b"expired"), io.BytesIO())


async def test_an_upload_a_peer_already_reclaimed_does_not_end_the_sweep(
    store: S3StagingObjectStore, client: FakeS3Client
) -> None:
    """Two pods on one interval would otherwise abandon their listing every tick."""
    client.missing_uploads = {"gone"}
    client.pages["list_multipart_uploads"] = [
        {
            "Uploads": [
                {"Key": _REMOTE_KEY, "UploadId": "gone", "Initiated": _WHEN - timedelta(days=1)},
                {"Key": _REMOTE_KEY, "UploadId": "stale", "Initiated": _WHEN - timedelta(days=1)},
            ]
        }
    ]

    aborted = await store.abort_incomplete_uploads(_WHEN - timedelta(hours=1))

    assert aborted == 1
    assert client.aborted == ["stale"]


async def test_a_large_reservation_grows_the_part_size_to_stay_under_the_ceiling(
    client: FakeS3Client,
) -> None:
    """S3 refuses part 10,001, which arrives only after the whole object is transferred."""
    store = _store(client)
    payload = b"x" * 100

    await store.upload(_target(len(payload)), io.BytesIO(payload), declared_max_bytes=200_000)

    # 200_000 / 10_000 = 20 bytes a part, rather than the configured 4.
    assert client.operations.count("upload_part") == 5
    assert client.objects[_REMOTE_KEY] == payload


async def test_a_reservation_no_multipart_upload_can_carry_is_refused_up_front(
    client: FakeS3Client,
) -> None:
    """Refused before a byte is sent, not after 10,000 maximum-sized parts."""
    store = _store(client)

    with pytest.raises(StagingObjectStoreError, match="fall short"):
        await store.upload(
            _target(0), io.BytesIO(b""), declared_max_bytes=_MAX_PARTS * _MAX_PART_SIZE + 1
        )

    assert client.operations == []


async def test_completion_names_every_part_in_ascending_order(
    store: S3StagingObjectStore, client: FakeS3Client
) -> None:
    """The fake assembles from the numbers the completion names, so ordering is checked."""
    payload = b"0123456789"

    await store.upload(_target(len(payload)), io.BytesIO(payload), declared_max_bytes=16)

    assert client.completed == [(number, client.part_etag) for number in (1, 2, 3)]
    assert client.objects[_REMOTE_KEY] == payload


async def test_a_failed_create_reports_itself_without_a_phantom_abort(
    store: S3StagingObjectStore, client: FakeS3Client
) -> None:
    """There is no upload to abort yet, and inventing an identifier would be worse."""
    client.errors["create_multipart_upload"] = _client_error(
        "InternalError", "CreateMultipartUpload"
    )

    with pytest.raises(StagingObjectStoreError, match="creating the multipart upload"):
        await store.upload(_target(10), io.BytesIO(b"0123456789"), declared_max_bytes=16)

    assert client.aborted == []


async def test_an_abort_failing_outside_the_sdk_still_reports_the_residue(
    store: S3StagingObjectStore, client: FakeS3Client
) -> None:
    """A bare socket error must not erase the upload failure and the billed parts."""
    client.errors["complete_multipart_upload"] = _client_error(
        "InternalError", "CompleteMultipartUpload"
    )
    client.errors["abort_multipart_upload"] = OSError("connection reset by peer")

    with pytest.raises(StagingObjectStoreError, match="could not be aborted") as failure:
        await store.upload(_target(10), io.BytesIO(b"0123456789"), declared_max_bytes=16)

    assert "upload-1" in str(failure.value)
    assert isinstance(failure.value.__cause__, StagingTransientError)


async def test_a_second_cancellation_does_not_abandon_the_multipart_abort(
    client: FakeS3Client,
) -> None:
    """A cancelled worker still owes the abort; dropping it leaves parts billing."""
    store = _store(client)
    client.stalls["upload_part"] = 60.0
    client.stalls["abort_multipart_upload"] = 0.0
    task = asyncio.create_task(
        store.upload(_target(10), io.BytesIO(b"0123456789"), declared_max_bytes=16)
    )
    # The second cancellation lands while the abort is already in flight.
    client.on_abort = task.cancel
    for _ in range(10):
        await asyncio.sleep(0)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert client.aborted == ["upload-1"]


async def test_an_upload_past_its_ceiling_is_abandoned_rather_than_held_open(
    client: FakeS3Client,
) -> None:
    """Per-socket timeouts never fire on a trickle; the ceiling is per transfer."""
    store = _store(client, transfer_timeout=0.01)
    client.stalls["put_object"] = 30.0

    with capture_logs() as events, pytest.raises(StagingTransientError, match="ceiling"):
        await store.upload(_target(3), io.BytesIO(b"abc"), declared_max_bytes=3)

    assert events == [
        {
            "event": "staging.transfer_deadline_hit",
            "log_level": "warning",
            "transfer": f"uploading {_OBJECT_KEY}",
            "deadline_seconds": 0.01,
        }
    ]


async def test_a_download_past_its_ceiling_is_abandoned_rather_than_held_open(
    client: FakeS3Client,
) -> None:
    """The same ceiling on the read side, where a stalled reader holds its scratch."""
    store = _store(client, transfer_timeout=0.01)
    client.objects[_REMOTE_KEY] = b"0123456789"
    client.stalls["get_object"] = 30.0

    with capture_logs() as events, pytest.raises(StagingTransientError, match="ceiling"):
        await store.download(_registered(b"0123456789"), io.BytesIO())

    assert events == [
        {
            "event": "staging.transfer_deadline_hit",
            "log_level": "warning",
            "transfer": f"downloading {_OBJECT_KEY}",
            "deadline_seconds": 0.01,
        }
    ]


async def test_a_deadline_hit_is_reported_when_the_multipart_abort_also_fails(
    client: FakeS3Client,
) -> None:
    """The abort's failure replaces the timeout, but the deadline still expired."""
    store = _store(client, transfer_timeout=0.01)
    client.stalls["upload_part"] = 30.0
    client.errors["abort_multipart_upload"] = _client_error(
        "ServiceUnavailable", "AbortMultipartUpload"
    )

    with capture_logs() as events, pytest.raises(StagingObjectStoreError) as failure:
        await store.upload(_target(10), io.BytesIO(b"0123456789"), declared_max_bytes=16)

    assert "could not be aborted" in str(failure.value)
    assert isinstance(failure.value.__cause__, asyncio.CancelledError)
    assert [e for e in events if e["event"] == "staging.transfer_deadline_hit"] == [
        {
            "event": "staging.transfer_deadline_hit",
            "log_level": "warning",
            "transfer": f"uploading {_OBJECT_KEY}",
            "deadline_seconds": 0.01,
        }
    ]


async def test_an_external_cancellation_is_not_a_deadline_hit(client: FakeS3Client) -> None:
    """Only the transfer's own ceiling counts; a cancelled worker is not a timeout."""
    store = _store(client)
    client.stalls["upload_part"] = 60.0
    client.stalls["abort_multipart_upload"] = 0.0

    with capture_logs() as events:
        task = asyncio.create_task(
            store.upload(_target(10), io.BytesIO(b"0123456789"), declared_max_bytes=16)
        )
        for _ in range(10):
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert client.aborted == ["upload-1"]
    assert [e for e in events if e["event"] == "staging.transfer_deadline_hit"] == []


async def test_a_transfer_inside_its_ceiling_reports_no_deadline_hit(
    store: S3StagingObjectStore,
) -> None:
    """Only an expiry is a deadline hit; a completed transfer logs nothing here."""
    with capture_logs() as events:
        await store.upload(_target(3), io.BytesIO(b"abc"), declared_max_bytes=3)

    assert [e for e in events if e["event"] == "staging.transfer_deadline_hit"] == []


async def test_download_stops_once_the_object_outgrows_its_registration(
    store: S3StagingObjectStore, client: FakeS3Client
) -> None:
    """The reader's scratch is reserved from the registration; drift must not fill it."""
    client.objects[_REMOTE_KEY] = b"x" * 64
    destination = io.BytesIO()

    with pytest.raises(StagingIntegrityError, match="still delivering past them"):
        await store.download(_registered(b"x" * 8), destination)

    assert len(destination.getvalue()) == 8
    assert client.bodies[0].closed


async def test_the_response_body_is_closed_after_a_download(
    store: S3StagingObjectStore, client: FakeS3Client
) -> None:
    """Matches the readiness read, and keeps a cancelled read out of the warning log."""
    payload = b"0123456789"
    client.objects[_REMOTE_KEY] = payload

    await store.download(_registered(payload), io.BytesIO())

    assert client.bodies[0].closed


async def test_a_batch_opens_one_client_for_the_deletes_inside_it(
    client: FakeS3Client,
) -> None:
    """A client per delete is a connect, a handshake and a CA-bundle read, under the lock."""
    factory = CountingFactory(client)
    store = _store(client, client_factory=factory)

    async with store.batch():
        for index in range(5):
            await store.delete(f"{_KEY}/{index:032x}")

    assert factory.opened == 1
    assert client.operations.count("delete_object") == 5


async def test_a_transfer_inside_a_batch_still_opens_its_own_client(
    client: FakeS3Client,
) -> None:
    """A pinned connection must not outlive the batch by riding a transfer."""
    factory = CountingFactory(client)
    store = _store(client, client_factory=factory)

    async with store.batch():
        await store.upload(_target(3), io.BytesIO(b"abc"), declared_max_bytes=3)

    assert factory.opened == 2


async def test_outside_a_batch_each_operation_opens_its_own_client(
    client: FakeS3Client,
) -> None:
    """The default stays per operation; batching is something a caller asks for."""
    factory = CountingFactory(client)
    store = _store(client, client_factory=factory)

    await store.delete(_OBJECT_KEY)
    await store.delete(_OBJECT_KEY)

    assert factory.opened == 2


class UnopenableClient:
    """A client whose construction fails, as it does on a malformed endpoint or region."""

    async def __aenter__(self) -> "S3Client":
        raise NoRegionError()

    async def __aexit__(self, *exc_info: object) -> None:
        return None


def _unopenable() -> AbstractAsyncContextManager["S3Client"]:
    return cast("AbstractAsyncContextManager[S3Client]", UnopenableClient())


async def test_a_batch_whose_client_cannot_open_fails_at_this_boundary(
    client: FakeS3Client,
) -> None:
    """Callers catch the staging boundary, not botocore's, including on connect."""
    store = _store(client, client_factory=_unopenable)

    with pytest.raises(StagingObjectStoreError, match="opening the staging client"):
        async with store.batch():
            pass


async def test_a_transfer_whose_client_cannot_open_fails_at_this_boundary(
    client: FakeS3Client,
) -> None:
    store = _store(client, client_factory=_unopenable)

    with pytest.raises(StagingObjectStoreError, match="opening the staging client"):
        await store.download(_registered(b"abc"), io.BytesIO())


def test_the_retry_policy_is_stated_rather_than_inherited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """botocore's default is `legacy`; a bucket that answers SlowDown wants `standard`."""
    recorded: dict[str, object] = {}

    class RecordingSession:
        def __init__(self, **credentials: str) -> None:
            return None

        def client(self, service_name: str, **options: object) -> object:
            recorded.update(options)
            return object()

    monkeypatch.setattr(aioboto3, "Session", RecordingSession)

    build_client_factory(_settings())()

    config = recorded["config"]
    assert isinstance(config, AioConfig)
    # botocore sets each option as an attribute from its defaults table, so the stubs
    # do not declare `retries` even though every Config carries it.
    assert getattr(config, "retries", None) == {"mode": "standard", "max_attempts": 5}


async def test_an_unclassified_error_is_still_named_at_this_boundary(
    store: S3StagingObjectStore, client: FakeS3Client
) -> None:
    """Callers depend on this boundary; an unfamiliar code must not leak botocore's."""
    client.errors["get_object"] = _client_error("MalformedXML", "GetObject")

    with pytest.raises(StagingObjectStoreError, match="MalformedXML") as failure:
        await store.download(_registered(b"odd"), io.BytesIO())

    assert not isinstance(failure.value, StagingTransientError)
