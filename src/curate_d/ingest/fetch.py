"""Fetching a source file's content into local scratch.

The staging store uploads from a synchronous, seekable source; a provider hands content
back as an asynchronous stream. The I/O profile therefore lands the stream on its bounded
local scratch first. The scratch reservation covers the listed size and the stream is cut
off one byte past it, so a file that grew since it was listed cannot fill the disk, and
the caller sees from the fetched size that it is not the file the scan listed.
"""

import tempfile
from collections.abc import AsyncGenerator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Final

from curate_d.providers.access import ProviderFiles
from curate_d.staging.snapshots import LocalScratchBudget

# One byte past the listed size is enough to prove the file is not what was listed.
_OVERRUN_ALLOWANCE: Final = 1


@dataclass(frozen=True)
class Fetched:
    """Fetched content on local scratch, and how many bytes of it arrived."""

    handle: BinaryIO
    size_bytes: int


@contextmanager
def _scratch_file(root: Path) -> Iterator[BinaryIO]:
    """An anonymous file under `root`, removed when closed."""
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryFile(dir=root) as handle:
        yield handle


@asynccontextmanager
async def fetched(
    files: ProviderFiles,
    native_id: str,
    *,
    size_bytes: int,
    scratch_root: Path,
    budget: LocalScratchBudget,
) -> AsyncGenerator[Fetched, None]:
    """Yield a local, seekable copy of a file's content, positioned at the start.

    At most `size_bytes + 1` bytes are kept: enough to show that the file is not the size
    it was listed at, and no more.

    Raises:
        StagingCapacityExhaustedError: local scratch cannot hold the file now.
        ProviderError: the provider could not deliver the content.
        OSError: the scratch file could not be written.
    """
    limit = size_bytes + _OVERRUN_ALLOWANCE
    with budget.reserve(limit), _scratch_file(scratch_root) as handle:
        written = 0
        async with files.open_content(native_id) as chunks:
            async for chunk in chunks:
                room = limit - written
                handle.write(chunk[:room])
                written += min(len(chunk), room)
                if written >= limit:
                    break
        handle.seek(0)
        yield Fetched(handle=handle, size_bytes=written)
