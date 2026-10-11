"""Exercise the application S3 contract on isolated local-engine and disposable OVH prefixes.

The default run includes RustFS and OVH. Missing remote credentials fail with PENDING,
so a local pass cannot be mistaken for remote verification. Select --staging-backend=rustfs
for local CI; that selection supplies no OVH acceptance evidence. An OVH run requires
CURATE_D_TEST_OVH_DISPOSABLE=true and explicit endpoint, bucket, two scoped S3
identities and a third, explicitly denied identity; management API credentials cannot
substitute for those identities.
"""

import asyncio
import hashlib
import io
import json
import os
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast
from uuid import uuid4

import pytest
from botocore.exceptions import EndpointConnectionError
from pydantic import HttpUrl, SecretStr

from curate_d.config import S3AddressingStyle, Settings, StagingRole
from curate_d.staging.errors import (
    StagedEntryMissingError,
    StagingAccessDeniedError,
    StagingIntegrityError,
    StagingObjectStoreError,
)
from curate_d.staging.keys import StagingKey
from curate_d.staging.object_store import S3StagingObjectStore, build_client_factory
from curate_d.staging.objects import StagedObject, StagingObjectStore

if TYPE_CHECKING:
    from botocore.awsrequest import AWSPreparedRequest
    from types_aiobotocore_s3.client import S3Client

pytestmark = pytest.mark.integration
_PART_SIZE = 5 * 1024 * 1024
# The loopback ports compose.yaml publishes for each local engine.
_LOCAL_ENDPOINTS = {"rustfs": "http://127.0.0.1:9100"}


@dataclass
class WireEvidence:
    """Retain only safe wire fields needed to prove the SDK behavior under test."""

    checksum_headers: list[dict[str, str]] = field(default_factory=list)
    operations: list[str] = field(default_factory=list)
    multipart_etags: list[str] = field(default_factory=list)
    multipart_keys: set[str] = field(default_factory=set)
    continuation_tokens: list[str] = field(default_factory=list)
    abort_failures_remaining: int = 0
    put_failures_remaining: int = 0

    def capture_put(self, request: "AWSPreparedRequest", **_kwargs: object) -> None:
        """Capture checksum headers without retaining authorization or signatures."""
        allowed = {
            "x-amz-checksum-crc32",
            "x-amz-sdk-checksum-algorithm",
            "x-amz-content-sha256",
            "x-amz-trailer",
            "content-encoding",
        }
        headers = cast(Mapping[str, str | bytes], request.headers)
        self.checksum_headers.append(
            {
                key.lower(): value.decode() if isinstance(value, bytes) else value
                for key, value in headers.items()
                if key.lower() in allowed
            }
        )
        if self.put_failures_remaining:
            self.put_failures_remaining -= 1
            raise EndpointConnectionError(endpoint_url="injected-put-connection-failure")

    def capture_operation(self, event_name: str, **_kwargs: object) -> None:
        """Record operations so multipart tests prove they reached the real SDK."""
        operation = event_name.rsplit(".", 1)[-1]
        self.operations.append(operation)
        if operation == "AbortMultipartUpload" and self.abort_failures_remaining:
            self.abort_failures_remaining -= 1
            raise EndpointConnectionError(endpoint_url="injected-abort-connection-failure")

    def capture_created(self, parsed: dict[str, object], **_kwargs: object) -> None:
        """Track exact upload keys: some engines list uploads only by exact-key prefix."""
        key = parsed.get("Key")
        if isinstance(key, str):
            self.multipart_keys.add(key)

    def capture_completion(self, parsed: dict[str, object], **_kwargs: object) -> None:
        """Retain the completion ETag as an opaque token rather than an integrity hash."""
        etag = parsed.get("ETag")
        if isinstance(etag, str):
            self.multipart_etags.append(etag.strip('"'))

    def small_pages(self, params: dict[str, object], **_kwargs: object) -> None:
        """Force real continuation requests without staging a thousand large fixtures."""
        params["MaxKeys"] = 1
        token = params.get("ContinuationToken")
        if isinstance(token, str):
            self.continuation_tokens.append(token)


