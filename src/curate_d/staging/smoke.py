"""Verify the staging access matrix with both provisioning-time identities.

Keep this command out of worker readiness: each worker holds only its own identity.
Use the application adapter for every request, including deliberately attempting writes
with the reader credential. Emit fixed diagnostics rather than backend exception text or
configuration values, because either can contain credentials before validation succeeds.
"""

import argparse
import asyncio
import hashlib
import io
import os
import sys
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from functools import partial
from typing import Never
from uuid import uuid4

from pydantic import SecretStr

from curate_d.config import Settings, StagingRole, get_settings
from curate_d.observability import configure_logging, get_logger
from curate_d.staging import factories
from curate_d.staging.errors import StagedEntryMissingError, StagingAccessDeniedError
from curate_d.staging.keys import StagingKey
from curate_d.staging.object_store import S3StagingObjectStore
from curate_d.staging.objects import StagedObject, StagingObjectStore

EXIT_OK = 0
EXIT_CHECK_FAILED = 1
EXIT_NOT_RUN = 2
_READER_KEY_VAR = "CURATE_D_STAGING_SMOKE_READER_ACCESS_KEY_ID"
_READER_SECRET_VAR = "CURATE_D_STAGING_SMOKE_READER_SECRET_ACCESS_KEY"
_PAYLOAD = b"curate-d staging access-matrix probe\n"


class SmokeCheck(StrEnum):
    """Name the operations required to prove the provisioning access matrix."""

    WRITER_UPLOAD = "writer_upload"
    READER_GET = "reader_get"
    INTEGRITY = "integrity"
    WRITER_DELETE = "writer_delete"
    READER_WRITE_DENIED = "reader_write_denied"
    READER_DELETE_DENIED = "reader_delete_denied"
    READER_CROSS_BUCKET_DENIED = "reader_cross_bucket_denied"
    WRITER_CROSS_BUCKET_DENIED = "writer_cross_bucket_denied"
    READINESS_MARKER = "readiness_marker"
    CLEANUP = "cleanup"


@dataclass(frozen=True)
class SmokeCheckOutcome:
    """Record one check using diagnostics that contain no backend-supplied values."""

    check: SmokeCheck
    passed: bool
    detail: str


@dataclass(frozen=True)
class SmokeReport:
    """Collect all attempted checks and any cleanup failures."""

    outcomes: tuple[SmokeCheckOutcome, ...]

    @property
    def passed(self) -> bool:
        """Return whether every required check ran and all outcomes passed."""
        required = set(SmokeCheck) - {SmokeCheck.CLEANUP}
        return required <= {item.check for item in self.outcomes} and all(
            item.passed for item in self.outcomes
        )

    @property
    def exit_code(self) -> int:
        """Return zero only for a complete successful access matrix."""
        return EXIT_OK if self.passed else EXIT_CHECK_FAILED


@dataclass(frozen=True, kw_only=True)
class SmokeStores:
    """Hold protocol-based stores for each identity and bucket combination.

    The reader-as-writer store declares a writer role but uses the reader credential.
    This provisioning-only override makes PUT and DELETE reach the backend instead of
    passing the denial check at the adapter's local role guard.
    """

    writer: StagingObjectStore
    reader: StagingObjectStore
    reader_as_writer: StagingObjectStore
    writer_cross_bucket: StagingObjectStore
    reader_cross_bucket: StagingObjectStore
    publish_readiness_marker: Callable[[], Awaitable[None]]


def build_smoke_stores(
    *, writer_settings: Settings, reader_settings: Settings, cross_bucket: str
) -> SmokeStores:
    """Build stores carrying distinct identities on the same configured backend.

    Args:
        cross_bucket: Existing unrelated disposable bucket neither identity may access.
            Provision it separately; this command has no bucket-management authority.

    Raises:
        ValueError: Identities, roles, backend settings or cross-bucket scope disagree,
            or the content prefix is empty.
    """
    writer = writer_settings
    reader = reader_settings
    if not writer.s3_staging_enabled or not reader.s3_staging_enabled:
        raise ValueError("both identities require complete S3 configuration")
    # This path builds stores directly rather than through the worker factories, so it
    # restates their prefix guarantee instead of inheriting it. An empty prefix makes
    # the adapter's `key.startswith(prefix)` scope check match every key in the bucket.
    # The backend comparison below then holds it for the reader too.
    if not writer.staging_s3_content_prefix:
        raise ValueError("staging checks require a nonempty content prefix")
    if writer.staging_s3_role is not StagingRole.WRITER:
        raise ValueError("writer settings must declare the writer role")
    if reader.staging_s3_role is not StagingRole.READER:
        raise ValueError("reader settings must declare the reader role")
    if writer.staging_s3_access_key_id == reader.staging_s3_access_key_id:
        raise ValueError("writer and reader access keys must be distinct")
    writer_backend = writer.model_dump(
        exclude={"staging_s3_role", "staging_s3_access_key_id", "staging_s3_secret_access_key"}
    )
    reader_backend = reader.model_dump(
        exclude={"staging_s3_role", "staging_s3_access_key_id", "staging_s3_secret_access_key"}
    )
    if any(
        writer_backend[field] != reader_backend[field]
        for field in writer_backend
        if field.startswith("staging_s3_")
    ):
        raise ValueError("both identities must use the same backend configuration")
    if not cross_bucket or cross_bucket == writer.staging_s3_bucket:
        raise ValueError("cross-bucket checks require a different existing bucket")

    async def publish_marker() -> None:
        """Seed the shared readiness marker with the writer identity after a full pass."""
        await factories.refresh_readiness_marker(writer)

    return SmokeStores(
        writer=S3StagingObjectStore(writer),
        reader=S3StagingObjectStore(reader),
        reader_as_writer=S3StagingObjectStore(
            reader.model_copy(update={"staging_s3_role": StagingRole.WRITER})
        ),
        writer_cross_bucket=S3StagingObjectStore(
            writer.model_copy(update={"staging_s3_bucket": cross_bucket})
        ),
        reader_cross_bucket=S3StagingObjectStore(
            reader.model_copy(update={"staging_s3_bucket": cross_bucket})
        ),
        publish_readiness_marker=publish_marker,
    )


