"""Eviction of staged entries, bounded by age and by total size.

The volume is a cache and not a ledger: losing an entry costs a re-fetch and never a
record, so eviction can be aggressive. The one thing it may never do is remove an entry
another process is decoding, which is why every candidate is filtered through the lease
registry before it is considered.
"""

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from curate_d.staging.coordination import (
    WRITES_DIRNAME,
    WriteReservation,
    new_reservation,
    reserved_bytes,
    volume_lock,
)
from curate_d.staging.errors import (
    StagingCapacityExhaustedError,
    StagingCapacityNeverFitsError,
    StagingKeyError,
)
from curate_d.staging.keys import StagingKey, entry_path, parse_staging_key
from curate_d.staging.leases import LEASES_DIRNAME, Clock, LeaseRegistry


@dataclass(frozen=True)
class StagedFile:
    """One entry on the volume, as eviction sees it."""

    key: StagingKey
    path: Path
    size_bytes: int
    staged_at: datetime


@dataclass(frozen=True)
class EvictionOutcome:
    """What a sweep actually removed, for the caller to log or assert on."""

    evicted: tuple[StagingKey, ...]
    freed_bytes: int
    retained_in_use: tuple[StagingKey, ...]


class StagingEvictor:
    """Keeps the staging volume inside its age and size bounds."""

    def __init__(
        self,
        root: Path,
        *,
        max_bytes: int,
        max_age: timedelta,
        leases: LeaseRegistry,
        clock: Clock,
    ) -> None:
        self._root = root
        self._max_bytes = max_bytes
        self._max_age = max_age
        self._leases = leases
        self._clock = clock

    def release(self, key: StagingKey) -> bool:
        """Drop an idle completed entry; return False when absent or still leased."""
        with volume_lock(self._root):
            path = entry_path(self._root, key)
            if not path.is_file() or self._leases.is_in_use(key):
                return False
            path.unlink()
            return True

    def evict_expired(self) -> EvictionOutcome:
        """Remove entries past the age bound that nothing is using."""
        with volume_lock(self._root):
            reserved_bytes(self._root)
            return self._evict_expired()

    def _evict_expired(self) -> EvictionOutcome:
        now = self._clock()
        expired = [entry for entry in self._scan() if now - entry.staged_at >= self._max_age]
        return self._evict(expired)

    def ensure_capacity(self, needed_bytes: int) -> EvictionOutcome:
        """Make room for `needed_bytes`, evicting oldest-first.

        This is a point-in-time check; writers must use `reserve` to retain admission.

        Raises rather than proceeding when the bound cannot be met. The alternatives are
        overwriting an entry another worker is decoding or abandoning the fetch quietly,
        and both lose work while leaving no record that they did.
        """
        with volume_lock(self._root):
            return self._ensure_capacity(needed_bytes)

    @contextmanager
    def reserve(self, needed_bytes: int, *, key: StagingKey) -> Iterator[WriteReservation | None]:
        """Reserve until publish or failure; None means an existing entry needs no write.

        In the existing-entry case the lock stays held across the yield, so the caller
        can describe that entry before eviction runs. Otherwise streaming is unlocked.
        """
        # Enter the ownership context under the volume lock, but stream without the
        # volume lock. ExitStack keeps the owner lock held through final cleanup.
        with ExitStack() as stack:
            with volume_lock(self._root):
                if entry_path(self._root, key).is_file():
                    yield None
                    return
                self._ensure_capacity(needed_bytes)
                reservation = stack.enter_context(new_reservation(self._root, needed_bytes))
            try:
                yield reservation
            finally:
                with volume_lock(self._root):
                    reservation.discard()

    def _ensure_capacity(self, needed_bytes: int) -> EvictionOutcome:
        if needed_bytes < 0:
            raise ValueError("needed_bytes must be nonnegative")
        if needed_bytes > self._max_bytes:
            raise StagingCapacityNeverFitsError(
                f"staging cannot hold {needed_bytes} bytes: the total bound is "
                f"{self._max_bytes} bytes, so this entry will never fit"
            )

        reserved = reserved_bytes(self._root)
        outcome = self._evict_expired()
        used = self._used_bytes() + reserved
        if used + needed_bytes <= self._max_bytes:
            return outcome

        # Oldest first: the least recently fetched entry is the one whose re-fetch is
        # least likely to be needed again shortly.
        surplus = used + needed_bytes - self._max_bytes
        further = self._evict(sorted(self._scan(), key=lambda e: e.staged_at), limit=surplus)
        combined = EvictionOutcome(
            evicted=outcome.evicted + further.evicted,
            freed_bytes=outcome.freed_bytes + further.freed_bytes,
            retained_in_use=tuple(dict.fromkeys(outcome.retained_in_use + further.retained_in_use)),
        )

        remaining = self._used_bytes() + reserved
        if remaining + needed_bytes > self._max_bytes:
            raise StagingCapacityExhaustedError(
                f"staging needs {needed_bytes} bytes but holds {remaining} of "
                f"{self._max_bytes}; {len(combined.retained_in_use)} entries are in use "
                f"and were not evicted"
            )
        return combined

    def entries(self) -> tuple[StagedFile, ...]:
        """Every staged entry currently on the volume."""
        with volume_lock(self._root):
            return tuple(self._scan())

    def _evict(self, candidates: list[StagedFile], *, limit: int | None = None) -> EvictionOutcome:
        evicted: list[StagingKey] = []
        retained: list[StagingKey] = []
        freed = 0
        for entry in candidates:
            if limit is not None and freed >= limit:
                break
            self._leases.prune_expired(entry.key)
            if self._leases.is_in_use(entry.key):
                retained.append(entry.key)
                continue
            # A concurrent sweep may have got there first; the space is free either way.
            with suppress(FileNotFoundError):
                entry.path.unlink()
            evicted.append(entry.key)
            freed += entry.size_bytes
        return EvictionOutcome(
            evicted=tuple(evicted), freed_bytes=freed, retained_in_use=tuple(retained)
        )

    def _used_bytes(self) -> int:
        return sum(entry.size_bytes for entry in self._scan())

    def _scan(self) -> Iterator[StagedFile]:
        if not self._root.is_dir():
            return
        for shard in sorted(self._root.iterdir()):
            if not shard.is_dir() or shard.name in {LEASES_DIRNAME, WRITES_DIRNAME}:
                continue
            for path in sorted(shard.iterdir()):
                entry = _describe(path)
                if entry is not None:
                    yield entry


def _describe(path: Path) -> StagedFile | None:
    """Read one entry's bookkeeping, skipping anything that is not a staged entry."""
    if not path.is_file():
        return None
    try:
        key = parse_staging_key(path.name)
    except StagingKeyError:
        # Not ours. Eviction never deletes a file it cannot name, because the volume may
        # be shared and an unrecognised file is far more likely to be someone else's.
        return None
    stat = path.stat()
    return StagedFile(
        key=key,
        path=path,
        size_bytes=stat.st_size,
        staged_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
    )
