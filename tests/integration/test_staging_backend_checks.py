"""Verify backend provisioning differences against the staging compatibility matrix.

Select CURATE_D_TEST_STAGING_BACKENDS=rustfs or ovh; the default requires both.
OVH uses the contract suite's CURATE_D_TEST_OVH_* variables and explicit DISPOSABLE=true.
Lifecycle observations require CURATE_D_TEST_STAGING_LIFECYCLE_TIMEOUT_SECONDS. Without
that wall-clock budget they skip on a backend whose observation is recorded in the matrix
and fail as PENDING on any other; they never pass because a rule merely exists.
`make lifecycle-check` observes them on a time-scaled RustFS in seconds; OVH acts on real
day boundaries and needs a multi-day budget.
"""

import asyncio
import contextlib
import hashlib
import os
import re
import time
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NoReturn, cast
from urllib.parse import quote, urlencode, urlsplit
from uuid import uuid4

import aioboto3
import httpx
import pytest
from botocore import UNSIGNED
from botocore.config import Config
from botocore.exceptions import ClientError, SSLError
from pydantic import HttpUrl, SecretStr

from curate_d.config import S3AddressingStyle, Settings, StagingRole
from curate_d.staging.errors import StagingNotReadyError
from curate_d.staging.factories import check_staging_ready, refresh_readiness_marker
from curate_d.staging.object_store import S3StagingObjectStore
from tests.integration.rustfs_staging import provisioned_location

if TYPE_CHECKING:
    from types_aiobotocore_s3.client import S3Client
    from types_aiobotocore_s3.type_defs import CompletedPartTypeDef

pytestmark = pytest.mark.integration
_PART_SIZE = 5 * 1024 * 1024
_ROOT = Path(__file__).resolve().parents[2]
# The loopback ports compose.yaml publishes for each local engine.
_LOCAL_ENDPOINTS = {"rustfs": "http://127.0.0.1:9100"}
# compose.yaml's development root credentials, overridable as CURATE_D_TEST_<ENGINE>_ROOT_*.
_LOCAL_ROOT_DEFAULTS = {"rustfs": "rustfsadmin"}


def _pending(row: int, reason: str) -> NoReturn:
    """Fail visibly when required backend evidence cannot be collected."""
    pytest.fail(
        f"PENDING VERIFICATION (staging-compatibility.md row {row}): {reason}", pytrace=False
    )


@dataclass(frozen=True)
class Backend:
    """Hold explicitly selected disposable provisioning-check configuration."""

    name: str
    endpoint: str
    bucket: str
    prefix: str
    region: str
    addressing: Literal["auto", "virtual", "path"]
    writer: tuple[str, str] = field(repr=False)
    reader: tuple[str, str] = field(repr=False)


@pytest.fixture
def backend(staging_backend_name: str) -> Backend:
    """Resolve the selected backend; OVH requires explicit credentials and disposable consent."""
    name = staging_backend_name
    env = f"CURATE_D_TEST_{name.upper()}_"
    if name == "ovh":
        required = (
            "ENDPOINT",
            "BUCKET",
            "WRITER_ACCESS_KEY_ID",
            "WRITER_SECRET_ACCESS_KEY",
            "READER_ACCESS_KEY_ID",
            "READER_SECRET_ACCESS_KEY",
        )
        missing = [key for key in required if not os.environ.get(env + key)]
        if missing or os.environ.get(env + "DISPOSABLE") != "true":
            _pending(
                22, "OVH needs disposable fixture configuration and role-specific S3 credentials"
            )
    endpoint = os.environ.get(env + "ENDPOINT", _LOCAL_ENDPOINTS.get(name, ""))
    if name == "ovh" and not endpoint.startswith("https://"):
        pytest.fail("OVH checks require certificate-verified HTTPS", pytrace=False)
    local_bucket, local_prefix = provisioned_location() if name != "ovh" else ("", "content/")
    prefix = os.environ.get(env + "PREFIX", local_prefix)
    if not prefix or not prefix.endswith("/"):
        pytest.fail("disposable fixture prefix must be nonempty and end with /", pytrace=False)
    return Backend(
        name,
        endpoint,
        os.environ.get(env + "BUCKET", local_bucket),
        prefix,
        os.environ.get(env + "REGION", "us-east-va" if name == "ovh" else "us-east-1"),
        cast(
            Literal["auto", "virtual", "path"],
            os.environ.get(env + "ADDRESSING_STYLE", "virtual" if name == "ovh" else "path"),
        ),
        (
            os.environ.get(env + "WRITER_ACCESS_KEY_ID", "curate-d-staging-writer"),
            os.environ.get(env + "WRITER_SECRET_ACCESS_KEY", "writer-development-secret"),
        ),
        (
            os.environ.get(env + "READER_ACCESS_KEY_ID", "curate-d-staging-reader"),
            os.environ.get(env + "READER_SECRET_ACCESS_KEY", "reader-development-secret"),
        ),
    )


