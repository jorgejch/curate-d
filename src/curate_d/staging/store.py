"""The filesystem staging volume, split into a write side and a read side.

Superseded by S3 staging: no worker profile builds these classes. `factories` builds the
S3 backends and has no filesystem fallback.

The split is the security boundary. The credential-holding I/O profile constructs
`ContentStagingArea` and writes fetched bytes; the decoding CPU profile constructs
`StagedContentReader` and does nothing but read. The reader holds no provider client and
has no constructor parameter through which one could be supplied, so a missing entry
cannot be resolved by re-fetching — it raises. That is the point: a silent re-fetch here
would mean a live credential had been handed to the one process that decodes
attacker-influenced bytes, which is the exposure the profile split exists to remove.
"""

import os
from collections.abc import Iterable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from curate_d.staging.coordination import volume_lock
from curate_d.staging.errors import (
    StagedContentUnreadableError,
    StagedEntryMissingError,
    StagingWriteError,
)
from curate_d.staging.eviction import StagingEvictor
from curate_d.staging.keys import StagingKey, entry_path
from curate_d.staging.leases import LeaseRegistry


@dataclass(frozen=True)
class StagedEntry:
    """One entry as the writer just left it."""

    key: StagingKey
    path: Path
    size_bytes: int
    staged_at: datetime


class StagedContentReader:
    """Read side of the staging volume.

    Constructed with a root and a lease registry, and nothing else. There is deliberately
    no provider, client, or credential parameter: the absence is the guarantee.
    """

    def __init__(self, root: Path, *, leases: LeaseRegistry) -> None:
        self._root = root
        self._leases = leases

    @contextmanager
    def open(self, key: StagingKey) -> Iterator[BinaryIO]:
        """Open a staged entry for reading, holding a use lease for the duration.

        The lease is what stops eviction reclaiming these bytes halfway through a decode.
        """
        path = entry_path(self._root, key)
        if not path.is_file():
            raise StagedEntryMissingError(
                f"staged entry {key} is not present at {path}; the read path holds no "
                f"provider credential and cannot fetch it"
            )
        with self._leases.hold(key):
            try:
                handle = path.open("rb")
            except OSError as error:
                raise StagedContentUnreadableError(
                    f"staged entry {key} at {path} could not be opened for reading: {error}"
                ) from error
            with handle:
                yield handle


class ContentStagingArea:
    """Write side of the staging volume. Only the credential-holding profile builds one."""

    def __init__(self, root: Path, *, evictor: StagingEvictor) -> None:
        self._root = root
        self._evictor = evictor

    def stage(
        self, key: StagingKey, chunks: Iterable[bytes], *, expected_bytes: int
    ) -> StagedEntry:
        """Write fetched bytes under `key`, returning the entry.

        `expected_bytes` is admission control: capacity is settled before a single byte is
        read. It is an upper bound: a stream exceeding it fails before excess bytes are
        written; a shorter stream releases unused capacity when published.

        Re-staging an entry that is already present is a no-op, which is what makes an
        interrupted fetch safe to retry.
        """
        if expected_bytes < 0:
            raise ValueError("expected_bytes must be nonnegative")
        path = entry_path(self._root, key)
        try:
            with self._evictor.reserve(expected_bytes, key=key) as reservation:
                if reservation is None:
                    return _describe_entry(key, path)
                written = 0
                with reservation.temporary.open("xb") as handle:
                    for chunk in chunks:
                        if written + len(chunk) > expected_bytes:
                            raise StagingWriteError(
                                f"staging entry {key} exceeds its {expected_bytes}-byte reservation"
                            )
                        handle.write(chunk)
                        written += len(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())

                with volume_lock(self._root):
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with suppress(FileExistsError):
                        # Link is atomic and cannot replace a concurrent winner. The
                        # private inode is closed before any reader can observe it.
                        os.link(reservation.temporary, path)
                    # Convert reserved bytes to published bytes within the same lock.
                    reservation.discard()
                    return _describe_entry(key, path)
        except OSError as error:
            raise StagingWriteError(f"staging entry {key} to {path} failed: {error}") from error


def _describe_entry(key: StagingKey, path: Path) -> StagedEntry:
    stat = path.stat()
    return StagedEntry(
        key=key,
        path=path,
        size_bytes=stat.st_size,
        staged_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
    )
