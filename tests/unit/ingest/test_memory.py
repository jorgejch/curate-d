"""Hashing and fetching a large file hold one chunk at a time, not the file."""

import hashlib
import tracemalloc
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from pydantic import SecretStr

from curate_d.ingest.fetch import fetched
from curate_d.ingest.hashing import hash_content
from curate_d.providers.pcloud.files import PCloudFiles
from curate_d.staging.snapshots import LocalScratchBudget
from tests.stubs.pcloud import StubPCloud

_SIZE = 48 * 1024 * 1024
# A few download chunks of headroom; far below the file itself.
_BOUND = 12 * 1024 * 1024


@pytest.fixture
def large() -> tuple[StubPCloud, int, bytes]:
    """A stub account holding one large file."""
    pcloud = StubPCloud()
    content = hashlib.sha256(b"seed").digest() * (_SIZE // 32)
    stub = pcloud.file("/video.bin", content)
    return pcloud, stub.file_id, content


@pytest.fixture
async def files(large: tuple[StubPCloud, int, bytes]) -> AsyncIterator[PCloudFiles]:
    """A client for the stub account."""
    pcloud = large[0]
    async with pcloud.client() as http:
        yield PCloudFiles(
            http, api_host=pcloud.host, account="1", access_token=SecretStr(pcloud.access_token)
        )


async def test_hashing_a_large_file_keeps_memory_bounded(
    files: PCloudFiles, large: tuple[StubPCloud, int, bytes]
) -> None:
    """Peak allocation while hashing stays a small fraction of the file."""
    _, file_id, content = large
    tracemalloc.start()
    try:
        digests = await hash_content(files, str(file_id))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert digests.sha256 == hashlib.sha256(content).hexdigest()
    assert peak < _BOUND, peak


async def test_fetching_a_large_file_keeps_memory_bounded(
    files: PCloudFiles, large: tuple[StubPCloud, int, bytes], tmp_path: Path
) -> None:
    """The fetch streams to scratch; memory holds chunks, the disk holds the file."""
    _, file_id, content = large
    tracemalloc.start()
    try:
        async with fetched(
            files,
            str(file_id),
            size_bytes=len(content),
            scratch_root=tmp_path,
            budget=LocalScratchBudget(2 * _SIZE),
        ) as local:
            _, peak = tracemalloc.get_traced_memory()
            assert local.size_bytes == len(content)
    finally:
        tracemalloc.stop()
    assert peak < _BOUND, peak
