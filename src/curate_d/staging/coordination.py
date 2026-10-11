"""Cross-process bookkeeping on a POSIX staging volume.

The shared filesystem must support advisory flock locks and atomic hard links (including
across its clients). Locks are held on stable inodes: the volume lock is never removed;
a reservation is removed only while holding both its owner lock and the volume lock.
No timeout can mistake a slow, live download for a dead writer.
"""

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO
from uuid import uuid4

from curate_d.staging.keys import require_within_root

WRITES_DIRNAME = ".staging-writes"


@contextmanager
def volume_lock(root: Path) -> Iterator[None]:
    """Serialize admission, publication, lease acquisition and eviction, not downloads."""
    root.mkdir(parents=True, exist_ok=True)
    lock = require_within_root(root, root / ".staging.lock")
    with lock.open("a+b") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


@dataclass
class WriteReservation:
    """An in-flight writer's private bytes, protected by an OS-owned lock."""

    directory: Path
    owner: TextIO

    @property
    def temporary(self) -> Path:
        return self.directory / "content.partial"

    def discard(self) -> None:
        """Remove only this writer's files, under the volume lock; safe to repeat."""
        self.temporary.unlink(missing_ok=True)
        (self.directory / "owner").unlink(missing_ok=True)
        if self.directory.exists():
            self.directory.rmdir()


@contextmanager
def new_reservation(root: Path, needed_bytes: int) -> Iterator[WriteReservation]:
    """Create under the volume lock; retain ownership until caller finishes cleanup."""
    directory = require_within_root(root, root / WRITES_DIRNAME / uuid4().hex)
    directory.mkdir(parents=True)
    with (directory / "owner").open("x+", encoding="ascii") as owner:
        fcntl.flock(owner, fcntl.LOCK_EX)
        try:
            owner.write(str(needed_bytes))
            owner.flush()
            os.fsync(owner.fileno())
            yield WriteReservation(directory, owner)
        finally:
            fcntl.flock(owner, fcntl.LOCK_UN)


def reserved_bytes(root: Path) -> int:
    """Count live reservations and reclaim dead writers under the volume lock.

    A writer may die at any point, including before writing its size or after linking
    its content into place. Delete its private partial before releasing the reservation;
    a published hard link survives that cleanup and is counted by the entry scan.
    """
    directory = require_within_root(root, root / WRITES_DIRNAME)
    if not directory.exists():
        return 0
    total = 0
    for pending in directory.iterdir():
        try:
            owner = (pending / "owner").open("r+", encoding="ascii")
        except FileNotFoundError:
            # Creation died between mkdir and open. No writer can be creating here
            # while we hold the volume lock.
            pending.rmdir()
            continue
        with owner:
            try:
                fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                total += int(owner.read())
            else:
                try:
                    WriteReservation(pending, owner).discard()
                finally:
                    fcntl.flock(owner, fcntl.LOCK_UN)
    return total