@asynccontextmanager
async def _client(
    backend: Backend,
    credentials: tuple[str, str],
    *,
    anonymous: bool = False,
    ca_bundle: str | None = None,
) -> AsyncGenerator["S3Client", None]:
    """Open one identity without the ambient credential chain."""
    session = aioboto3.Session(
        aws_access_key_id=credentials[0],
        aws_secret_access_key=credentials[1],
        region_name=backend.region,
    )
    async with session.client(  # type: ignore[call-overload]
        "s3",
        endpoint_url=backend.endpoint,
        verify=ca_bundle or True,
        config=Config(
            signature_version=UNSIGNED if anonymous else "s3v4",
            s3={"addressing_style": backend.addressing},
            connect_timeout=5,
            read_timeout=10,
            retries={"max_attempts": 1},
        ),
    ) as client:
        yield client


@pytest.fixture
async def writer(backend: Backend) -> AsyncIterator["S3Client"]:
    """Open the provisioned writer identity."""
    async with _client(backend, backend.writer) as client:
        yield client


@pytest.fixture
async def reader(backend: Backend) -> AsyncIterator["S3Client"]:
    """Open the provisioned reader identity."""
    async with _client(backend, backend.reader) as client:
        yield client


@pytest.fixture
async def staged_key(backend: Backend, writer: "S3Client") -> AsyncIterator[str]:
    """Remove only this test's unique completed object on every exit path."""
    key = f"{backend.prefix}backend-checks/{uuid4().hex}/object"
    try:
        yield key
    finally:
        await writer.delete_object(Bucket=backend.bucket, Key=key)


@asynccontextmanager
async def _residual_upload(
    backend: Backend,
    writer: "S3Client",
    key: str,
) -> AsyncGenerator[str, None]:
    """Abort this test's upload on failure as well as success."""
    upload = await writer.create_multipart_upload(
        Bucket=backend.bucket, Key=key, StorageClass="STANDARD"
    )
    upload_id = upload["UploadId"]
    try:
        yield upload_id
    finally:
        try:
            await writer.abort_multipart_upload(Bucket=backend.bucket, Key=key, UploadId=upload_id)
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "NoSuchUpload":
                raise


async def test_only_provisioned_identities_are_accepted(backend: Backend, staged_key: str) -> None:
    """Reject unknown keys and wrong secrets (row 22)."""
    for credentials in (
        ("no-such-identity", "no-such-secret"),
        (backend.writer[0], "wrong-secret"),
    ):
        async with _client(backend, credentials) as client:
            with pytest.raises(ClientError) as failure:
                await client.get_object(Bucket=backend.bucket, Key=staged_key)
            assert failure.value.response["Error"]["Code"] in {
                "InvalidAccessKeyId",
                "SignatureDoesNotMatch",
                "AccessDenied",
            }


async def test_anonymous_requests_are_denied(
    backend: Backend,
    writer: "S3Client",
    staged_key: str,
) -> None:
    """Reject anonymous access to an existing object (row 22)."""
    await writer.put_object(Bucket=backend.bucket, Key=staged_key, Body=b"private")
    async with _client(backend, backend.writer, anonymous=True) as client:
        with pytest.raises(ClientError) as failure:
            await client.get_object(Bucket=backend.bucket, Key=staged_key)
        assert failure.value.response["Error"]["Code"] == "AccessDenied"