@dataclass
class StagingContract:
    """Hold role-specific adapters and wire observations for one isolated test prefix."""

    writer: StagingObjectStore
    reader: StagingObjectStore
    denied_reader: StagingObjectStore
    evidence: WireEvidence
    writer_settings: Settings
    reader_settings: Settings


def _settings(backend: str, role: StagingRole, prefix: str) -> Settings:
    """Resolve explicit backend credentials and refuse non-disposable remote fixtures."""
    env = f"CURATE_D_TEST_{backend.upper()}_"
    defaults = {
        "ENDPOINT": _LOCAL_ENDPOINTS.get(backend, ""),
        "BUCKET": "curate-d-staging",
        "REGION": "us-east-1",
        "ADDRESSING_STYLE": "path",
        "WRITER_ACCESS_KEY_ID": "curate-d-staging-writer",
        "WRITER_SECRET_ACCESS_KEY": "writer-development-secret",
        "READER_ACCESS_KEY_ID": "curate-d-staging-reader",
        "READER_SECRET_ACCESS_KEY": "reader-development-secret",
    }
    if backend == "ovh":
        required = (
            "ENDPOINT",
            "BUCKET",
            "WRITER_ACCESS_KEY_ID",
            "WRITER_SECRET_ACCESS_KEY",
            "READER_ACCESS_KEY_ID",
            "READER_SECRET_ACCESS_KEY",
        )
        missing = [f"{env}{key}" for key in required if not os.environ.get(f"{env}{key}")]
        if missing or os.environ.get(f"{env}DISPOSABLE") != "true":
            pytest.fail(
                "PENDING OVH verification: configure a disposable staging bucket with "
                "both scoped S3 identities and CURATE_D_TEST_OVH_DISPOSABLE=true. "
                "Missing settings: " + ", ".join(missing),
                pytrace=False,
            )
        defaults = {"REGION": "us-east-va", "ADDRESSING_STYLE": "virtual"}
    values = {key: os.environ.get(f"{env}{key}", value) for key, value in defaults.items()}
    endpoint = os.environ.get(f"{env}ENDPOINT", values.get("ENDPOINT", ""))
    if backend == "ovh" and not endpoint.startswith("https://"):
        pytest.fail("OVH contract fixtures require certificate-verified HTTPS", pytrace=False)
    return Settings(
        staging_s3_endpoint_url=HttpUrl(endpoint),
        staging_s3_bucket=os.environ.get(f"{env}BUCKET", values.get("BUCKET", "")),
        staging_s3_region=values["REGION"],
        staging_s3_role=role,
        staging_s3_access_key_id=SecretStr(
            os.environ.get(
                f"{env}{role.value.upper()}_ACCESS_KEY_ID",
                values.get(f"{role.value.upper()}_ACCESS_KEY_ID", ""),
            )
        ),
        staging_s3_secret_access_key=SecretStr(
            os.environ.get(
                f"{env}{role.value.upper()}_SECRET_ACCESS_KEY",
                values.get(f"{role.value.upper()}_SECRET_ACCESS_KEY", ""),
            )
        ),
        staging_s3_addressing_style=S3AddressingStyle(values["ADDRESSING_STYLE"]),
        staging_s3_verify_tls=endpoint.startswith("https://"),
        staging_s3_content_prefix=prefix,
    )


