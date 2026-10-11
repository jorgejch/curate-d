"""The S3 adapter behind `StagingObjectStore`, for RustFS locally and OVH in the cluster.

One adapter serves both backends. Endpoint, signing region, addressing style, TLS trust
and credentials are configuration; request construction, integrity checking and failure
translation are not, because a difference in those is exactly what would let a local
RustFS pass stand in for evidence about OVH.

Three properties the rest of staging depends on live here. Objects are written under an
attempt-unique key and are never overwritten, so a losing writer leaves an orphan rather
than corrupting a winner. The SHA-256 is computed by this process while streaming and is
the only digest anyone may trust: a multipart ETag is a completion token, not a content
hash, and OVH does not serve GetObjectAttributes at all. And every failure leaves this
boundary as a named staging error, with a multipart upload explicitly aborted rather than
left to accrue part storage nobody is accounting for.
"""

import asyncio
import hashlib
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Iterator
from contextlib import (
    AbstractAsyncContextManager,
    AsyncExitStack,
    asynccontextmanager,
    closing,
    contextmanager,
)
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import TYPE_CHECKING, BinaryIO, Final

import aioboto3
from aiobotocore.config import AioConfig
from botocore.exceptions import BotoCoreError, ClientError

from curate_d.config import Settings, StagingRole
from curate_d.observability import get_logger
from curate_d.staging.errors import (
    StagedEntryMissingError,
    StagingAccessDeniedError,
    StagingBucketMissingError,
    StagingError,
    StagingIntegrityError,
    StagingObjectStoreError,
    StagingTransientError,
    StagingUploadMissingError,
)
from curate_d.staging.objects import StagedObject, StagedObjectRef

if TYPE_CHECKING:
    from types_aiobotocore_s3.client import S3Client
    from types_aiobotocore_s3.type_defs import (
        CompletedPartTypeDef,
        MultipartUploadTypeDef,
        ObjectTypeDef,
    )

type S3ClientFactory = Callable[[], AbstractAsyncContextManager[S3Client]]

logger = get_logger(__name__)

# OVH maps STANDARD to its Standard tier and RustFS accepts the same request. It is sent
# explicitly so neither backend's default can quietly place staged media in a tier with
# a minimum-duration charge. A HEAD response may omit the class entirely, and absence
# means STANDARD; requiring an identical spelling from both backends would fail on a
# difference that carries no meaning.
_STANDARD: Final = "STANDARD"

# Well above the 5 MiB service minimum for a non-final part, and small enough that a
# transfer's buffer is a fixed cost rather than a function of the object's size.
_DEFAULT_PART_SIZE: Final = 8 * 1024 * 1024

# S3's hard multipart limits, which both backends enforce. The part ceiling is the one
# that bites: at a fixed 8 MiB part the default 100 GiB reservation needs 12,800 parts,
# and the rejection arrives at part 10,001 with ~80 GiB already transferred and billed.
_MAX_PARTS: Final = 10_000
_MAX_PART_SIZE: Final = 5 * 1024**3

# Total attempts per request, not retries on top of one. Stated rather than left to
# botocore's `legacy` default so the retry policy for a throttling-sensitive bucket is
# a decision in this file.
_MAX_ATTEMPTS: Final = 5

# A denial and an absence are indistinguishable to a reader that may not list the
# bucket, so they are separated here by code. Conflating them would turn a misprovisioned
# policy into an endless re-staging loop.
_DENIED_CODES: Final = frozenset(
    {
        "AccessDenied",
        "403",
        "AllAccessDisabled",
        "InvalidAccessKeyId",
        "SignatureDoesNotMatch",
        # A credential that has aged out is a denial the caller must re-provision, not
        # something a retry against the same token resolves.
        "ExpiredToken",
        "InvalidToken",
    }
)

# Only a missing key. A missing bucket and a missing multipart upload each get their own
# error: neither is "found nothing at that key", and callers that treat absence as
# success must not swallow them.
_MISSING_CODES: Final = frozenset({"404", "NoSuchKey"})
_BUCKET_MISSING_CODES: Final = frozenset({"NoSuchBucket"})
_UPLOAD_MISSING_CODES: Final = frozenset({"NoSuchUpload"})