async def test_reader_is_read_only(
    backend: Backend,
    writer: "S3Client",
    reader: "S3Client",
    staged_key: str,
) -> None:
    """Verify reader GET and denied PUT/DELETE with real identities (rows 3, 16, 17, 22)."""
    await writer.put_object(Bucket=backend.bucket, Key=staged_key, Body=b"private")
    response = await reader.get_object(Bucket=backend.bucket, Key=staged_key)
    async with response["Body"] as body:
        assert await body.read() == b"private"
    with pytest.raises(ClientError) as put:
        await reader.put_object(Bucket=backend.bucket, Key=staged_key, Body=b"forbidden")
    assert put.value.response["Error"]["Code"] == "AccessDenied"
    with pytest.raises(ClientError) as delete:
        await reader.delete_object(Bucket=backend.bucket, Key=staged_key)
    assert delete.value.response["Error"]["Code"] == "AccessDenied"


async def test_reader_bucket_enumeration_matches_recorded_posture(
    backend: Backend, reader: "S3Client"
) -> None:
    """Pin reader bucket-wide listing: denied locally, provider-specific on OVH."""
    if backend.name == "ovh":
        # This provider's reader needs bucket-wide listing for an absent key to answer 404
        # (row 21), a provider-specific behaviour. Fails if the grant is revoked.
        response = await reader.list_objects_v2(Bucket=backend.bucket, MaxKeys=1)
        assert response["ResponseMetadata"]["HTTPStatusCode"] == 200
        return
    with pytest.raises(ClientError) as failure:
        await reader.list_objects_v2(Bucket=backend.bucket, MaxKeys=1)
    assert failure.value.response["Error"]["Code"] == "AccessDenied"


async def test_neither_runtime_identity_can_create_a_bucket(
    backend: Backend, writer: "S3Client", reader: "S3Client"
) -> None:
    """Deny bucket creation to both runtime identities (row 20)."""
    # Without a matching LocationConstraint OVH can reject the request as malformed,
    # which would pass as a denial without exercising the policy.
    location: dict[str, dict[str, str]] = (
        {}
        if backend.region == "us-east-1"
        else {"CreateBucketConfiguration": {"LocationConstraint": backend.region}}
    )
    for client in (writer, reader):
        bucket = f"curate-d-unauthorized-{uuid4().hex[:16]}"
        try:
            with pytest.raises(ClientError) as failure:
                await client.create_bucket(Bucket=bucket, **location)  # type: ignore[arg-type]
        finally:
            # A permitted create would leave a real bucket behind; remove it before failing.
            with contextlib.suppress(ClientError):
                await client.delete_bucket(Bucket=bucket)
        assert failure.value.response["Error"]["Code"] == "AccessDenied"


async def test_reader_is_denied_another_bucket(backend: Backend, reader: "S3Client") -> None:
    """Deny the reader an object under its own prefix in any other bucket."""
    if backend.name == "ovh":
        pytest.skip("local engines only: this suite provisions no second OVH bucket")
    bucket = f"curate-d-other-{uuid4().hex[:16]}"
    # Same key as a staged object would have, so only the bucket differs from the grant.
    key = f"{backend.prefix}backend-checks/{uuid4().hex}/object"
    async with _client(backend, _root_credentials(backend)) as admin:
        await admin.create_bucket(Bucket=bucket)
        try:
            await admin.put_object(Bucket=bucket, Key=key, Body=b"other bucket")
            with pytest.raises(ClientError) as failure:
                await reader.get_object(Bucket=bucket, Key=key)
            assert failure.value.response["Error"]["Code"] == "AccessDenied"
        finally:
            with contextlib.suppress(ClientError):
                await admin.delete_object(Bucket=bucket, Key=key)
            await admin.delete_bucket(Bucket=bucket)