def _denied_settings(backend: str, reader_settings: Settings) -> Settings:
    """Resolve an identity the backend must refuse, to pin the denial half of row 21.

    The local engine's reader holds no ListBucket, so re-pointing it outside its grant is
    denied.
    On OVH, provider-specific listing behaviour turns that probe into a 404, so OVH uses
    a separate identity denied by policy.
    """
    if backend != "ovh":
        return reader_settings.model_copy(
            update={"staging_s3_content_prefix": f"outside-{uuid4().hex}/"}
        )
    env = "CURATE_D_TEST_OVH_DENIED_"
    required = ("ACCESS_KEY_ID", "SECRET_ACCESS_KEY")
    missing = [f"{env}{key}" for key in required if not os.environ.get(f"{env}{key}")]
    if missing:
        pytest.fail(
            "PENDING OVH verification: configure the explicitly denied S3 identity. "
            "Missing settings: " + ", ".join(missing),
            pytrace=False,
        )
    return reader_settings.model_copy(
        update={
            "staging_s3_access_key_id": SecretStr(os.environ[f"{env}ACCESS_KEY_ID"]),
            "staging_s3_secret_access_key": SecretStr(os.environ[f"{env}SECRET_ACCESS_KEY"]),
        }
    )


@asynccontextmanager
async def _isolated_contract(staging_backend_name: str) -> AsyncGenerator[StagingContract, None]:
    """Clean completed objects and multipart parts after success, failure or cancellation."""
    root = os.environ.get(f"CURATE_D_TEST_{staging_backend_name.upper()}_PREFIX", "content/")
    prefix = f"{root}contract-{uuid4().hex}/"
    writer_settings = _settings(staging_backend_name, StagingRole.WRITER, prefix)
    reader_settings = _settings(staging_backend_name, StagingRole.READER, prefix)
    evidence = WireEvidence()
    open_client = build_client_factory(writer_settings)

    @asynccontextmanager
    async def observed_client() -> AsyncGenerator["S3Client", None]:
        """Observe the application's SDK requests while retaining only safe fields."""
        async with open_client() as client:
            client.meta.events.register("before-send.s3.PutObject", evidence.capture_put)
            client.meta.events.register("before-call.s3.*", evidence.capture_operation)
            client.meta.events.register(
                "after-call.s3.CompleteMultipartUpload", evidence.capture_completion
            )
            client.meta.events.register(
                "after-call.s3.CreateMultipartUpload", evidence.capture_created
            )
            client.meta.events.register(
                "before-parameter-build.s3.ListObjectsV2", evidence.small_pages
            )
            yield client

    writer = S3StagingObjectStore(
        writer_settings, client_factory=observed_client, part_size=_PART_SIZE
    )
    reader = S3StagingObjectStore(reader_settings, part_size=_PART_SIZE)
    denied_reader = S3StagingObjectStore(_denied_settings(staging_backend_name, reader_settings))
    try:
        yield StagingContract(
            writer, reader, denied_reader, evidence, writer_settings, reader_settings
        )
    finally:
        # The per-test prefix contains no other worker's objects, including on OVH.
        # Always attempt both cleanup categories even if one fails.
        try:
            # Some engines (MinIO, the previous local engine) only find incomplete uploads
            # when Prefix is the exact key, so cleanup does not rely on a prefix scan.
            # These keys came from this fixture's own adapter calls, not a bucket scan.
            for key in evidence.multipart_keys:
                exact = S3StagingObjectStore(
                    writer_settings.model_copy(update={"staging_s3_content_prefix": key}),
                    client_factory=observed_client,
                )
                await exact.abort_incomplete_uploads(datetime.now(UTC) + timedelta(minutes=1))
                assert (
                    await exact.abort_incomplete_uploads(datetime.now(UTC) + timedelta(minutes=1))
                    == 0
                )
        finally:
            objects = [obj async for obj in writer.list_objects()]
            results = await asyncio.gather(
                *(writer.delete(obj.object_key) for obj in objects), return_exceptions=True
            )
            for result in results:
                if isinstance(result, BaseException):
                    raise result
        assert [obj async for obj in writer.list_objects()] == []
        assert await writer.abort_incomplete_uploads(datetime.now(UTC) + timedelta(minutes=1)) == 0


@pytest.fixture
async def staging_contract(staging_backend_name: str) -> AsyncIterator[StagingContract]:
    """Yield a backend adapter whose isolated prefix is reclaimed on every exit path."""
    async with _isolated_contract(staging_backend_name) as contract:
        yield contract


