"""Age- and size-bounded eviction, and what it must never reclaim."""

from datetime import timedelta
from pathlib import Path

import pytest

from curate_d.staging.errors import StagingCapacityExhaustedError
from curate_d.staging.eviction import StagingEvictor
from curate_d.staging.keys import entry_path
from curate_d.staging.leases import LeaseRegistry
from tests.unit.staging.conftest import FakeClock, age, key_for

MAX_BYTES = 100
MAX_AGE = timedelta(hours=1)


@pytest.fixture
def leases(staging_root: Path, clock: FakeClock) -> LeaseRegistry:
    return LeaseRegistry(staging_root, ttl=timedelta(minutes=30), clock=clock)


@pytest.fixture
def evictor(staging_root: Path, leases: LeaseRegistry, clock: FakeClock) -> StagingEvictor:
    return StagingEvictor(
        staging_root, max_bytes=MAX_BYTES, max_age=MAX_AGE, leases=leases, clock=clock
    )


def _stage(staging_root: Path, seed: int, size: int, *, seconds_old: float = 0.0) -> Path:
    key = key_for(seed)
    path = entry_path(staging_root, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    age(path, seconds=seconds_old)
    return path


def test_a_completed_entry_is_removed(evictor: StagingEvictor, staging_root: Path) -> None:
    path = _stage(staging_root, 1, 10)
    assert evictor.release(key_for(1)) is True
    assert not path.exists()


def test_releasing_an_absent_entry_reports_rather_than_raises(evictor: StagingEvictor) -> None:
    """Release is idempotent: a retried workflow must not fail on its second pass."""
    assert evictor.release(key_for(99)) is False


def test_entries_past_the_age_bound_are_evicted(
    evictor: StagingEvictor, staging_root: Path
) -> None:
    old = _stage(staging_root, 1, 10, seconds_old=MAX_AGE.total_seconds() + 1)
    fresh = _stage(staging_root, 2, 10, seconds_old=0)

    outcome = evictor.evict_expired()

    assert outcome.evicted == (key_for(1),)
    assert not old.exists()
    assert fresh.exists()


def test_eviction_under_pressure_never_removes_an_entry_in_use(
    evictor: StagingEvictor, leases: LeaseRegistry, staging_root: Path
) -> None:
    """The one thing eviction may not do: reclaim bytes a decoder is mid-way through."""
    in_use = _stage(staging_root, 1, 60, seconds_old=MAX_AGE.total_seconds() + 1)
    idle = _stage(staging_root, 2, 30, seconds_old=MAX_AGE.total_seconds() + 1)

    with leases.hold(key_for(1)):
        outcome = evictor.evict_expired()

    assert in_use.exists()
    assert not idle.exists()
    assert outcome.retained_in_use == (key_for(1),)


def test_capacity_is_made_by_evicting_oldest_first(
    evictor: StagingEvictor, staging_root: Path
) -> None:
    oldest = _stage(staging_root, 1, 50, seconds_old=30)
    newer = _stage(staging_root, 2, 40, seconds_old=10)

    evictor.ensure_capacity(50)

    assert not oldest.exists()
    assert newer.exists()


def test_capacity_within_the_bound_evicts_nothing(
    evictor: StagingEvictor, staging_root: Path
) -> None:
    kept = _stage(staging_root, 1, 10, seconds_old=5)
    outcome = evictor.ensure_capacity(20)

    assert outcome.evicted == ()
    assert kept.exists()


def test_exhaustion_by_entries_in_use_is_an_explicit_resource_error(
    evictor: StagingEvictor, leases: LeaseRegistry, staging_root: Path
) -> None:
    """Overwriting an entry another worker is decoding, or dropping the fetch quietly,
    both lose work with no record. Raising is the only honest option left."""
    _stage(staging_root, 1, 90, seconds_old=5)

    with (
        leases.hold(key_for(1)),
        pytest.raises(StagingCapacityExhaustedError, match="in use"),
    ):
        evictor.ensure_capacity(50)


def test_an_entry_larger_than_the_whole_bound_is_refused_immediately(
    evictor: StagingEvictor,
) -> None:
    """No amount of eviction makes this fit, so it must not evict anything trying."""
    with pytest.raises(StagingCapacityExhaustedError, match="will never fit"):
        evictor.ensure_capacity(MAX_BYTES + 1)


def test_an_expired_lease_stops_pinning_its_entry(
    evictor: StagingEvictor,
    leases: LeaseRegistry,
    staging_root: Path,
    clock: FakeClock,
) -> None:
    """A decoder killed mid-read leaves its lease behind; it must not pin forever."""
    entry = _stage(staging_root, 1, 90, seconds_old=5)
    lease_dir = staging_root / "leases" / key_for(1)
    lease_dir.mkdir(parents=True)
    (lease_dir / "abandoned").write_text(clock().isoformat(), encoding="ascii")

    clock.advance(timedelta(hours=2))
    evictor.ensure_capacity(50)

    assert not entry.exists()


def test_a_file_eviction_cannot_name_is_left_alone(
    evictor: StagingEvictor, staging_root: Path
) -> None:
    """The volume may be shared; an unrecognised file is more likely someone else's."""
    stranger = staging_root / "ab" / "not-a-staging-key"
    stranger.parent.mkdir(parents=True)
    stranger.write_bytes(b"x" * 10)

    evictor.evict_expired()

    assert stranger.exists()
    assert evictor.entries() == ()
