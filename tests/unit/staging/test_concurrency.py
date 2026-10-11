"""Regression tests using independent processes against one shared staging volume."""

from __future__ import annotations

import multiprocessing
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from pathlib import Path

import pytest

from curate_d.staging.coordination import WRITES_DIRNAME
from curate_d.staging.errors import StagingCapacityExhaustedError, StagingWriteError
from curate_d.staging.eviction import StagingEvictor
from curate_d.staging.keys import entry_path
from curate_d.staging.leases import LeaseRegistry
from curate_d.staging.store import ContentStagingArea, StagedContentReader
from tests.unit.staging.conftest import FakeClock, key_for


def _services(root: Path, maximum: int) -> tuple[ContentStagingArea, StagingEvictor, LeaseRegistry]:
    clock = FakeClock()
    leases = LeaseRegistry(root, ttl=timedelta(minutes=30), clock=clock)
    evictor = StagingEvictor(
        root, max_bytes=maximum, max_age=timedelta(hours=1), leases=leases, clock=clock
    )
    return ContentStagingArea(root, evictor=evictor), evictor, leases


def _writer(
    root: Path,
    seed: int,
    payload: bytes,
    expected: int,
    maximum: int,
    connection: Connection[object, object],
    crash_after_link: bool,
) -> None:
    area, _, _ = _services(root, maximum)

    def chunks() -> Iterator[bytes]:
        yield payload[:1]
        connection.send("streaming")
        command = connection.recv()
        if command == "fail":
            raise RuntimeError("provider interrupted")
        yield payload[1:]

    if crash_after_link:
        original_link = os.link

        def link_and_die(source: Path, destination: Path) -> None:
            original_link(source, destination)
            os._exit(7)

        os.link = link_and_die  # type: ignore[assignment]
    try:
        entry = area.stage(key_for(seed), chunks(), expected_bytes=expected)
        connection.send(("done", entry.size_bytes))
    except (StagingCapacityExhaustedError, StagingWriteError, RuntimeError) as error:
        connection.send(("error", type(error).__name__))
    finally:
        connection.close()


@contextmanager
def _running_writer(
    root: Path,
    seed: int,
    payload: bytes,
    *,
    expected: int | None = None,
    maximum: int = 100,
    crash_after_link: bool = False,
) -> Iterator[tuple[BaseProcess, Connection[object, object]]]:
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(
        target=_writer,
        args=(
            root,
            seed,
            payload,
            len(payload) if expected is None else expected,
            maximum,
            child,
            crash_after_link,
        ),
    )
    process.start()
    child.close()
    try:
        yield process, parent
    finally:
        if process.is_alive():
            process.terminate()
        process.join(timeout=10)
        parent.close()
        assert not process.is_alive()


def _streaming(connection: Connection[object, object]) -> None:
    assert connection.poll(10), "writer never started its stream"
    assert connection.recv() == "streaming"


def _finished(connection: Connection[object, object], size: int) -> None:
    connection.send("finish")
    assert connection.poll(10), "writer did not finish"
    assert connection.recv() == ("done", size)


@pytest.mark.parametrize("second_fails", [False, True])
def test_same_key_writers_never_mutate_a_published_inode(
    staging_root: Path, second_fails: bool
) -> None:
    area, _, leases = _services(staging_root, 100)
    reader = StagedContentReader(staging_root, leases=leases)
    with (
        _running_writer(staging_root, 1, b"AAAA") as (_, first),
        _running_writer(staging_root, 1, b"BBBBBBBB") as (_, second),
    ):
        _streaming(first)
        _streaming(second)
        _finished(first, 4)
        with reader.open(key_for(1)) as handle:
            original_inode = os.fstat(handle.fileno()).st_ino
            if second_fails:
                second.send("fail")
                assert second.poll(10)
                assert second.recv() == ("error", "RuntimeError")
            else:
                _finished(second, 4)
            assert handle.read() == b"AAAA"
            assert entry_path(staging_root, key_for(1)).stat().st_ino == original_inode
        assert area.stage(key_for(1), [], expected_bytes=0).path.read_bytes() == b"AAAA"
        assert list((staging_root / WRITES_DIRNAME).iterdir()) == []


def test_in_flight_bytes_are_reserved_before_consuming_another_stream(staging_root: Path) -> None:
    area, evictor, _ = _services(staging_root, 10)
    with _running_writer(staging_root, 1, b"123456", maximum=10) as (_, first):
        _streaming(first)
        with _running_writer(staging_root, 2, b"abcdef", maximum=10) as (_, second):
            assert second.poll(10)
            assert second.recv() == ("error", "StagingCapacityExhaustedError")
        # Even an ordinary capacity check sees the outstanding reservation.
        with pytest.raises(StagingCapacityExhaustedError):
            evictor.ensure_capacity(6)
        # Different keys can stream concurrently when their reservations fit.
        area.stage(key_for(3), [b"abcd"], expected_bytes=4)
        _finished(first, 6)
    assert sum(entry.size_bytes for entry in evictor.entries()) == 10