def _target(payload: bytes, *, key: StagingKey | None = None) -> StagedObject:
    """Create an immutable upload attempt with an independently computed expected digest."""
    return StagedObject(
        key=key or StagingKey(uuid4().hex),
        attempt_id=uuid4(),
        size_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        created_at=datetime.now(UTC),
    )


async def _upload(contract: StagingContract, payload: bytes) -> StagedObject:
    """Upload known content through the same protocol the application uses."""
    return await contract.writer.upload(
        _target(payload), io.BytesIO(payload), declared_max_bytes=len(payload)
    )


async def _read(contract: StagingContract, obj: StagedObject) -> bytes:
    """Read and verify the entire object with the scoped CPU identity."""
    destination = io.BytesIO()
    await contract.reader.download(obj, destination)
    return destination.getvalue()


async def test_writer_round_trips_content(staging_contract: StagingContract) -> None:
    """Verify single PUT, metadata and reader SHA-256 through the adapter (rows 1, 3, 4, 16)."""
    payload = b"application-adapter contract bytes"
    obj = await _upload(staging_contract, payload)
    assert obj.size_bytes == len(payload)
    assert obj.sha256 == hashlib.sha256(payload).hexdigest()
    assert obj.created_at.tzinfo is not None
    assert await _read(staging_contract, obj) == payload
    refs = [ref async for ref in staging_contract.writer.list_objects()]
    assert len(refs) == 1
    assert refs[0].object_key == obj.object_key
    assert refs[0].size_bytes == obj.size_bytes
    # HTTP HEAD dates have second precision; XML listings may include milliseconds.
    assert refs[0].last_modified.replace(microsecond=0) == obj.created_at.replace(microsecond=0)
    assert "PutObject" in staging_contract.evidence.operations
    assert "HeadObject" in staging_contract.evidence.operations


async def test_zero_length_content_round_trips(staging_contract: StagingContract) -> None:
    """Verify zero-length upload and the empty SHA-256 digest (row 2)."""
    obj = await _upload(staging_contract, b"")
    assert obj.size_bytes == 0
    assert await _read(staging_contract, obj) == b""


@pytest.mark.parametrize("size", [_PART_SIZE, _PART_SIZE + 1, 2 * _PART_SIZE + 1])
async def test_multipart_upload_completes_and_verifies(
    staging_contract: StagingContract, size: int
) -> None:
    """Verify real part boundaries, completion and independent full SHA-256 (rows 9-11, 14)."""
    payload = b"m" * size
    obj = await _upload(staging_contract, payload)
    assert await _read(staging_contract, obj) == payload
    assert "CreateMultipartUpload" in staging_contract.evidence.operations
    assert (
        staging_contract.evidence.operations.count("UploadPart")
        == (size + _PART_SIZE - 1) // _PART_SIZE
    )
    etag = staging_contract.evidence.multipart_etags[-1]
    assert "-" in etag
    assert etag != obj.sha256


async def test_default_sdk_checksum_headers_are_accepted(
    staging_contract: StagingContract, record_property: Callable[[str, object], None]
) -> None:
    """Verify and record the actual SDK checksum and payload-signing defaults (rows 32-33)."""
    obj = await _upload(staging_contract, b"checksum wire contract")
    assert await _read(staging_contract, obj) == b"checksum wire contract"
    headers = staging_contract.evidence.checksum_headers[-1]
    record_property("sdk_checksum_headers", json.dumps(headers, sort_keys=True))
    assert headers["x-amz-sdk-checksum-algorithm"] == "CRC32"
    assert (
        "x-amz-checksum-crc32" in headers or headers.get("x-amz-trailer") == "x-amz-checksum-crc32"
    )
    signed_payload = headers["x-amz-content-sha256"]
    if "TRAILER" in signed_payload:
        assert "aws-chunked" in headers["content-encoding"]
        assert headers["x-amz-trailer"] == "x-amz-checksum-crc32"
    else:
        assert signed_payload == hashlib.sha256(b"checksum wire contract").hexdigest()


