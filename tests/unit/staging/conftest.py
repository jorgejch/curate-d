"""Fixtures for the staging suite.

The clock is injected everywhere rather than read from the wall so that age-bounded
eviction and lease expiry are pinned by the test rather than by how long it took to run.
"""

from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from curate_d.staging.keys import StagingKey, entry_path

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)


class FakeClock:
    """A clock the test moves by hand."""

    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def staging_root(tmp_path: Path) -> Path:
    root = tmp_path / "staging"
    root.mkdir()
    return root


@pytest.fixture
def write_entry(staging_root: Path) -> Iterator[Callable[[StagingKey, bytes], Path]]:
    """Put an entry on the volume directly, bypassing the writer."""

    def _write(key: StagingKey, payload: bytes) -> Path:
        path = entry_path(staging_root, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return path

    yield _write


def key_for(seed: int) -> StagingKey:
    """A valid staging key, distinct per seed, without constructing a SourceObject."""
    return StagingKey(f"{seed:032x}")


def age(path: Path, *, seconds: float, now: datetime = NOW) -> None:
    """Backdate an entry so age-bounded eviction sees it as old."""
    stamp = (now - timedelta(seconds=seconds)).timestamp()
    import os

    os.utime(path, (stamp, stamp))