async def _attempt(
    check: SmokeCheck,
    operation: Callable[[], Awaitable[None]],
    *,
    expect_denial: bool = False,
) -> SmokeCheckOutcome:
    """Classify one request without allowing exceptions to leak backend text."""
    try:
        await operation()
    except StagingAccessDeniedError:
        return SmokeCheckOutcome(check, expect_denial, "access denied")
    except StagedEntryMissingError:
        return SmokeCheckOutcome(check, False, "resource missing; denial unproven")
    except Exception:
        return SmokeCheckOutcome(check, False, "request failed; inspect backend privately")
    return SmokeCheckOutcome(check, not expect_denial, "request succeeded")


async def run_smoke_checks(stores: SmokeStores) -> SmokeReport:
    """Exercise every grant and denial and clean up even after failed requests.

    Return a failed outcome for each dependent check that could not run. Cleanup covers
    attempted uploads too: an upload may succeed remotely before response verification
    fails. Each run uses fresh immutable attempt keys and never registers cache entries.
    """
    target = StagedObject(
        key=StagingKey(uuid4().hex),
        attempt_id=uuid4(),
        size_bytes=len(_PAYLOAD),
        sha256=hashlib.sha256(_PAYLOAD).hexdigest(),
        created_at=datetime.now(UTC),
    )
    denied_target = replace(target, attempt_id=uuid4())
    staged = target
    outcomes: list[SmokeCheckOutcome] = []
    cleanup_keys: set[str] = set()

    async def upload() -> None:
        """Upload the writer probe and retain the verified registration."""
        nonlocal staged
        cleanup_keys.add(target.object_key)
        staged = await stores.writer.upload(
            target, io.BytesIO(_PAYLOAD), declared_max_bytes=len(_PAYLOAD)
        )

    async def reader_upload() -> None:
        """Attempt a reader PUT without letting cleanup masquerade as denial."""
        cleanup_keys.add(denied_target.object_key)
        await stores.reader_as_writer.upload(
            denied_target, io.BytesIO(_PAYLOAD), declared_max_bytes=len(_PAYLOAD)
        )

    try:
        uploaded = await _attempt(SmokeCheck.WRITER_UPLOAD, upload)
        outcomes.append(uploaded)
        if uploaded.passed:
            body = io.BytesIO()
            retrieved = await _attempt(
                SmokeCheck.READER_GET, lambda: stores.reader.download(staged, body)
            )
            outcomes.append(retrieved)
            integrity = (
                retrieved.passed
                and body.getvalue() == _PAYLOAD
                and staged.size_bytes == len(_PAYLOAD)
                and staged.sha256 == target.sha256
            )
            outcomes.append(
                SmokeCheckOutcome(
                    SmokeCheck.INTEGRITY,
                    integrity,
                    "size and SHA-256 match" if integrity else "integrity unproven or mismatched",
                )
            )
        else:
            outcomes.extend(
                SmokeCheckOutcome(check, False, "not run: writer upload failed")
                for check in (SmokeCheck.READER_GET, SmokeCheck.INTEGRITY)
            )

        write_denied = await _attempt(
            SmokeCheck.READER_WRITE_DENIED, reader_upload, expect_denial=True
        )
        if write_denied.passed:
            # upload() includes HEAD verification. A denied HEAD after an accepted PUT
            # must not be mistaken for a denied write, so inspect that key as writer.
            try:
                await stores.writer.download(denied_target, io.BytesIO())
            except StagedEntryMissingError:
                pass
            except Exception:
                write_denied = SmokeCheckOutcome(
                    SmokeCheck.READER_WRITE_DENIED,
                    False,
                    "reader PUT absence could not be verified",
                )
            else:
                write_denied = SmokeCheckOutcome(
                    SmokeCheck.READER_WRITE_DENIED, False, "reader PUT created an object"
                )
        outcomes.append(write_denied)
        # Probe the existing writer object before deleting it, so a backend's absent-key
        # handling cannot hide an overly broad reader delete grant.
        outcomes.append(
            await _attempt(
                SmokeCheck.READER_DELETE_DENIED,
                lambda: stores.reader_as_writer.delete(target.object_key),
                expect_denial=True,
            )
        )
        for check, store in (
            (SmokeCheck.READER_CROSS_BUCKET_DENIED, stores.reader_cross_bucket),
            (SmokeCheck.WRITER_CROSS_BUCKET_DENIED, stores.writer_cross_bucket),
        ):
            outcomes.append(
                await _attempt(
                    check,
                    partial(store.download, target, io.BytesIO()),
                    expect_denial=True,
                )
            )
        deleted = await _attempt(
            SmokeCheck.WRITER_DELETE, lambda: stores.writer.delete(target.object_key)
        )
        if not uploaded.passed:
            deleted = SmokeCheckOutcome(
                SmokeCheck.WRITER_DELETE, False, "not proven: writer upload failed"
            )
        elif deleted.passed:
            cleanup_keys.discard(target.object_key)
        outcomes.append(deleted)
    finally:
        for key in sorted(cleanup_keys):
            cleanup = await _attempt(SmokeCheck.CLEANUP, partial(stores.writer.delete, key))
            if not cleanup.passed:
                outcomes.append(cleanup)
    if all(outcome.passed for outcome in outcomes):
        outcomes.append(
            await _attempt(SmokeCheck.READINESS_MARKER, stores.publish_readiness_marker)
        )
    else:
        outcomes.append(
            SmokeCheckOutcome(
                SmokeCheck.READINESS_MARKER, False, "not run: access matrix or cleanup failed"
            )
        )
    return SmokeReport(tuple(outcomes))