async def test_neighbouring_keys_stay_denied(
    backend: Backend, writer: "S3Client", reader: "S3Client"
) -> None:
    """Deny both identities keys that share the prefix's spelling but sit outside it.

    RustFS's policies also grant the slash-less prefix key (see its initialize.sh), which
    must widen them by that one key and no further.
    """
    if backend.name == "ovh":
        pytest.skip("local engines only: seeding keys outside the prefix needs root access")
    root = backend.prefix.rstrip("/")
    neighbours = [
        f"{root}x",
        f"{root}-other/{uuid4().hex}",
        f"{root}.{uuid4().hex}",
        f"outside-{uuid4().hex}/object",
    ]
    async with _client(backend, _root_credentials(backend)) as admin:
        try:
            for key in neighbours:
                await admin.put_object(Bucket=backend.bucket, Key=key, Body=b"neighbour")
            for key in neighbours:
                with pytest.raises(ClientError) as read:
                    await reader.get_object(Bucket=backend.bucket, Key=key)
                assert read.value.response["Error"]["Code"] == "AccessDenied", key
                with pytest.raises(ClientError) as write:
                    await writer.put_object(Bucket=backend.bucket, Key=key, Body=b"overwrite")
                assert write.value.response["Error"]["Code"] == "AccessDenied", key
        finally:
            for key in neighbours:
                with contextlib.suppress(ClientError):
                    await admin.delete_object(Bucket=backend.bucket, Key=key)


async def test_workers_pass_readiness_at_the_content_prefix(backend: Backend) -> None:
    """Run both workers' startup readiness against the real content-prefix marker.

    The marker's key is the granted prefix itself (`content/`). The contract suite's
    per-test prefixes all sit below it, so only this check proves a `content/*` grant
    covers the marker, which is where a policy engine's wildcard matching can differ.
    """
    await check_staging_ready(_role_settings(backend, StagingRole.WRITER))
    await check_staging_ready(_role_settings(backend, StagingRole.READER))


async def test_listing_the_content_prefix_yields_each_object_once(
    backend: Backend, writer: "S3Client"
) -> None:
    """List the real content prefix, marker included, as occupancy accounting does.

    The contract suite lists per-test sub-prefixes, which never contain the marker. At
    the prefix itself RustFS 1.0.0 lists the marker twice and, with one key per page,
    ends pagination after it (an upstream RustFS bug). The adapter's listing must still yield every
    object exactly once and never the marker, including across small pages.
    """
    settings = _role_settings(backend, StagingRole.WRITER)
    await refresh_readiness_marker(settings)
    sub = f"listing-{uuid4().hex}/"
    keys = [f"{backend.prefix}{sub}{index:02d}" for index in range(5)]
    try:
        for key in keys:
            await writer.put_object(
                Bucket=backend.bucket, Key=key, Body=b"x", StorageClass="STANDARD"
            )

        listed = [ref.object_key async for ref in S3StagingObjectStore(settings).list_objects()]
        ours = [key for key in listed if key.startswith(sub)]
        assert sorted(ours) == [key.removeprefix(backend.prefix) for key in keys]
        assert len(listed) == len(set(listed))
        assert "" not in listed

        paged: list[str] = []
        paginator = writer.get_paginator("list_objects_v2")
        async for page in paginator.paginate(
            Bucket=backend.bucket, Prefix=backend.prefix, PaginationConfig={"PageSize": 2}
        ):
            paged += [item["Key"] for item in page.get("Contents", [])]
        assert set(keys) <= set(paged)
    finally:
        for key in keys:
            with contextlib.suppress(ClientError):
                await writer.delete_object(Bucket=backend.bucket, Key=key)


