"""Which worker may apply and retire the application's persisted schedules, and when."""

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import cast

import pytest
from dbos import DBOS, ScheduleInput
from structlog.testing import CapturingLogger

from curate_d.config import Settings
from curate_d.runtime.dbos_app import application_version
from curate_d.runtime.profiles import WorkerProfile
from curate_d.runtime.schedule_authority import StoredSchedule
from curate_d.worker import main as worker_main
from curate_d.workflows.registry import declared_schedule_names, schedules_for

_SETTLE_SECONDS = 5.0


def _entry(name: str, cron: str = "0 0 * * *") -> ScheduleInput:
    """A schedule declaration as the fake store reads it: a name and a cron."""
    return cast(ScheduleInput, {"schedule_name": name, "schedule": cron})


@contextmanager
def _worker_logs(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict[str, object]]]:
    """Record what the worker module logs in the block, each entry with its event name."""
    sink = CapturingLogger()
    monkeypatch.setattr(worker_main, "logger", sink)
    entries: list[dict[str, object]] = []
    yield entries
    entries.extend({"event": call.args[0], **call.kwargs} for call in sink.calls)


class _WorkerVersion:
    """`DBOS.application_version` as seen from whichever worker thread reads it."""

    def __init__(self, store: "_SharedSchedules") -> None:
        self._store = store

    def __get__(self, _instance: object, _owner: type) -> str:
        return self._store.worker().version


class _Worker(threading.local):
    """One worker's view: its application version and the schedules its build declares."""

    version = "1.0.0"
    declared: list[ScheduleInput] | None = None


class _BackendLostError(Exception):
    """The fake transaction's backend ended; every later statement and the commit fail."""


class _Transaction:
    """One worker's authority: the lock plus writes staged until the commit.

    Mirrors the PostgreSQL transaction: reads see committed rows and this transaction's
    own writes, nothing is visible to other workers before the commit, and losing the
    backend releases the lock and discards the staged writes.
    """

    def __init__(self, store: "_SharedSchedules") -> None:
        self._store = store
        self._rows = dict(store.rows)
        self._applied = False
        self._deleted: list[str] = []
        self.lost = False

    def _statement(self) -> None:
        if self.lost:
            raise _BackendLostError

    def latest_version(self) -> str:
        self._statement()
        latest = self._store.latest
        self._store.pause(self)
        return latest

    def apply(self, schedules: list[ScheduleInput]) -> None:
        self._statement()
        for entry in schedules:
            self._rows[entry["schedule_name"]] = entry["schedule"]
        self._applied = True

    def stored(self) -> list[StoredSchedule]:
        self._statement()
        return [StoredSchedule(name, "wf") for name in sorted(self._rows)]

    def delete(self, name: str) -> None:
        self._statement()
        self._deleted.append(name)
        self._rows.pop(name, None)

    def lose(self) -> None:
        """End the backend: the lock goes at once, the staged writes with it."""
        self.lost = True
        self._store.authority.release()

    def commit(self) -> None:
        self._statement()
        self._store.before_commit(self)
        self._statement()
        self._store.rows = self._rows
        self._store.deleted.extend(self._deleted)
        if self._applied:
            self._store.applied.append(self._store.worker().version)
        self._store.authority.release()

    def rollback(self) -> None:
        if not self.lost:
            self._store.authority.release()


class _SharedSchedules:
    """DBOS's schedule rows, latest version and the authority lock, shared by workers.

    Each thread acts as one worker, with its own version and declarations. `pause` runs
    inside a worker's transaction right after it reads the latest version, which is where
    a worker can stall after passing the check; `before_commit` runs once its writes are
    staged and not yet committed.
    """

    def __init__(self, *stored: str, latest: str = "1.0.0") -> None:
        self.rows: dict[str, str] = dict.fromkeys(stored, "0 0 * * *")
        self.latest = latest
        self.applied: list[str] = []
        self.deleted: list[str] = []
        self.pause: Callable[[_Transaction], None] = lambda _txn: None
        self.before_commit: Callable[[_Transaction], None] = lambda _txn: None
        self.authority = threading.Lock()
        self._worker = _Worker()

    def worker(self) -> _Worker:
        return self._worker

    @contextmanager
    def acting_as(
        self, version: str, declared: list[ScheduleInput] | None = None
    ) -> Iterator[None]:
        """Run the block as a worker on `version` declaring `declared` (default: the real list)."""
        self._worker.version, self._worker.declared = version, declared
        yield

    @contextmanager
    def _schedule_authority(self) -> Iterator[_Transaction]:
        self.authority.acquire()
        txn = _Transaction(self)
        try:
            yield txn
        except BaseException:
            txn.rollback()
            raise
        txn.commit()

    def install(self, monkeypatch: pytest.MonkeyPatch) -> "_SharedSchedules":
        """Substitute this store for the authority transaction and the declarations."""

        def declared_for(profile: WorkerProfile, settings: Settings) -> list[ScheduleInput]:
            own = self._worker.declared
            if own is None or profile is not WorkerProfile.IO:
                return schedules_for(profile, settings)
            return own

        def names_declared(settings: Settings) -> frozenset[str]:
            own = self._worker.declared
            if own is None:
                return declared_schedule_names(settings)
            return frozenset(entry["schedule_name"] for entry in own)

        monkeypatch.setattr(DBOS, "application_version", _WorkerVersion(self))
        monkeypatch.setattr(worker_main, "schedules_for", declared_for)
        monkeypatch.setattr(worker_main, "declared_schedule_names", names_declared)
        monkeypatch.setattr(worker_main, "schedule_authority", self._schedule_authority)
        return self