async def test_listing_paginates(staging_contract: StagingContract) -> None:
    """Require continuation tokens and complete object accounting (rows 7-8)."""
    objects = [await _upload(staging_contract, bytes([index])) for index in range(3)]
    refs = [ref async for ref in staging_contract.writer.list_objects()]
    assert {ref.object_key for ref in refs} == {obj.object_key for obj in objects}
    assert sum(ref.size_bytes for ref in refs) == 3
    assert len(staging_contract.evidence.continuation_tokens) >= 2


async def test_a_missing_object_is_distinguishable_from_a_denial(
    staging_contract: StagingContract,
) -> None:
    """Distinguish a remote missing key from a real scoped-identity denial (rows 19, 21)."""
    target = _target(b"absent")
    with pytest.raises(StagedEntryMissingError):
        await staging_contract.reader.download(target, io.BytesIO())
    with pytest.raises(StagingAccessDeniedError):
        await staging_contract.denied_reader.download(target, io.BytesIO())


async def test_integrity_mismatch_is_not_a_success(staging_contract: StagingContract) -> None:
    """Reject real remote bytes when either registered size or SHA-256 disagrees."""
    obj = await _upload(staging_contract, b"verified immutable media")
    for incorrect in (replace(obj, size_bytes=obj.size_bytes + 1), replace(obj, sha256="0" * 64)):
        with pytest.raises(StagingIntegrityError):
            await _read(staging_contract, incorrect)


async def test_deleting_an_absent_object_succeeds(staging_contract: StagingContract) -> None:
    """Retry deletion before and after a real upload without requiring existence (row 6)."""
    obj = await _upload(staging_contract, b"release me")
    await staging_contract.writer.delete(obj.object_key)
    await staging_contract.writer.delete(obj.object_key)
    await staging_contract.writer.delete(_target(b"").object_key)
    with pytest.raises(StagedEntryMissingError):
        await _read(staging_contract, obj)


@pytest.mark.parametrize("multipart", [False, True])
@pytest.mark.parametrize("failure", ["truncated", "oversized"])
async def test_invalid_stream_leaves_no_object_or_parts(
    staging_contract: StagingContract, multipart: bool, failure: str
) -> None:
    """Abort short and oversized streams before they can publish a completed object."""
    size = _PART_SIZE + 1 if multipart else 8
    target = _target(b"x" * size)
    payload = b"x" * (size - 1 if failure == "truncated" else size + 1)
    with pytest.raises(
        StagingIntegrityError if failure == "truncated" else StagingObjectStoreError
    ):
        await staging_contract.writer.upload(target, io.BytesIO(payload), declared_max_bytes=size)
    with pytest.raises(StagedEntryMissingError):
        await _read(staging_contract, target)
    assert [ref async for ref in staging_contract.writer.list_objects()] == []
    assert (
        await staging_contract.writer.abort_incomplete_uploads(
            datetime.now(UTC) + timedelta(minutes=1)
        )
        == 0
    )
    if multipart:
        assert "AbortMultipartUpload" in staging_contract.evidence.operations


class InterruptedSource(io.BytesIO):
    """Interrupt after the first real part reaches the backend."""

    def __init__(self, *, cancel: bool) -> None:
        super().__init__(b"x" * (2 * _PART_SIZE))
        self._cancel = cancel

    def read(self, size: int | None = -1) -> bytes:
        """Fail only after the adapter uploads its first buffered part."""
        if self.tell() >= _PART_SIZE:
            if self._cancel:
                raise asyncio.CancelledError("injected transfer interruption")
            raise OSError("injected source read failure")
        return super().read(size)


