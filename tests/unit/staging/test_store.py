"""The write side, the read side, and the boundary between them."""

import inspect
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest

from curate_d.staging import store
from curate_d.staging.coordination import WRITES_DIRNAME
from curate_d.staging.errors import (
    StagedEntryMissingError,
    StagingPathEscapeError,
    StagingWriteError,
)
from curate_d.staging.eviction import StagingEvictor
from curate_d.staging.keys import entry_path
from curate_d.staging.leases import LeaseRegistry
from curate_d.staging.store import ContentStagingArea, StagedContentReader
from tests.unit.staging.conftest import FakeClock, key_for


@pytest.fixture
def leases(staging_root: Path, clock: FakeClock) -> LeaseRegistry:
    return LeaseRegistry(staging_root, ttl=timedelta(minutes=30), clock=clock)


@pytest.fixture
def evictor(staging_root: Path, leases: LeaseRegistry, clock: FakeClock) -> StagingEvictor:
    return StagingEvictor(
        staging_root,
        max_bytes=1024,
        max_age=timedelta(hours=1),
        leases=leases,
        clock=clock,
    )


@pytest.fixture
def area(staging_root: Path, evictor: StagingEvictor) -> ContentStagingArea:
    return ContentStagingArea(staging_root, evictor=evictor)


@pytest.fixture
def reader(staging_root: Path, leases: LeaseRegistry) -> StagedContentReader:
    return StagedContentReader(staging_root, leases=leases)


def test_staged_bytes_are_readable_by_the_read_path(
    area: ContentStagingArea, reader: StagedContentReader
) -> None:
    key = key_for(1)
    area.stage(key, [b"hello ", b"world"], expected_bytes=11)

    with reader.open(key) as handle:
        assert handle.read() == b"hello world"


def test_a_missing_entry_fails_explicitly_rather_than_re_fetching(
    reader: StagedContentReader,
) -> None:
    """The decoding profile holds no credential, so it must not paper this over."""
    with (
        pytest.raises(StagedEntryMissingError, match="holds no provider credential"),
        reader.open(key_for(2)),
    ):
        pass


def test_the_read_path_takes_no_provider_credential() -> None:
    """Structural, not conventional: there is no parameter through which one could arrive."""
    parameters = set(inspect.signature(StagedContentReader.__init__).parameters) - {"self"}
    assert parameters == {"root", "leases"}


def test_the_staging_package_does_not_import_any_provider_code() -> None:
    """A provider import on the read path would reintroduce exactly the exposure the
    profile split exists to remove, so it is checked rather than trusted."""
    staging_dir = Path(store.__file__).parent
    offenders = [
        path.name
        for path in staging_dir.glob("*.py")
        if "curate_d.providers" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_re_staging_an_existing_entry_does_not_rewrite_it(area: ContentStagingArea) -> None:
    """An interrupted fetch is retried by re-running stage; that must be a no-op."""
    key = key_for(3)
    first = area.stage(key, [b"original"], expected_bytes=8)
    second = area.stage(key, [b"replacement"], expected_bytes=11)

    assert second == first
    assert first.path.read_bytes() == b"original"


def test_a_failed_write_leaves_no_partial_entry(
    area: ContentStagingArea, staging_root: Path
) -> None:
    """A partial entry read as complete would be decoded as truncated content."""
    key = key_for(4)

    def exploding_chunks() -> Iterator[bytes]:
        yield b"da"
        raise OSError("provider stream died")

    with pytest.raises(StagingWriteError, match="provider stream died"):
        area.stage(key, exploding_chunks(), expected_bytes=4)

    assert not entry_path(staging_root, key).exists()
    assert list((staging_root / WRITES_DIRNAME).iterdir()) == []


def test_the_reader_refuses_an_entry_that_escapes_the_root(
    reader: StagedContentReader, staging_root: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "elsewhere"
    outside.write_bytes(b"not ours")
    key = key_for(5)
    planted = staging_root / key[:2] / key
    planted.parent.mkdir(parents=True)
    planted.symlink_to(outside)

    with pytest.raises(StagingPathEscapeError), reader.open(key):
        pass