async def test_marker_refresh_survives_a_concurrent_writer(
    backend: Backend, writer: "S3Client"
) -> None:
    """Refresh the marker while another writer rewrites it, as peer I/O workers do.

    Every I/O worker refreshes the same zero-byte marker on its idle tick, so two
    writes of the key can overlap. Each refresh must still read back what it wrote:
    a read that misses the key would fail the worker's readiness.
    """
    settings = _role_settings(backend, StagingRole.WRITER)
    stop = asyncio.Event()

    async def rewrite() -> None:
        """Keep rewriting the marker until the refreshes are done."""
        while not stop.is_set():
            await writer.put_object(
                Bucket=backend.bucket, Key=backend.prefix, Body=b"", StorageClass="STANDARD"
            )

    peer = asyncio.create_task(rewrite())
    failures: list[str] = []
    try:
        for _attempt in range(50):
            try:
                await refresh_readiness_marker(settings)
            except StagingNotReadyError as error:
                failures.append(str(error))
    finally:
        stop.set()
        await peer
    assert failures == []


@pytest.mark.parametrize("multipart", [False, True])
async def test_storage_class_standard_is_accepted(
    backend: Backend,
    writer: "S3Client",
    staged_key: str,
    multipart: bool,
) -> None:
    """Verify STANDARD requests and encrypted single/multipart objects (rows 23, 24, 26, 27)."""
    if multipart:
        async with _residual_upload(backend, writer, staged_key) as upload_id:
            parts: list[CompletedPartTypeDef] = []
            for number, payload in enumerate((b"x" * _PART_SIZE, b"tail"), 1):
                result = await writer.upload_part(
                    Bucket=backend.bucket,
                    Key=staged_key,
                    UploadId=upload_id,
                    PartNumber=number,
                    Body=payload,
                )
                parts.append({"ETag": result["ETag"], "PartNumber": number})
            await writer.complete_multipart_upload(
                Bucket=backend.bucket,
                Key=staged_key,
                UploadId=upload_id,
                MultipartUpload={"Parts": parts},
            )
    else:
        await writer.put_object(
            Bucket=backend.bucket, Key=staged_key, Body=b"standard tier", StorageClass="STANDARD"
        )
    # Assert on HEAD: OVH's CompleteMultipartUpload response omits ServerSideEncryption.
    head = await writer.head_object(Bucket=backend.bucket, Key=staged_key)
    assert head.get("StorageClass", "STANDARD") == "STANDARD"
    assert head["ServerSideEncryption"] == "AES256"
    listed = await writer.list_objects_v2(Bucket=backend.bucket, Prefix=staged_key)
    assert [item.get("StorageClass") for item in listed["Contents"]] == ["STANDARD"]


def _positive_days(value: object) -> bool:
    """Require a positive service retention period."""
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _mapping(value: object) -> dict[str, object]:
    """Validate a management response object without trusting untyped JSON."""
    if value is None:
        return {}
    assert isinstance(value, dict) and all(isinstance(key, str) for key in value)
    return cast(dict[str, object], value)


async def _ovh_bucket_state(backend: Backend, object_key: str) -> dict[str, object]:
    """Read actual tier and configuration through OVH's separate management API.

    The read-only request signature follows ovh/python-ovh's documented algorithm.
    S3 credentials cannot authorize this API and are never reused as management keys.
    """
    prefix = "CURATE_D_TEST_OVH_MANAGEMENT_"
    required = ("APPLICATION_KEY", "APPLICATION_SECRET", "CONSUMER_KEY", "PROJECT", "REGION")
    values = {name: os.environ.get(prefix + name, "") for name in required}
    if not all(values.values()):
        _pending(
            27,
            "OVH actual tier/encryption/lifecycle need explicit read-only management "
            "API credentials and project/region configuration",
        )
    endpoint = os.environ.get(prefix + "ENDPOINT", "https://api.us.ovhcloud.com/1.0").rstrip("/")
    parsed = urlsplit(endpoint)
    assert parsed.scheme == "https" and parsed.hostname in {
        "api.us.ovhcloud.com",
        "eu.api.ovh.com",
        "ca.api.ovh.com",
    }
    assert parsed.username is None and parsed.password is None and not parsed.query
    target = (
        endpoint
        + "/cloud/project/"
        + quote(values["PROJECT"], safe="")
        + "/region/"
        + quote(values["REGION"], safe="")
        + "/storage/"
        + quote(backend.bucket, safe="")
        + "?"
        + urlencode({"prefix": object_key, "limit": 1})
    )
    async with httpx.AsyncClient(verify=True, timeout=15) as client:
        clock = await client.get(endpoint + "/auth/time")
        clock.raise_for_status()
        timestamp = str(int(clock.text))
        signature = hashlib.sha1(
            "+".join(
                (values["APPLICATION_SECRET"], values["CONSUMER_KEY"], "GET", target, "", timestamp)
            ).encode()
        ).hexdigest()
        response = await client.get(
            target,
            headers={
                "X-Ovh-Application": values["APPLICATION_KEY"],
                "X-Ovh-Consumer": values["CONSUMER_KEY"],
                "X-Ovh-Timestamp": timestamp,
                "X-Ovh-Signature": "$1$" + signature,
            },
        )
        assert response.is_success, "OVH management GET failed; configuration is unverified"
        return _mapping(response.json())