@pytest.mark.parametrize("cancel", [False, True])
async def test_an_aborted_upload_leaves_no_readable_object(
    staging_contract: StagingContract, cancel: bool
) -> None:
    """Reclaim real multipart parts after source failure or cancellation (rows 12-13)."""
    target = _target(b"x" * (2 * _PART_SIZE))
    with pytest.raises(asyncio.CancelledError if cancel else OSError):
        await staging_contract.writer.upload(
            target, InterruptedSource(cancel=cancel), declared_max_bytes=target.size_bytes
        )
    assert "UploadPart" in staging_contract.evidence.operations
    assert "AbortMultipartUpload" in staging_contract.evidence.operations
    with pytest.raises(StagedEntryMissingError):
        await _read(staging_contract, target)
    assert (
        await staging_contract.writer.abort_incomplete_uploads(
            datetime.now(UTC) + timedelta(minutes=1)
        )
        == 0
    )
    retried = await staging_contract.writer.upload(
        replace(target, attempt_id=uuid4()),
        io.BytesIO(b"x" * target.size_bytes),
        declared_max_bytes=target.size_bytes,
    )
    assert await _read(staging_contract, retried) == b"x" * target.size_bytes


async def test_concurrent_attempts_reads_and_cleanup(staging_contract: StagingContract) -> None:
    """Preserve independent immutable attempts while publication, reads and cleanup overlap."""
    source_key = StagingKey(uuid4().hex)
    payloads = [b"a" * (_PART_SIZE + 1), b"b" * (_PART_SIZE + 1)]
    targets = [_target(payload, key=source_key) for payload in payloads]
    objects = await asyncio.gather(
        *(
            staging_contract.writer.upload(
                target, io.BytesIO(payload), declared_max_bytes=len(payload)
            )
            for target, payload in zip(targets, payloads, strict=True)
        )
    )
    assert objects[0].object_key != objects[1].object_key
    assert await asyncio.gather(*(_read(staging_contract, obj) for obj in objects)) == payloads
    reads = await asyncio.gather(
        _read(staging_contract, objects[0]),
        staging_contract.writer.delete(objects[1].object_key),
        _upload(staging_contract, b"next publication"),
    )
    assert reads[0] == payloads[0]
    assert await _read(staging_contract, objects[0]) == payloads[0]


@pytest.mark.parametrize("fail_body", [False, True])
async def test_fixture_reclaims_completed_objects_and_abandoned_parts(
    staging_backend_name: str, fail_body: bool
) -> None:
    """Verify fixture cleanup after successful and failed bodies with real billed residue."""

    contracts: list[StagingContract] = []

    async def exercise_fixture() -> StagingContract:
        """Leave a completed object and an upload whose first abort loses its connection."""
        async with _isolated_contract(staging_backend_name) as contract:
            contracts.append(contract)
            await _upload(contract, b"unreferenced completed object")
            target = _target(b"x" * (2 * _PART_SIZE))
            contract.evidence.abort_failures_remaining = 1
            with pytest.raises(StagingObjectStoreError, match="could not be aborted"):
                await contract.writer.upload(
                    target, InterruptedSource(cancel=False), declared_max_bytes=target.size_bytes
                )
            if fail_body:
                raise RuntimeError("injected failing test body")
        return contract

    if fail_body:
        with pytest.raises(RuntimeError, match="injected failing test body"):
            await exercise_fixture()
    else:
        await exercise_fixture()
    contract = contracts[0]
    assert [ref async for ref in contract.writer.list_objects()] == []
    assert contract.evidence.operations.count("AbortMultipartUpload") == 2


async def test_sdk_retries_transient_put_failure(staging_contract: StagingContract) -> None:
    """Retry a lost connection through the SDK and verify the resulting immutable object."""
    staging_contract.evidence.put_failures_remaining = 1
    obj = await _upload(staging_contract, b"retry-safe transfer")
    assert len(staging_contract.evidence.checksum_headers) == 2
    assert await _read(staging_contract, obj) == b"retry-safe transfer"
    refs = [ref async for ref in staging_contract.writer.list_objects()]
    assert [ref.object_key for ref in refs] == [obj.object_key]
