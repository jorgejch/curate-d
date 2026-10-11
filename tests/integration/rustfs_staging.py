"""Staging through the local RustFS engine, for pipelines that should use the real backends.

The writer and reader hold the two identities the worker profiles hold, against the
bucket `make stack-up` provisions; one test process plays both profiles, so both live on
its loop.
"""

import os
from collections.abc import AsyncGenerator, Coroutine
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from pydantic import HttpUrl, SecretStr

from curate_d.config import S3AddressingStyle, Settings, StagingRole
from curate_d.staging.factories import (
    StagingReaderBackend,
    StagingWriterBackend,
    build_reader_backend,
    build_writer_backend,
)

ENDPOINT: Final = os.environ.get("CURATE_D_TEST_RUSTFS_ENDPOINT", "http://127.0.0.1:9100")
BUCKET: Final = os.environ.get("CURATE_D_TEST_RUSTFS_BUCKET", "curate-d-staging")
_WRITER: Final = ("curate-d-staging-writer", "writer-development-secret")
_READER: Final = ("curate-d-staging-reader", "reader-development-secret")


def rustfs_settings(base: Settings, role: StagingRole, scratch: Path) -> Settings:
    """One staging identity against the local RustFS engine."""
    key, secret = _WRITER if role is StagingRole.WRITER else _READER
    return base.model_copy(
        update={
            "staging_s3_endpoint_url": HttpUrl(ENDPOINT),
            "staging_s3_bucket": BUCKET,
            "staging_s3_region": "us-east-1",
            "staging_s3_role": role,
            "staging_s3_access_key_id": SecretStr(key),
            "staging_s3_secret_access_key": SecretStr(secret),
            "staging_s3_addressing_style": S3AddressingStyle.PATH,
            "staging_s3_verify_tls": False,
            "staging_local_scratch_root": scratch,
        }
    )


@dataclass
class S3Staging:
    """Both sides of S3 staging, on the test's own loop."""

    writer: StagingWriterBackend | None
    reader: StagingReaderBackend | None

    async def run[T](self, work: Coroutine[None, None, T]) -> T:
        """Await `work` here: there is only one loop."""
        return await work


@asynccontextmanager
async def rustfs_staging(settings: Settings, scratch: Path) -> AsyncGenerator[S3Staging, None]:
    """The writer and reader against RustFS, closed afterwards."""
    writer = build_writer_backend(rustfs_settings(settings, StagingRole.WRITER, scratch / "w"))
    reader = build_reader_backend(rustfs_settings(settings, StagingRole.READER, scratch / "r"))
    try:
        yield S3Staging(writer=writer, reader=reader)
    finally:
        await writer.aclose()
        await reader.aclose()