def test_only_the_io_profile_applies_schedules(monkeypatch: pytest.MonkeyPatch) -> None:
    """The CPU profile applies nothing; the I/O profile applies its list."""
    store = _SharedSchedules().install(monkeypatch)
    worker_main.apply_schedules(WorkerProfile.CPU, Settings())
    assert store.applied == []
    worker_main.apply_schedules(WorkerProfile.IO, Settings())
    assert set(store.rows) == declared_schedule_names(Settings())
    assert schedules_for(WorkerProfile.CPU, Settings()) == []


def test_no_two_declared_schedules_share_a_name() -> None:
    """DBOS upserts by name, so a duplicate would silently replace another pass."""
    names = [entry["schedule_name"] for entry in schedules_for(WorkerProfile.IO, Settings())]
    assert len(names) == len(set(names))
    assert declared_schedule_names(Settings()) == frozenset(names)


def test_applying_after_every_restart_leaves_one_row_per_schedule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Restarts re-apply the same names and delete none of them."""
    store = _SharedSchedules().install(monkeypatch)
    for _ in range(3):
        worker_main.apply_schedules(WorkerProfile.IO, Settings())
    assert set(store.rows) == declared_schedule_names(Settings())
    assert store.deleted == []


def test_a_schedule_no_longer_declared_is_deleted(monkeypatch: pytest.MonkeyPatch) -> None:
    """A dropped or renamed pass stops firing once the latest version starts."""
    store = _SharedSchedules("retired-pass").install(monkeypatch)
    with _worker_logs(monkeypatch) as logs:
        worker_main.apply_schedules(WorkerProfile.IO, Settings())
    assert store.deleted == ["retired-pass"]
    assert set(store.rows) == declared_schedule_names(Settings())
    assert [log["schedule"] for log in logs if log["event"] == "worker.schedule_deleted"] == [
        "retired-pass"
    ]


def test_the_cpu_profile_deletes_no_schedule(monkeypatch: pytest.MonkeyPatch) -> None:
    """It declares none, so it takes no part in reconciliation."""
    store = _SharedSchedules("retired-pass").install(monkeypatch)
    store.pause = lambda _txn: pytest.fail("the CPU profile took the schedule authority")
    worker_main.apply_schedules(WorkerProfile.CPU, Settings())
    assert store.applied == []
    assert store.deleted == []


def test_an_older_version_leaves_a_newer_ones_schedules_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A replica restarting on the old version mid-rollout neither retires nor rewrites."""
    store = _SharedSchedules("added-by-the-newer-version", latest="1.1.0").install(monkeypatch)
    with store.acting_as("1.0.0"), _worker_logs(monkeypatch) as logs:
        worker_main.apply_schedules(WorkerProfile.IO, Settings())
    assert store.applied == []
    assert store.deleted == []
    assert set(store.rows) == {"added-by-the-newer-version"}
    left = [log for log in logs if log["event"] == "worker.schedules_left_to_latest_version"]
    assert [(log["version"], log["latest_version"]) for log in left] == [("1.0.0", "1.1.0")]