async def test_provisioning_encryption_and_lifecycle(
    backend: Backend,
    writer: "S3Client",
    staged_key: str,
) -> None:
    """Verify backend-specific SSE-S3 and cleanup configuration (rows 23, 25, 28, 29)."""
    if backend.name == "ovh":
        await writer.put_object(
            Bucket=backend.bucket,
            Key=staged_key,
            Body=b"management tier probe",
            StorageClass="STANDARD",
        )
        state = await _ovh_bucket_state(backend, staged_key)
        assert state["name"] == backend.bucket
        assert _mapping(state["encryption"])["sseAlgorithm"] == "AES256"
        assert _mapping(state["versioning"])["status"] == "disabled"
        lock = state.get("objectLock")
        assert lock is None or _mapping(lock).get("status") == "disabled"
        objects = state["objects"]
        assert isinstance(objects, list)
        assert any(
            _mapping(obj).get("key") == staged_key
            and _mapping(obj).get("storageClass") == "STANDARD"
            for obj in objects
        )
        lifecycle = _mapping(state["lifecycle"])
        rules = lifecycle["rules"]
        assert isinstance(rules, list)
        enabled = [_mapping(rule) for rule in rules if _mapping(rule).get("status") == "enabled"]
        assert any(
            _mapping(rule.get("filter")).get("prefix") == backend.prefix
            and _positive_days(_mapping(rule.get("expiration")).get("days"))
            for rule in enabled
        )
        assert any(
            _mapping(rule.get("filter")).get("prefix") == backend.prefix
            and _positive_days(
                _mapping(rule.get("abortIncompleteMultipartUpload")).get("daysAfterInitiation")
            )
            for rule in enabled
        )
        return
    async with _client(backend, _root_credentials(backend)) as admin:
        encryption = await admin.get_bucket_encryption(Bucket=backend.bucket)
        rules = encryption["ServerSideEncryptionConfiguration"]["Rules"]
        assert rules == [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]
        before = (await admin.get_bucket_lifecycle_configuration(Bucket=backend.bucket))["Rules"]
        assert any(
            rule["Status"] == "Enabled"
            and rule.get("Filter") == {"Prefix": backend.prefix}
            and rule.get("Expiration", {}).get("Days", 0) > 0
            for rule in before
        )
        # Initialization imports the same abort payload OVH carries (row 28). Acceptance is
        # all this shows; reclamation is row 31's observation, never this readback.
        assert any(
            rule["Status"] == "Enabled"
            and rule.get("Filter") == {"Prefix": backend.prefix}
            and rule.get("AbortIncompleteMultipartUpload", {}).get("DaysAfterInitiation", 0) > 0
            for rule in before
        )


def _role_settings(backend: Backend, role: StagingRole) -> Settings:
    """Build application settings for one runtime identity at the real content prefix."""
    key_id, secret = backend.writer if role is StagingRole.WRITER else backend.reader
    return Settings(
        staging_s3_endpoint_url=HttpUrl(backend.endpoint),
        staging_s3_bucket=backend.bucket,
        staging_s3_region=backend.region,
        staging_s3_role=role,
        staging_s3_access_key_id=SecretStr(key_id),
        staging_s3_secret_access_key=SecretStr(secret),
        staging_s3_addressing_style=S3AddressingStyle(backend.addressing),
        staging_s3_verify_tls=backend.endpoint.startswith("https://"),
        staging_s3_content_prefix=backend.prefix,
    )