def test_killed_writer_releases_capacity_and_only_its_own_partial(staging_root: Path) -> None:
    area, evictor, _ = _services(staging_root, 10)
    with (
        _running_writer(staging_root, 1, b"123456", maximum=10) as (dead, first),
        _running_writer(staging_root, 2, b"abcd", maximum=10) as (_, live),
    ):
        _streaming(first)
        _streaming(live)
        dead.kill()
        dead.join(timeout=10)
        # Cleanup must leave the second writer's reservation and partial intact.
        evictor.evict_expired()
        assert len(list((staging_root / WRITES_DIRNAME).iterdir())) == 1
        area.stage(key_for(3), [b"abcdef"], expected_bytes=6)
        _finished(live, 4)
    assert not entry_path(staging_root, key_for(1)).exists()
    assert entry_path(staging_root, key_for(2)).read_bytes() == b"abcd"
    assert sum(entry.size_bytes for entry in evictor.entries()) == 10


def test_crash_after_publication_keeps_content_and_reclaims_the_reservation(
    staging_root: Path,
) -> None:
    area, evictor, leases = _services(staging_root, 10)
    with _running_writer(staging_root, 1, b"123456", maximum=10, crash_after_link=True) as (
        dead,
        writer,
    ):
        _streaming(writer)
        writer.send("finish")
        dead.join(timeout=10)
        assert dead.exitcode == 7
    with leases.hold(key_for(1)):
        area.stage(key_for(2), [b"abcd"], expected_bytes=4)
        with leases.hold(key_for(2)), pytest.raises(StagingCapacityExhaustedError):
            evictor.ensure_capacity(1)
    assert entry_path(staging_root, key_for(1)).read_bytes() == b"123456"
    assert list((staging_root / WRITES_DIRNAME).iterdir()) == []


@pytest.mark.parametrize("payload", [[b"123456"], [b"123", b"456"]])
def test_stream_overrun_never_writes_beyond_its_reservation(
    staging_root: Path, payload: list[bytes]
) -> None:
    area, _, _ = _services(staging_root, 10)
    with pytest.raises(StagingWriteError, match="exceeds its 5-byte reservation"):
        area.stage(key_for(1), payload, expected_bytes=5)
    assert not entry_path(staging_root, key_for(1)).exists()
    assert list((staging_root / WRITES_DIRNAME).iterdir()) == []
    area.stage(key_for(2), [b"1234567890"], expected_bytes=10)


def test_short_stream_releases_unused_reserved_bytes(staging_root: Path) -> None:
    area, _, leases = _services(staging_root, 10)
    area.stage(key_for(1), [b"12"], expected_bytes=10)
    with leases.hold(key_for(1)):
        area.stage(key_for(2), [b"12345678"], expected_bytes=8)


def test_failed_stream_cleans_up_after_arbitrary_exceptions(staging_root: Path) -> None:
    area, _, _ = _services(staging_root, 10)

    def chunks() -> Iterator[bytes]:
        yield b"first"
        raise ValueError("bad provider response")

    with pytest.raises(ValueError, match="bad provider response"):
        area.stage(key_for(1), chunks(), expected_bytes=10)
    assert list((staging_root / WRITES_DIRNAME).iterdir()) == []
    area.stage(key_for(2), [b"1234567890"], expected_bytes=10)


def test_release_does_not_remove_a_readers_leased_entry(staging_root: Path) -> None:
    area, evictor, leases = _services(staging_root, 10)
    area.stage(key_for(1), [b"123456"], expected_bytes=6)
    with leases.hold(key_for(1)):
        assert not evictor.release(key_for(1))
        with pytest.raises(StagingCapacityExhaustedError):
            area.stage(key_for(2), [b"abcdef"], expected_bytes=6)
    assert evictor.release(key_for(1))


def test_existing_entry_needs_no_capacity_and_does_not_consume_a_retry_stream(
    staging_root: Path,
) -> None:
    area, _, leases = _services(staging_root, 6)
    entry = area.stage(key_for(1), [b"123456"], expected_bytes=6)

    consumed: list[bool] = []

    def chunks() -> Iterator[bytes]:
        consumed.append(True)
        yield b"replacement"

    with leases.hold(key_for(1)):
        assert area.stage(key_for(1), chunks(), expected_bytes=100) == entry
    assert not consumed


def test_reservation_creation_crash_is_reclaimed(staging_root: Path) -> None:
    area, _, _ = _services(staging_root, 10)
    # A process can die after mkdir or after opening its owner file but before writing
    # the reservation size. Both are provably unowned under the volume lock.
    early = staging_root / WRITES_DIRNAME / "before-owner"
    early.mkdir(parents=True)
    later = staging_root / WRITES_DIRNAME / "before-size"
    later.mkdir()
    (later / "owner").touch()
    area.stage(key_for(1), [b"1234567890"], expected_bytes=10)
    assert list((staging_root / WRITES_DIRNAME).iterdir()) == []


def test_negative_size_is_rejected_before_starting_a_write(staging_root: Path) -> None:
    area, _, _ = _services(staging_root, 10)
    with pytest.raises(ValueError, match="nonnegative"):
        area.stage(key_for(1), [], expected_bytes=-1)
    assert list(staging_root.iterdir()) == []
