"""Use leases over staged entries.

Eviction and decoding run in different pods against one shared volume, so "is anything
using this entry right now" cannot be answered from process memory. A lease is therefore
a file: the volume is the only thing both processes can see.

Leases expire. A decoder killed mid-read leaves its lease behind, and a lease that never
expired would pin that entry until an operator noticed.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

from curate_d.staging.coordination import volume_lock
from curate_d.staging.errors import StagingLeaseError
from curate_d.staging.keys import StagingKey

type Clock = Callable[[], datetime]

LEASES_DIRNAME = "leases"


class LeaseRegistry:
    """Records which staged entries are in use, on the volume itself."""

    def __init__(self, root: Path, *, ttl: timedelta, clock: Clock) -> None:
        self._dir = root / LEASES_DIRNAME
        self._ttl = ttl
        self._clock = clock

    @contextmanager
    def hold(self, key: StagingKey) -> Iterator[None]:
        """Mark `key` in use for the duration of the block.

        The lease is released even when the body raises: a failed decode that left its
        entry pinned would leak capacity on every failure.
        """
        lease = self._dir / key / uuid4().hex
        try:
            with volume_lock(self._dir.parent):
                lease.parent.mkdir(parents=True, exist_ok=True)
                lease.write_text(self._clock().isoformat(), encoding="ascii")
        except OSError as error:
            raise StagingLeaseError(
                f"could not take a use lease on staged entry {key} at {lease}: {error}"
            ) from error
        try:
            yield
        finally:
            lease.unlink(missing_ok=True)

    def is_in_use(self, key: StagingKey) -> bool:
        """Whether any unexpired lease is held on `key`."""
        return any(self._live_leases(key))

    def prune_expired(self, key: StagingKey) -> None:
        """Drop lease files left behind by processes that died holding them."""
        for lease, held_at in self._leases(key):
            if held_at is None or self._is_expired(held_at):
                lease.unlink(missing_ok=True)

    def _live_leases(self, key: StagingKey) -> Iterator[Path]:
        for lease, held_at in self._leases(key):
            # An unreadable or malformed lease counts as live. The alternative is
            # evicting an entry a running decoder is reading because its bookkeeping
            # was unparseable, which trades a recoverable stall for corrupt output.
            if held_at is None or not self._is_expired(held_at):
                yield lease

    def _leases(self, key: StagingKey) -> Iterator[tuple[Path, datetime | None]]:
        key_dir = self._dir / key
        if not key_dir.is_dir():
            return
        for lease in sorted(key_dir.iterdir()):
            if lease.is_file():
                yield lease, _read_held_at(lease)

    def _is_expired(self, held_at: datetime) -> bool:
        return self._clock() - held_at >= self._ttl


def _read_held_at(lease: Path) -> datetime | None:
    """When the lease was taken, or None if that cannot be established."""
    try:
        return datetime.fromisoformat(lease.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None