def _root_credentials(backend: Backend) -> tuple[str, str]:
    """Resolve a local engine's bootstrap identity, used only to inspect or stage fixtures."""
    env = f"CURATE_D_TEST_{backend.name.upper()}_ROOT_"
    root = _LOCAL_ROOT_DEFAULTS.get(backend.name, "")
    return os.environ.get(env + "USER", root), os.environ.get(env + "PASSWORD", root)


def test_rustfs_encryption_provisioning_fails_closed() -> None:
    """Require the encrypted probe that makes RustFS initialization fail closed (row 25).

    RustFS starts without a valid SSE-S3 master key and refuses encrypted writes instead,
    so the probe write and its `AES256` readback are the only thing that fails stack-up.
    """
    script = (_ROOT / "infrastructure/rustfs/initialize.sh").read_text()
    assert "RUSTFS_SSE_S3_MASTER_KEY" in (_ROOT / "compose.yaml").read_text()
    assert "rc bucket encryption set --mode sse-s3" in script
    assert '|| fail "could not set SSE-S3 as the default' in script
    assert 'fail "SSE-S3 is not reported as the default' in script
    assert '|| fail "writer cannot upload; if the server rejects SSE-S3' in script
    assert """'"x-amz-server-side-encryption": "AES256"'""" in script
    assert 'fail "the probe object is not reported as SSE-S3 encrypted"' in script


# Where each backend's rows 30/31 observation is recorded. Only a backend listed here may
# skip without a budget; any other still fails as PENDING.
_RECORDED_LIFECYCLE_OBSERVATIONS = {
    "rustfs": "observed time-scaled by `make lifecycle-check` 2026-09-26",
}


def _observation_budget(row: int, backend: Backend) -> float:
    """Return the wall-clock allowance for observing asynchronous service cleanup.

    Without one, a backend whose observation is already recorded skips and cites it;
    rule acceptance alone is never reclamation evidence, so any other backend fails.
    """
    seconds = float(os.environ.get("CURATE_D_TEST_STAGING_LIFECYCLE_TIMEOUT_SECONDS", "0"))
    if seconds > 0:
        return seconds
    recorded = _RECORDED_LIFECYCLE_OBSERVATIONS.get(backend.name)
    observation_path = os.environ.get("CURATE_D_STAGING_LIFECYCLE_OBSERVATION")
    if recorded is None and backend.name == "ovh" and observation_path:
        recorded = f"observation recorded in {observation_path}"
    if recorded is None:
        _pending(
            row,
            "set CURATE_D_TEST_STAGING_LIFECYCLE_TIMEOUT_SECONDS for a real observation "
            "(minutes via `make lifecycle-check` on RustFS, days elsewhere); "
            "rule acceptance is not reclamation evidence",
        )
    pytest.skip(
        f"staging-compatibility.md row {row} on {backend.name}: {recorded}; set "
        "CURATE_D_TEST_STAGING_LIFECYCLE_TIMEOUT_SECONDS to observe it again"
    )


def _poll_interval(budget: float) -> float:
    """Poll about 60 times per budget: every second in minutes, every 30s over days."""
    return min(30.0, max(1.0, budget / 60))


async def test_expiration_removes_an_aged_object(
    backend: Backend,
    writer: "S3Client",
    staged_key: str,
) -> None:
    """Observe actual expiration of an object created by this test (row 30)."""
    budget = _observation_budget(30, backend)
    await writer.put_object(Bucket=backend.bucket, Key=staged_key, Body=b"aging content")
    head = await writer.head_object(Bucket=backend.bucket, Key=staged_key)
    assert head["ContentLength"] == len(b"aging content")
    assert "Expiration" in head, "the lifecycle rule does not apply to this object"
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        await asyncio.sleep(min(_poll_interval(budget), max(0, deadline - time.monotonic())))
        try:
            await writer.head_object(Bucket=backend.bucket, Key=staged_key)
        except ClientError as exc:
            assert exc.response["Error"]["Code"] in {"404", "NoSuchKey"}
            return
    pytest.fail("row 30: expiration was not observed within the configured observation budget")