# Worth another attempt later. Named so a caller can tell a throttle from a bad
# signature; botocore has already exhausted its own retries by the time these surface.
_TRANSIENT_CODES: Final = frozenset(
    {
        "SlowDown",
        "ServiceUnavailable",
        "RequestTimeout",
        "InternalError",
        "RequestTimeTooSkewed",
        "503",
        "500",
    }
)


class S3StagingObjectStore:
    """Remote staging objects over S3, satisfying the `StagingObjectStore` protocol.

    Holds no `Settings` and no credential after construction: the configured secrets
    reach the SDK session once, inside the client factory, and nothing on this object
    can render them.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        client_factory: S3ClientFactory | None = None,
        part_size: int = _DEFAULT_PART_SIZE,
    ) -> None:
        """Build the adapter from configuration.

        Args:
            settings: Resolved configuration. Must have an S3 staging endpoint.
            client_factory: Opens a client per operation. Substituted by tests at the
                SDK boundary; the default builds one from `settings` alone.
            part_size: Bytes buffered per multipart part. Real backends reject a
                non-final part below 5 MiB, so only a fake tolerates a smaller value.

        Raises:
            StagingObjectStoreError: No staging endpoint is configured.
            ValueError: `part_size` is not positive.
        """
        if not settings.s3_staging_enabled:
            raise StagingObjectStoreError(
                "S3 staging has no configured endpoint; staging fails here rather than "
                "guessing one or falling back to a different backend"
            )
        if part_size <= 0:
            raise ValueError("part_size must be positive")
        self._bucket = settings.staging_s3_bucket
        self._prefix = settings.staging_s3_content_prefix
        self._role = settings.staging_s3_role
        self._part_size = part_size
        self._transfer_timeout = settings.staging_transfer_timeout_seconds
        self._open_client = client_factory or build_client_factory(settings)
        # Per instance rather than module-level so two stores in one task cannot see
        # each other's pinned client; per task rather than per instance so a concurrent
        # download never rides a batch's connection.
        self._pinned_client: ContextVar[S3Client | None] = ContextVar(
            "curate_d_staging_pinned_client", default=None
        )

    @asynccontextmanager
    async def batch(self) -> AsyncGenerator[None, None]:
        """Pin one client for the metadata operations issued inside this scope.

        Reclamation is thousands of serial deletes, and a client per delete is a TCP
        connect, a TLS handshake, a fresh SSL context and a CA-bundle read each time —
        all of it under the global writer lock. Transfers deliberately stay outside this:
        `upload` and `download` always open their own client, so no connection outlives
        the transfer that needed it.

        Raises:
            StagingObjectStoreError: The client could not be opened.
        """
        async with self._connect() as client:
            token = self._pinned_client.set(client)
            try:
                yield
            finally:
                self._pinned_client.reset(token)

    @asynccontextmanager
    async def _client(self) -> AsyncGenerator["S3Client", None]:
        """Yield the pinned batch client, or open one for this operation alone."""
        pinned = self._pinned_client.get()
        if pinned is not None:
            yield pinned
            return
        async with self._connect() as client:
            yield client

    @asynccontextmanager
    async def _connect(self) -> AsyncGenerator["S3Client", None]:
        """Open a client, translating a failure to open it.

        Only the open is translated: a failure in the body keeps the context its own
        operation names rather than being reported as a failed connect.

        Raises:
            StagingObjectStoreError: The client could not be opened.
        """
        async with AsyncExitStack() as stack:
            with _sdk_failures("opening the staging client"):
                client = await stack.enter_async_context(self._open_client())
            yield client

    async def upload(
        self, target: StagedObject, source: BinaryIO, *, declared_max_bytes: int
    ) -> StagedObject:
        """Upload `source` under `target`'s attempt key and verify what landed.

        `target.size_bytes` is what the stream is expected to deliver and is enforced
        exactly, so a truncated source fails rather than publishing a short object under
        a complete object's name. `target.sha256` is verified only when set: a first
        upload does not know the digest until the bytes have been read, which is the
        value this call returns.

        Args:
            target: Key, attempt identifier, and expected metadata for this attempt.
            source: Bytes to upload, read once in bounded blocks.
            declared_max_bytes: Upper bound admission already reserved.

        Returns:
            The object as verified after upload, carrying the computed digest and the
            remote creation timestamp.

        Raises:
            StagingObjectStoreError: The upload or its verification did not complete, or
                the stream outgrew its reservation.
            StagingAccessDeniedError: This process is configured as a reader, or the
                identity may not write.
            StagingIntegrityError: What landed remotely does not match what was sent.
            StagingTransientError: The transfer did not finish inside its configured
                ceiling, or the backend asked for a later attempt.
            ValueError: A negative size or reservation was declared.
        """
        self._require_writer(f"upload {target.object_key}")
        if declared_max_bytes < 0 or target.size_bytes < 0:
            raise ValueError("declared_max_bytes and target.size_bytes must be nonnegative")
        if target.size_bytes > declared_max_bytes:
            raise StagingObjectStoreError(
                f"staging upload {target.object_key} declares {target.size_bytes} bytes "
                f"against a {declared_max_bytes}-byte reservation; admission reserved "
                f"less capacity than the upload needs"
            )
        part_size = self._part_size_for(target.object_key, declared_max_bytes)

        async with _transfer_deadline(self._transfer_timeout, f"uploading {target.object_key}"):
            return await self._upload(
                target, source, declared_max_bytes=declared_max_bytes, part_size=part_size
            )

    async def _upload(
        self, target: StagedObject, source: BinaryIO, *, declared_max_bytes: int, part_size: int
    ) -> StagedObject:
        """Send the bytes, under an already-checked reservation and part size."""
        key = self._remote_key(target.object_key)
        digest = hashlib.sha256()
        # A transfer never rides a pinned batch client: its connection must not outlive it.
        async with self._connect() as client:
            first = _next_block(source, part_size, written=0, limit=declared_max_bytes, key=key)
            if len(first) < part_size:
                # The block reader runs to end of stream, so a short block is the whole
                # object: it is verified and sent as one request, never as a multipart.
                _require_declared_size(target, len(first))
                digest.update(first)
                written = len(first)
                with _sdk_failures(f"uploading {key}"):
                    await client.put_object(
                        Bucket=self._bucket, Key=key, Body=first, StorageClass=_STANDARD
                    )
            else:
                written = await self._upload_parts(
                    client,
                    key,
                    target=target,
                    source=source,
                    first=first,
                    digest=digest,
                    limit=declared_max_bytes,
                    part_size=part_size,
                )
            return await self._verify_upload(
                client, key, target=target, written=written, sha256=digest.hexdigest()
            )

    def _part_size_for(self, object_key: str, declared_max_bytes: int) -> int:
        """Return the part size that keeps the reservation inside S3's 10,000 parts.

        Grows the configured part size rather than discovering the ceiling at part
        10,001, which is a rejection after the whole object has been transferred and
        left billing as abandoned parts.

        Raises:
            StagingObjectStoreError: Not even 10,000 maximum-sized parts cover the
                reservation, so the object can never be staged as one multipart upload.
        """
        needed = -(-declared_max_bytes // _MAX_PARTS)
        part_size = max(self._part_size, needed)
        if part_size > _MAX_PART_SIZE:
            raise StagingObjectStoreError(
                f"staging upload {object_key} reserves {declared_max_bytes} bytes, which "
                f"no multipart upload can carry: {_MAX_PARTS} parts of the {_MAX_PART_SIZE}"
                f"-byte maximum fall short. Refused before any byte is sent."
            )
        return part_size

    async def download(self, obj: StagedObject, destination: BinaryIO) -> None:
        """Stream `obj` into `destination`, verifying size and digest before returning.

        The digest is verified after the last byte is written, so `destination` may hold
        a partial copy when this raises. The caller owns that scratch and discards it;
        the alternative is buffering a whole object to keep the handle pristine. The size
        is the exception: it is enforced while the bytes arrive, because the caller's
        scratch reservation was sized from the registration and an object that has
        drifted past it would fill the volume before any check ran.

        Raises:
            StagedEntryMissingError: The object is absent; its registration is stale.
            StagingAccessDeniedError: The identity may not read it.
            StagingIntegrityError: The retrieved bytes do not match the registration,
                including as soon as they outgrow its declared size.
            StagingObjectStoreError: The transfer did not complete.
            StagingTransientError: The transfer did not finish inside its configured
                ceiling, or the backend asked for a later attempt.
            OSError: `destination` could not be written. Passed through untranslated:
                the destination is the caller's scratch and only the caller knows what a
                failed write there means.
        """
        async with _transfer_deadline(self._transfer_timeout, f"downloading {obj.object_key}"):
            await self._download(obj, destination)

    async def _download(self, obj: StagedObject, destination: BinaryIO) -> None:
        """Stream and verify one object, under an already-installed transfer deadline."""
        key = self._remote_key(obj.object_key)
        digest = hashlib.sha256()
        written = 0
        # A transfer never rides a pinned batch client: its connection must not outlive it.
        async with self._connect() as client:
            with _sdk_failures(f"reading staged object {key}"):
                response = await client.get_object(Bucket=self._bucket, Key=key)
                # Closed explicitly so a cancelled read does not leave aiohttp to report
                # the abandoned connection; the client's own teardown reclaims the
                # socket either way.
                with closing(response["Body"]) as body:
                    while True:
                        chunk = await body.read(self._part_size)
                        if not chunk:
                            break
                        digest.update(chunk)
                        written += len(chunk)
                        if written > obj.size_bytes:
                            # Stopped here rather than after the stream ends: the
                            # destination is a reservation sized from the registration,
                            # and an object that has drifted past it would fill the
                            # caller's scratch volume before anything checked.
                            raise StagingIntegrityError(
                                f"staged object {key} is registered at {obj.size_bytes} "
                                f"bytes but is still delivering past them; the "
                                f"registration no longer describes the object it names"
                            )
                        destination.write(chunk)

        if written != obj.size_bytes:
            raise StagingIntegrityError(
                f"staged object {key} is registered at {obj.size_bytes} bytes but "
                f"{written} were retrieved; the registration no longer describes the "
                f"object it names"
            )
        retrieved = digest.hexdigest()
        if retrieved != obj.sha256:
            raise StagingIntegrityError(
                f"staged object {key} hashes to {retrieved} but is registered as "
                f"{obj.sha256}; retrying the same bytes cannot resolve this"
            )

    async def delete(self, object_key: str) -> None:
        """Delete one object, treating an already-absent object as success.

        Absence means the key, and only the key. A missing bucket arrives as
        `StagingBucketMissingError` and is not caught here: reporting it as success would
        hand the caller freed bytes for a bucket nothing was ever deleted from.

        Raises:
            StagingObjectStoreError: Deletion failed for any reason other than the
                object already being absent.
            StagingAccessDeniedError: This process is configured as a reader, or the
                identity may not delete.
            StagingBucketMissingError: The configured bucket does not exist.
        """
        self._require_writer(f"delete {object_key}")
        key = self._remote_key(object_key)
        async with self._client() as client:
            try:
                with _sdk_failures(f"deleting staged object {key}"):
                    await client.delete_object(Bucket=self._bucket, Key=key)
            except StagedEntryMissingError:
                # Release is retried after partial cleanup, so an object that is already
                # gone is the state the caller asked for rather than a failure.
                return

    def list_objects(self) -> AsyncIterator[StagedObjectRef]:
        """Yield every completed object under the content prefix, paginating.

        Raises:
            StagingAccessDeniedError: This process is configured as a reader, which has
                no listing grant.
        """
        self._require_writer("list staged objects")
        return self._iter_objects()

    async def abort_incomplete_uploads(self, older_than: datetime) -> int:
        """Abort multipart uploads started before `older_than`, returning the count.

        Only the explicit half of residual cleanup. Some engines (MinIO did) restrict
        ListMultipartUploads to exact-key prefixes, so a backend may report fewer uploads
        here than it holds; the backend-specific lifecycle or server cleanup is the other
        half and neither is assumed to cover the other.

        An upload a peer writer aborted between this sweep's listing and its own abort
        is the desired end state, not a failure: it is skipped and the sweep continues,
        because two pods on the same cleanup interval would otherwise each abandon the
        remainder of their listing every tick.

        Raises:
            ValueError: `older_than` is naive, which cannot be ordered against the
                timezone-aware timestamps the SDK returns.
            StagingAccessDeniedError: This process is configured as a reader.
            StagingObjectStoreError: A listing or abort did not complete.
        """
        self._require_writer("abort incomplete uploads")
        if older_than.tzinfo is None:
            raise ValueError("older_than must be timezone-aware")

        aborted = 0
        async with self._client() as client:
            paginator = client.get_paginator("list_multipart_uploads")
            with _sdk_failures("listing incomplete staging uploads"):
                async for page in paginator.paginate(Bucket=self._bucket, Prefix=self._prefix):
                    for upload in page.get("Uploads", []):
                        key, upload_id, initiated = _describe_upload(upload)
                        if initiated >= older_than:
                            continue
                        try:
                            with _sdk_failures(f"aborting multipart upload {upload_id} of {key}"):
                                await client.abort_multipart_upload(
                                    Bucket=self._bucket, Key=key, UploadId=upload_id
                                )
                        except StagingUploadMissingError:
                            # Already reclaimed, by this pod's earlier tick or a peer's.
                            # Not counted: this sweep did not free those parts.
                            continue
                        aborted += 1
        return aborted

    async def _iter_objects(self) -> AsyncIterator[StagedObjectRef]:
        """Page through the content prefix, yielding one reference per distinct object.

        An engine can list a key more than once in one listing: RustFS 1.0.0 lists the
        zero-byte marker at the prefix itself twice (an upstream RustFS bug). Occupancy is
        summed from this listing, so a repeated key would be counted twice; each key is
        yielded once.
        """
        seen: set[str] = set()
        async with self._client() as client:
            paginator = client.get_paginator("list_objects_v2")
            with _sdk_failures("listing staged objects"):
                async for page in paginator.paginate(Bucket=self._bucket, Prefix=self._prefix):
                    for item in page.get("Contents", []):
                        ref = self._as_ref(item)
                        if ref is not None and ref.object_key not in seen:
                            seen.add(ref.object_key)
                            yield ref

    async def _upload_parts(
        self,
        client: "S3Client",
        key: str,
        *,
        target: StagedObject,
        source: BinaryIO,
        first: bytes,
        digest: "hashlib._Hash",
        limit: int,
        part_size: int,
    ) -> int:
        """Stream the remaining blocks as multipart parts, aborting on any failure."""
        # The create sits inside the try together with the identifier it yields, so any
        # failure from the moment the upload exists triggers an abort. The one window
        # nothing here can close is a cancellation between the backend creating the
        # upload and this process learning its identifier: there is no identifier to
        # abort with, and `abort_incomplete_uploads` is what reclaims it.
        upload_id: str | None = None
        parts: list[CompletedPartTypeDef] = []
        written = 0
        block = first
        try:
            with _sdk_failures(f"creating the multipart upload for {key}"):
                created = await client.create_multipart_upload(
                    Bucket=self._bucket, Key=key, StorageClass=_STANDARD
                )
            upload_id = created["UploadId"]
            while block:
                digest.update(block)
                written += len(block)
                number = len(parts) + 1
                with _sdk_failures(f"uploading part {number} of {key}"):
                    uploaded = await client.upload_part(
                        Bucket=self._bucket,
                        Key=key,
                        UploadId=upload_id,
                        PartNumber=number,
                        Body=block,
                    )
                # The part ETag is carried back to completion as an opaque token. It is
                # never folded into the digest: for a multipart object it is a hash of
                # part hashes, which no caller can recompute from the content.
                parts.append({"ETag": uploaded["ETag"], "PartNumber": number})
                block = _next_block(source, part_size, written=written, limit=limit, key=key)
            _require_declared_size(target, written)
            with _sdk_failures(f"completing the multipart upload for {key}"):
                await client.complete_multipart_upload(
                    Bucket=self._bucket,
                    Key=key,
                    UploadId=upload_id,
                    MultipartUpload={"Parts": parts},
                )
        except BaseException as failure:
            # Cancellation included: an abandoned upload bills for its parts whether the
            # worker failed or was simply told to stop.
            if upload_id is not None:
                await self._abort(client, key, upload_id, failure)
            raise
        return written

    async def _abort(
        self, client: "S3Client", key: str, upload_id: str, cause: BaseException
    ) -> None:
        """Abort a failed multipart upload, reporting residue if the abort also fails.

        The request is shielded because the common reason to be here is a cancellation,
        and a second one arriving mid-abort would drop a DELETE already on the wire
        together with the answer to whether parts are still billed. Every failure is
        reported, not just the SDK's own: a bare `OSError` out of the socket layer would
        otherwise replace both `cause` and the residue with a transport message.

        Raises:
            StagingObjectStoreError: The abort failed, leaving billed parts behind. It
                replaces `cause` rather than hiding it: the residue is the part an
                operator has to act on, and `cause` remains attached to it.
        """
        abort = asyncio.ensure_future(
            client.abort_multipart_upload(Bucket=self._bucket, Key=key, UploadId=upload_id)
        )
        try:
            await asyncio.shield(abort)
        except asyncio.CancelledError:
            await asyncio.wait({abort})
            if not abort.cancelled():
                # Retrieved so an abort failure during teardown is not reported as an
                # unhandled task exception; the cancellation is what propagates.
                abort.exception()
            raise
        except BaseException as error:
            raise StagingObjectStoreError(
                f"uploading {key} failed and its multipart upload {upload_id} could not "
                f"be aborted ({error}); its parts stay billed until the backend's "
                f"residual cleanup reclaims them"
            ) from cause

    async def _verify_upload(
        self, client: "S3Client", key: str, *, target: StagedObject, written: int, sha256: str
    ) -> StagedObject:
        """HEAD the uploaded object and confirm it is the one that may be registered.

        Raises:
            StagingIntegrityError: The remote size or the declared digest disagrees.
            StagingObjectStoreError: The HEAD failed, or the object landed in a storage
                class nobody asked for.
        """
        with _sdk_failures(f"verifying {key} after upload"):
            head = await client.head_object(Bucket=self._bucket, Key=key)

        if head["ContentLength"] != written:
            raise StagingIntegrityError(
                f"staging upload {key} sent {written} bytes but the object reports "
                f"{head['ContentLength']}; it is not registerable"
            )
        storage_class = head.get("StorageClass", _STANDARD)
        if storage_class != _STANDARD:
            raise StagingObjectStoreError(
                f"staging upload {key} requested the {_STANDARD} class but landed in "
                f"{storage_class}; a retention tier would bill for media staging deletes"
            )
        if target.sha256 and target.sha256 != sha256:
            raise StagingIntegrityError(
                f"staging upload {key} hashes to {sha256} but was declared as "
                f"{target.sha256}; the source did not deliver the expected content"
            )
        return StagedObject(
            key=target.key,
            attempt_id=target.attempt_id,
            size_bytes=written,
            sha256=sha256,
            created_at=_aware(head["LastModified"]),
        )

    def _as_ref(self, item: "ObjectTypeDef") -> StagedObjectRef | None:
        """Convert one listing entry, or None for the prefix's own placeholder key.

        Raises:
            StagingObjectStoreError: The entry carries no key, size or timestamp. Skipping it
                would understate occupancy, which admission would then read as capacity
                that does not exist.
        """
        key = item.get("Key")
        size = item.get("Size")
        modified = item.get("LastModified")
        if key is None or size is None or modified is None:
            raise StagingObjectStoreError(
                f"staging listing returned an entry without a key, size or timestamp "
                f"({item}); occupancy cannot be accounted for from it"
            )
        if not key.startswith(self._prefix) or key == self._prefix:
            return None
        return StagedObjectRef(
            object_key=key[len(self._prefix) :],
            size_bytes=size,
            last_modified=_aware(modified),
        )

    def _remote_key(self, object_key: str) -> str:
        """Place a prefix-relative object key under the configured content prefix."""
        return f"{self._prefix}{object_key}"

    def _require_writer(self, operation: str) -> None:
        """Refuse a write operation from a process configured as a reader.

        Raises:
            StagingAccessDeniedError: The configured role is reader. Refused locally
                rather than at the endpoint, so a worker holding credentials it should
                never have been given still cannot use them.
        """
        if self._role is not StagingRole.WRITER:
            raise StagingAccessDeniedError(
                f"this process is configured as a staging {self._role.value} and may "
                f"not {operation}"
            )


def build_client_factory(settings: Settings) -> S3ClientFactory:
    """Build an S3 client factory from explicit configuration.

    Credentials come from `settings` rather than from the SDK's ambient chain, which
    would otherwise fall back to an instance role or a developer's `~/.aws` and hand a
    worker authority nobody granted it.

    Raises:
        StagingObjectStoreError: The endpoint or its credentials are unset.
    """
    endpoint = settings.staging_s3_endpoint_url
    access_key = settings.staging_s3_access_key_id
    secret_key = settings.staging_s3_secret_access_key
    if endpoint is None or access_key is None or secret_key is None:
        raise StagingObjectStoreError(
            "S3 staging needs an explicit endpoint, access key and secret; there is no "
            "ambient credential fallback"
        )

    session = aioboto3.Session(
        aws_access_key_id=access_key.get_secret_value(),
        aws_secret_access_key=secret_key.get_secret_value(),
        region_name=settings.staging_s3_region,
    )
    config = AioConfig(
        s3={"addressing_style": settings.staging_s3_addressing_style.value},
        signature_version="s3v4",
        # These bound one socket operation each. The whole-transfer ceiling the setting
        # documents is installed separately, by `_transfer_deadline`.
        connect_timeout=settings.staging_transfer_timeout_seconds,
        read_timeout=settings.staging_transfer_timeout_seconds,
        # `standard` rather than botocore's `legacy` default: it retries on a documented
        # code list with equal-jitter backoff and a retry quota, which is what a bucket
        # that answers SlowDown under load needs.
        retries={"mode": "standard", "max_attempts": _MAX_ATTEMPTS},
    )
    endpoint_url = str(endpoint)
    verify = settings.staging_s3_verify_tls

    def _open() -> AbstractAsyncContextManager["S3Client"]:
        """Open one client. Per operation, so no connection outlives its transfer."""
        return session.client("s3", endpoint_url=endpoint_url, config=config, verify=verify)

    return _open


@asynccontextmanager
async def _transfer_deadline(seconds: float, context: str) -> AsyncGenerator[None, None]:
    """Bound one whole transfer, rather than one socket operation at a time.

    `connect_timeout` and `read_timeout` bound individual reads and writes, so a backend
    delivering a trickle keeps every part comfortably inside them while the transfer runs
    for days holding its admission reservation. This is the ceiling the configured
    setting promises. An expiry logs `staging.transfer_deadline_hit`: the ceiling also
    bounds how long a transfer keeps the staging locks around it held.

    The event is keyed on the deadline having expired, not on a `TimeoutError` reaching
    this frame: a multipart abort that fails during the cancellation raises its own
    error from the `CancelledError`, which `asyncio.timeout` then lets through as is.

    Raises:
        StagingTransientError: The transfer did not finish within `seconds`. Retryable:
            the same bytes against a recovered endpoint is exactly the next attempt.
    """
    deadline = asyncio.timeout(seconds)
    try:
        async with deadline:
            yield
    except TimeoutError as expiry:
        raise StagingTransientError(
            f"{context} did not finish within its {seconds}-second ceiling; it is "
            f"abandoned rather than left holding its admission reservation"
        ) from expiry
    finally:
        if deadline.expired():
            logger.warning(
                "staging.transfer_deadline_hit", transfer=context, deadline_seconds=seconds
            )


@contextmanager
def _sdk_failures(context: str) -> Iterator[None]:
    """Translate the SDK's exception surface into named staging failures.

    The two backends do not raise identically, so the translation stops here rather than
    leaving every caller to match on botocore error codes.
    """
    try:
        yield
    except ClientError as error:
        raise _translate(error, context) from error
    except BotoCoreError as error:
        raise StagingObjectStoreError(f"{context} failed: {error}") from error


def _translate(error: ClientError, context: str) -> StagingError:
    """Choose the staging error for one client error, keeping the families apart.

    Five outcomes, because collapsing any pair of them loses a decision a caller has to
    make: a denial is a policy fix, an absent key is a stale registration, an absent
    bucket is a misconfiguration, an absent upload is another sweep's success, and a
    transient failure is the only one worth trying again.
    """
    code = error.response.get("Error", {}).get("Code", "")
    if code in _DENIED_CODES:
        return StagingAccessDeniedError(
            f"{context} was denied by the configured staging identity's policy ({code})"
        )
    if code in _BUCKET_MISSING_CODES:
        return StagingBucketMissingError(
            f"{context} found no bucket to act on ({code}); the configured staging bucket "
            f"does not exist at this endpoint, so nothing was read, written or freed"
        )
    if code in _UPLOAD_MISSING_CODES:
        return StagingUploadMissingError(
            f"{context} found no such multipart upload ({code}); it was already completed "
            f"or aborted"
        )
    if code in _MISSING_CODES:
        return StagedEntryMissingError(f"{context} found nothing at that key ({code})")
    if code in _TRANSIENT_CODES:
        return StagingTransientError(
            f"{context} failed with {code}; the request may succeed on a later attempt"
        )
    return StagingObjectStoreError(f"{context} failed with {code or 'an unnamed error'}: {error}")


def _next_block(source: BinaryIO, part_size: int, *, written: int, limit: int, key: str) -> bytes:
    """Read the next bounded block, refusing a stream that outgrows its reservation.

    Reads one byte past the remaining allowance when that is the tighter of the two
    bounds, so an oversized stream is caught before its excess is sent anywhere.

    Raises:
        StagingObjectStoreError: The stream exceeds `limit`.
    """
    allowance = limit - written
    block = _read_block(source, min(part_size, allowance + 1))
    if len(block) > allowance:
        raise StagingObjectStoreError(
            f"staging upload {key} exceeds its {limit}-byte reservation; the remainder "
            f"would consume capacity admission never reserved"
        )
    return block


def _read_block(source: BinaryIO, size: int) -> bytes:
    """Read exactly `size` bytes, or fewer at end of stream.

    A single `read` may return short without the stream being finished, so a short block
    only means end of stream once the loop has asked again.
    """
    chunks: list[bytes] = []
    remaining = size
    while remaining > 0:
        chunk = source.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _require_declared_size(target: StagedObject, written: int) -> None:
    """Refuse a stream that did not deliver the byte count the caller declared.

    Raises:
        StagingIntegrityError: The stream was truncated or over-long.
    """
    if written != target.size_bytes:
        raise StagingIntegrityError(
            f"staging upload {target.object_key} declared {target.size_bytes} bytes but "
            f"its source delivered {written}; a truncated stream must not be published "
            f"under a complete object's name"
        )


def _describe_upload(upload: "MultipartUploadTypeDef") -> tuple[str, str, datetime]:
    """Pull the key, upload identifier and start time out of one listing entry.

    Raises:
        StagingObjectStoreError: The entry is missing any of the three, leaving an
            upload that cannot be aborted and would silently keep billing.
    """
    key = upload.get("Key")
    upload_id = upload.get("UploadId")
    initiated = upload.get("Initiated")
    if key is None or upload_id is None or initiated is None:
        raise StagingObjectStoreError(
            f"staging listed an incomplete upload without a key, identifier or start "
            f"time ({upload}); it cannot be aborted from this response"
        )
    return key, upload_id, _aware(initiated)


def _aware(value: datetime) -> datetime:
    """Read an SDK timestamp as UTC when it arrives without a timezone."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