def _reader_settings(writer: Settings) -> Settings:
    """Overlay the provisioning-only reader credential from explicit variables."""
    access_key = os.environ.get(_READER_KEY_VAR, "")
    secret_key = os.environ.get(_READER_SECRET_VAR, "")
    if not access_key or not secret_key:
        raise ValueError("both smoke-reader credential variables are required")
    return writer.model_copy(
        update={
            "staging_s3_role": StagingRole.READER,
            "staging_s3_access_key_id": SecretStr(access_key),
            "staging_s3_secret_access_key": SecretStr(secret_key),
        }
    )


class _SafeArgumentParser(argparse.ArgumentParser):
    """Reject invalid arguments without echoing values that may contain credentials."""

    def error(self, message: str) -> Never:
        """Emit fixed usage diagnostics and the standard parser failure status."""
        self.exit(EXIT_NOT_RUN, "invalid arguments; use --help for staging smoke usage\n")


def main(
    argv: Sequence[str] | None = None,
    *,
    settings: Settings | None = None,
    stores: SmokeStores | None = None,
) -> int:
    """Run the provisioning checks and return their process exit status.

    Args:
        settings: Writer configuration, normally loaded from the environment.
        stores: Protocol-based substitutes for isolated command tests.

    Returns:
        Zero for full success, one for a failed check, or two for invalid configuration
        or an invocation that could not run. Argument parsing exits directly with two.
    """
    parser = _SafeArgumentParser(
        prog="python -m curate_d.staging.smoke",
        description="Verify staging grants with both identities; zero requires all checks to pass.",
    )
    parser.add_argument(
        "--cross-bucket",
        required=True,
        help="Existing unrelated disposable bucket denied to both staging identities.",
    )
    parser.add_argument("--log-format", choices=("json", "console"), default="json")
    invocation = parser.parse_args(argv)
    configure_logging(json_output=invocation.log_format == "json")
    logger = get_logger(__name__)
    try:
        if stores is None:
            writer = settings if settings is not None else get_settings()
            stores = build_smoke_stores(
                writer_settings=writer,
                reader_settings=_reader_settings(writer),
                cross_bucket=invocation.cross_bucket,
            )
        report = asyncio.run(run_smoke_checks(stores))
    except (Exception, asyncio.CancelledError, KeyboardInterrupt):
        # ValidationError can include raw input dictionaries before SecretStr exists.
        # No exception, configuration, command-line value or traceback reaches output.
        logger.error("staging.smoke.not_run", detail="check configuration and backend privately")
        return EXIT_NOT_RUN
    for outcome in report.outcomes:
        logger.info(
            "staging.smoke.check",
            check=outcome.check.value,
            passed=outcome.passed,
            detail=outcome.detail,
        )
    logger.info("staging.smoke.result", passed=report.passed, exit_code=report.exit_code)
    return report.exit_code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