async def test_stale_multipart_is_reclaimed(
    backend: Backend,
    writer: "S3Client",
    staged_key: str,
) -> None:
    """Observe residual parts disappear before any explicit abort (row 31)."""
    budget = _observation_budget(31, backend)
    async with _residual_upload(backend, writer, staged_key) as upload_id:
        await writer.upload_part(
            Bucket=backend.bucket,
            Key=staged_key,
            UploadId=upload_id,
            PartNumber=1,
            Body=b"x" * _PART_SIZE,
        )
        assert (await writer.list_parts(Bucket=backend.bucket, Key=staged_key, UploadId=upload_id))[
            "Parts"
        ][0]["Size"] == _PART_SIZE
        deadline = time.monotonic() + budget
        while time.monotonic() < deadline:
            await asyncio.sleep(min(_poll_interval(budget), max(0, deadline - time.monotonic())))
            try:
                await writer.list_parts(Bucket=backend.bucket, Key=staged_key, UploadId=upload_id)
            except ClientError as exc:
                assert exc.response["Error"]["Code"] == "NoSuchUpload"
                return
        pytest.fail("row 31: server/lifecycle reclamation was not observed before timeout")


async def test_explicit_abort_reclaims_residual_parts(
    backend: Backend,
    writer: "S3Client",
    staged_key: str,
) -> None:
    """Verify explicit abort removes real parts independently of lifecycle (rows 12, 31)."""
    async with _residual_upload(backend, writer, staged_key) as upload_id:
        await writer.upload_part(
            Bucket=backend.bucket,
            Key=staged_key,
            UploadId=upload_id,
            PartNumber=1,
            Body=b"x" * _PART_SIZE,
        )
        await writer.abort_multipart_upload(
            Bucket=backend.bucket, Key=staged_key, UploadId=upload_id
        )
        with pytest.raises(ClientError) as failure:
            await writer.list_parts(Bucket=backend.bucket, Key=staged_key, UploadId=upload_id)
        assert failure.value.response["Error"]["Code"] == "NoSuchUpload"


async def test_endpoint_transport_and_certificate_trust(
    backend: Backend,
    writer: "S3Client",
    staged_key: str,
    tmp_path: Path,
) -> None:
    """Verify explicit local HTTP or trusted TLS with certificate rejection (row 37).

    Local engines run plain HTTP on loopback by design, so their row 37 column is not
    applicable; certificate rejection is proven against OVH.
    """
    if not backend.endpoint.startswith("https://"):
        assert backend.name in _LOCAL_ENDPOINTS and backend.endpoint.startswith("http://")
        return
    await writer.put_object(Bucket=backend.bucket, Key=staged_key, Body=b"TLS probe")
    # A valid unrelated CA isolates trust rejection from parsing an invalid PEM file.
    from datetime import UTC, datetime, timedelta

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "unrelated-test-root")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    trust = tmp_path / "unrelated-ca.pem"
    trust.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    async with _client(backend, backend.writer, ca_bundle=str(trust)) as untrusted:
        with pytest.raises(SSLError):
            await untrusted.head_object(Bucket=backend.bucket, Key=staged_key)


def test_matrix_names_required_backend_checks() -> None:
    """Keep required named checks linked to their behavior rows without rewriting evidence."""
    matrix = (_ROOT / "infrastructure/staging-compatibility.md").read_text()
    for row, name in {
        18: "test_reader_bucket_enumeration_matches_recorded_posture",
        20: "test_neither_runtime_identity_can_create_a_bucket",
        26: "test_storage_class_standard_is_accepted",
        30: "test_expiration_removes_an_aged_object",
        31: "test_stale_multipart_is_reclaimed",
    }.items():
        match = re.search(rf"^\| {row} \|.*$", matrix, re.MULTILINE)
        assert match is not None and name in match.group()
        assert name in globals()