def test_an_older_dev_build_of_the_same_release_keeps_a_newer_builds_schedule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dev builds of one packaged version are distinct versions, so the older one is not latest."""
    older = application_version("0.1.1", "dev-20261003100000-1111111")
    newer = application_version("0.1.1", "dev-20261003110000-2222222")
    store = _SharedSchedules(latest=older).install(monkeypatch)
    older_declares = [_entry("kept-pass")]
    newer_declares = [_entry("kept-pass"), _entry("added-pass")]
    with store.acting_as(older, older_declares):
        worker_main.apply_schedules(WorkerProfile.IO, Settings())

    store.latest = newer  # the newer build's first worker registers its version
    with store.acting_as(newer, newer_declares):
        worker_main.apply_schedules(WorkerProfile.IO, Settings())
    with store.acting_as(older, older_declares):  # an older replica restarts
        worker_main.apply_schedules(WorkerProfile.IO, Settings())

    assert store.deleted == []
    assert set(store.rows) == {"kept-pass", "added-pass"}
    assert store.applied == [older, newer]


def _run_as(store: _SharedSchedules, version: str, declared: list[ScheduleInput]) -> None:
    """Reconcile as a worker on `version` declaring `declared`."""
    with store.acting_as(version, declared):
        worker_main.apply_schedules(WorkerProfile.IO, Settings())


def test_a_worker_that_stalls_after_its_check_cannot_overwrite_a_newer_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The check and the writes are one critical section across workers."""
    store = _SharedSchedules(latest="1.0.0").install(monkeypatch)
    older = [_entry("changed-pass", "0 0 * * *")]
    newer = [_entry("changed-pass", "30 1 * * *"), _entry("added-pass", "0 2 * * *")]
    checked, resume = threading.Event(), threading.Event()

    def stall_the_older_worker(_txn: _Transaction) -> None:
        if store.worker().version == "1.0.0":
            checked.set()
            assert resume.wait(_SETTLE_SECONDS)

    store.pause = stall_the_older_worker
    old_worker = threading.Thread(target=_run_as, args=(store, "1.0.0", older))
    old_worker.start()
    assert checked.wait(_SETTLE_SECONDS)

    # The newer version registers and starts while the older worker is stalled.
    store.latest = "1.1.0"
    new_worker = threading.Thread(target=_run_as, args=(store, "1.1.0", newer))
    new_worker.start()
    new_worker.join(0.2)
    assert new_worker.is_alive(), "the newer worker reconciled while the older one held it"

    resume.set()
    old_worker.join(_SETTLE_SECONDS)
    new_worker.join(_SETTLE_SECONDS)
    assert store.applied == ["1.0.0", "1.1.0"]
    assert store.rows == {"changed-pass": "30 1 * * *", "added-pass": "0 2 * * *"}


@pytest.mark.parametrize("lost_at", ["after the check", "before the commit"])
def test_a_worker_that_loses_its_backend_writes_nothing_after_a_newer_version(
    monkeypatch: pytest.MonkeyPatch, lost_at: str
) -> None:
    """Losing the backend loses the lock and every pending write together, so none lands later."""
    store = _SharedSchedules("retired-pass", "changed-pass", latest="1.0.0").install(monkeypatch)
    older = [_entry("changed-pass", "0 0 * * *"), _entry("older-only-pass")]
    newer = [_entry("changed-pass", "30 1 * * *"), _entry("retired-pass"), _entry("added-pass")]
    stalled, resume = threading.Event(), threading.Event()
    failures: list[BaseException] = []

    def stall_and_lose(txn: _Transaction) -> None:
        if store.worker().version == "1.0.0":
            txn.lose()  # an idle-in-transaction timeout, say, while the worker is stalled
            stalled.set()
            assert resume.wait(_SETTLE_SECONDS)

    if lost_at == "after the check":
        store.pause = stall_and_lose
    else:
        store.before_commit = stall_and_lose

    def run_older() -> None:
        try:
            _run_as(store, "1.0.0", older)
        except _BackendLostError as failure:
            failures.append(failure)

    with _worker_logs(monkeypatch) as logs:
        old_worker = threading.Thread(target=run_older)
        old_worker.start()
        assert stalled.wait(_SETTLE_SECONDS)

        # The newer version registers and gets the authority the older worker lost.
        store.latest = "1.1.0"
        _run_as(store, "1.1.0", newer)
        newer_rows = dict(store.rows)

        resume.set()
        old_worker.join(_SETTLE_SECONDS)

    assert not old_worker.is_alive()
    assert len(failures) == 1, "the older worker carried on without its authority"
    assert store.applied == ["1.1.0"]
    assert (
        store.rows
        == newer_rows
        == {
            "changed-pass": "30 1 * * *",
            "retired-pass": "0 0 * * *",
            "added-pass": "0 0 * * *",
        }
    )
    assert store.deleted == []
    assert not [log for log in logs if log["event"] == "worker.schedule_deleted"]
