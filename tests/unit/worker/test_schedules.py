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
from curate_d.runtime.schedule_authority import (
    ScheduleAuthorityUnavailableError,
    StoredSchedule,
)
from curate_d.worker import schedules as worker_schedules
from curate_d.workflows.registry import declared_schedule_names, schedules_for

_SETTLE_SECONDS = 5.0


def _entry(name: str, cron: str = "0 0 * * *") -> ScheduleInput:
    """Return a schedule declaration as the fake store reads it: a name and a cron."""
    return cast(ScheduleInput, {"schedule_name": name, "schedule": cron})


@contextmanager
def _worker_logs(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[list[dict[str, object]]]:
    """Record what the schedules module logs in the block, with each event's name and level."""
    sink = CapturingLogger()
    monkeypatch.setattr(worker_schedules, "logger", sink)
    entries: list[dict[str, object]] = []
    yield entries
    entries.extend(
        {"event": call.args[0], "level": call.method_name, **call.kwargs} for call in sink.calls
    )


class _WorkerVersion:
    """`DBOS.application_version` as seen from whichever worker thread reads it."""

    def __init__(self, store: "_SharedSchedules") -> None:
        self._store = store

    def __get__(self, _instance: object, _owner: type) -> str:
        """Return the version of the worker on the calling thread."""
        return self._store.worker().version


class _Worker(threading.local):
    """One worker's view: its application version and the schedules its build declares."""

    version = "1.0.0"
    declared: list[ScheduleInput] | None = None


class _Authority:
    """One worker's hold on the authority, writing as DBOS does: each write commits at once.

    Losing the authority releases the lock to the next worker but stops nothing: the
    writes that follow still land, as DBOS's writes on its own pool do.
    """

    def __init__(self, store: "_SharedSchedules") -> None:
        self._store = store
        self.lost = False

    def latest_version(self) -> str:
        """Return the shared latest version, then run the store's pause hook."""
        latest = self._store.latest
        self._store.pause(self)
        return latest

    def apply(self, schedules: list[ScheduleInput]) -> None:
        """Commit each declaration's cron under its name."""
        for entry in schedules:
            self._store.rows[entry["schedule_name"]] = entry["schedule"]
        self._store.applied.append(self._store.worker().version)
        self._store.after_apply(self)

    def stored(self) -> list[StoredSchedule]:
        """Return the committed rows."""
        return [StoredSchedule(name, "wf") for name in sorted(self._store.rows)]

    def delete(self, name: str) -> None:
        """Commit the deletion of `name`."""
        self._store.deleted.append(name)
        self._store.rows.pop(name, None)

    def still_held(self) -> bool:
        """Return whether this hold has not been lost."""
        return not self.lost

    def lose(self) -> None:
        """Lose the session: the lock goes to the next worker at once."""
        self.lost = True
        self._store.lock.release()


class _SharedSchedules:
    """DBOS's schedule rows, latest version and the authority lock, shared by workers.

    Each thread acts as one worker, with its own version and declarations. `pause` runs
    right after a worker reads the latest version, which is where a worker can stall
    after passing the check; `after_apply` runs once its upserts have committed.
    """

    def __init__(self, *stored: str, latest: str = "1.0.0") -> None:
        self.rows: dict[str, str] = dict.fromkeys(stored, "0 0 * * *")
        self.latest = latest
        self.applied: list[str] = []
        self.deleted: list[str] = []
        self.pause: Callable[[_Authority], None] = lambda _hold: None
        self.after_apply: Callable[[_Authority], None] = lambda _hold: None
        self.lock = threading.Lock()
        self.holds = 0
        self._worker = _Worker()

    def worker(self) -> _Worker:
        """Return the calling thread's worker."""
        return self._worker

    @contextmanager
    def acting_as(
        self, version: str, declared: list[ScheduleInput] | None = None
    ) -> Iterator[None]:
        """Run the block as a worker on `version` declaring `declared` (default: the real list)."""
        self._worker.version, self._worker.declared = version, declared
        yield

    @contextmanager
    def _schedule_authority(self, _settings: Settings) -> Iterator[_Authority]:
        """Hold the lock for the block, unless the hold is lost first."""
        self.lock.acquire()
        self.holds += 1
        hold = _Authority(self)
        try:
            yield hold
        finally:
            if not hold.lost:
                self.lock.release()

    def install(self, monkeypatch: pytest.MonkeyPatch) -> "_SharedSchedules":
        """Substitute this store for the authority and the declarations."""

        def declared_for(profile: WorkerProfile, settings: Settings) -> list[ScheduleInput]:
            """Return the worker's own declarations, or the real ones."""
            own = self._worker.declared
            if own is None or profile is not WorkerProfile.IO:
                return schedules_for(profile, settings)
            return own

        def names_declared(settings: Settings) -> frozenset[str]:
            """Return the names the worker's build declares."""
            own = self._worker.declared
            if own is None:
                return declared_schedule_names(settings)
            return frozenset(entry["schedule_name"] for entry in own)

        monkeypatch.setattr(DBOS, "application_version", _WorkerVersion(self))
        monkeypatch.setattr(worker_schedules, "schedules_for", declared_for)
        monkeypatch.setattr(worker_schedules, "declared_schedule_names", names_declared)
        monkeypatch.setattr(worker_schedules, "schedule_authority", self._schedule_authority)
        return self


def _unverified(logs: list[dict[str, object]]) -> list[dict[str, object]]:
    """Return the unverified-reconciliation reports among `logs`."""
    return [log for log in logs if log["event"] == "worker.schedule_reconciliation_unverified"]


def test_only_the_io_profile_applies_schedules(monkeypatch: pytest.MonkeyPatch) -> None:
    """The CPU profile applies nothing; the I/O profile applies its list."""
    store = _SharedSchedules().install(monkeypatch)
    worker_schedules.apply_schedules(WorkerProfile.CPU, Settings())
    assert store.applied == []
    worker_schedules.apply_schedules(WorkerProfile.IO, Settings())
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
    """Restarts re-apply the same names, delete none of them and report nothing amiss."""
    store = _SharedSchedules().install(monkeypatch)
    with _worker_logs(monkeypatch) as logs:
        for _ in range(3):
            worker_schedules.apply_schedules(WorkerProfile.IO, Settings())
    assert set(store.rows) == declared_schedule_names(Settings())
    assert store.deleted == []
    assert _unverified(logs) == []


def test_a_schedule_no_longer_declared_is_deleted(monkeypatch: pytest.MonkeyPatch) -> None:
    """A dropped or renamed pass stops firing once the latest version starts."""
    store = _SharedSchedules("retired-pass").install(monkeypatch)
    with _worker_logs(monkeypatch) as logs:
        worker_schedules.apply_schedules(WorkerProfile.IO, Settings())
    assert store.deleted == ["retired-pass"]
    assert set(store.rows) == declared_schedule_names(Settings())
    assert [log["schedule"] for log in logs if log["event"] == "worker.schedule_deleted"] == [
        "retired-pass"
    ]


def test_the_cpu_profile_deletes_no_schedule(monkeypatch: pytest.MonkeyPatch) -> None:
    """It declares none, so it takes no part in reconciliation."""
    store = _SharedSchedules("retired-pass").install(monkeypatch)
    worker_schedules.apply_schedules(WorkerProfile.CPU, Settings())
    assert store.holds == 0
    assert store.applied == []
    assert store.deleted == []


def test_an_older_version_leaves_a_newer_ones_schedules_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A replica restarting on the old version mid-rollout neither retires nor rewrites."""
    store = _SharedSchedules("added-by-the-newer-version", latest="1.1.0").install(monkeypatch)
    with store.acting_as("1.0.0"), _worker_logs(monkeypatch) as logs:
        worker_schedules.apply_schedules(WorkerProfile.IO, Settings())
    assert store.applied == []
    assert store.deleted == []
    assert set(store.rows) == {"added-by-the-newer-version"}
    left = [log for log in logs if log["event"] == "worker.schedules_left_to_latest_version"]
    assert [(log["version"], log["latest_version"]) for log in left] == [("1.0.0", "1.1.0")]
    assert _unverified(logs) == []


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
        worker_schedules.apply_schedules(WorkerProfile.IO, Settings())

    store.latest = newer  # the newer build's first worker registers its version
    with store.acting_as(newer, newer_declares):
        worker_schedules.apply_schedules(WorkerProfile.IO, Settings())
    with store.acting_as(older, older_declares):  # an older replica restarts
        worker_schedules.apply_schedules(WorkerProfile.IO, Settings())

    assert store.deleted == []
    assert set(store.rows) == {"kept-pass", "added-pass"}
    assert store.applied == [older, newer]


def _run_as(store: _SharedSchedules, version: str, declared: list[ScheduleInput]) -> None:
    """Reconcile as a worker on `version` declaring `declared`."""
    with store.acting_as(version, declared):
        worker_schedules.apply_schedules(WorkerProfile.IO, Settings())


def test_a_worker_that_stalls_after_its_check_holds_off_a_newer_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """While the authority is held, the check and the writes are one critical section."""
    store = _SharedSchedules(latest="1.0.0").install(monkeypatch)
    older = [_entry("changed-pass", "0 0 * * *")]
    newer = [_entry("changed-pass", "30 1 * * *"), _entry("added-pass", "0 2 * * *")]
    checked, resume = threading.Event(), threading.Event()

    def stall_the_older_worker(_hold: _Authority) -> None:
        """Stall the older worker once, right after its check."""
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


def _stale_write(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[_SharedSchedules, list[ScheduleInput], list[dict[str, object]]]:
    """Let an older worker lose its authority after its check and write over a newer one.

    Returns:
        The store, the newer version's declarations and what the older worker logged.
    """
    store = _SharedSchedules("retired-pass", "changed-pass", latest="1.0.0").install(monkeypatch)
    older = [_entry("changed-pass", "0 0 * * *"), _entry("older-only-pass")]
    newer = [_entry("changed-pass", "30 1 * * *"), _entry("retired-pass"), _entry("added-pass")]
    stalled, resume = threading.Event(), threading.Event()

    def stall_and_lose(hold: _Authority) -> None:
        """Lose the older worker's session while it is stalled after its check."""
        if store.worker().version == "1.0.0" and not hold.lost:
            hold.lose()  # a terminated session, say, while the worker is stalled
            stalled.set()
            assert resume.wait(_SETTLE_SECONDS)

    store.pause = stall_and_lose
    with _worker_logs(monkeypatch) as logs:
        old_worker = threading.Thread(target=_run_as, args=(store, "1.0.0", older))
        old_worker.start()
        assert stalled.wait(_SETTLE_SECONDS)

        # The newer version registers and gets the authority the older worker lost.
        store.latest = "1.1.0"
        _run_as(store, "1.1.0", newer)
        resume.set()
        old_worker.join(_SETTLE_SECONDS)
    assert not old_worker.is_alive()
    return store, newer, logs


def test_a_worker_that_loses_its_authority_reports_its_writes_unverified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DBOS commits the stale writes; the worker cannot stop them, so it says so at ERROR."""
    store, _newer, logs = _stale_write(monkeypatch)
    assert store.applied == ["1.1.0", "1.0.0"]
    assert store.rows["changed-pass"] == "0 0 * * *", "the stale write did not land"
    [report] = _unverified(logs)
    assert report["level"] == "error"
    assert (report["version"], report["latest_before"], report["latest_after"]) == (
        "1.0.0",
        "1.0.0",
        "1.1.0",
    )
    assert report["held"] is False
    assert report["applied"] == ["changed-pass", "older-only-pass"]
    assert report["deleted"] == ["added-pass", "retired-pass"]


def test_the_next_reconciliation_on_the_latest_version_repairs_a_stale_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Detect, then heal: the newer worker's next pass restores its own declarations."""
    store, newer, _logs = _stale_write(monkeypatch)
    with _worker_logs(monkeypatch) as logs:
        _run_as(store, "1.1.0", newer)
    assert store.rows == {
        "changed-pass": "30 1 * * *",
        "retired-pass": "0 0 * * *",
        "added-pass": "0 0 * * *",
    }
    assert _unverified(logs) == []


def test_a_promotion_during_the_writes_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    """The lock still held is not enough: a newer latest makes the writes another's to make."""
    store = _SharedSchedules(latest="1.0.0").install(monkeypatch)

    def promote(_hold: _Authority) -> None:
        """Promote another version once the upserts have committed."""
        store.latest = "1.1.0"

    store.after_apply = promote
    with _worker_logs(monkeypatch) as logs:
        worker_schedules.apply_schedules(WorkerProfile.IO, Settings())
    [report] = _unverified(logs)
    assert (report["latest_before"], report["latest_after"], report["held"]) == (
        "1.0.0",
        "1.1.0",
        True,
    )


def test_the_reconciler_reapplies_every_interval_until_stopped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not at start, since startup just applied; then once per interval until the stop."""
    applied = threading.Semaphore(0)
    calls: list[WorkerProfile] = []

    def record(profile: WorkerProfile, settings: Settings) -> None:
        """Count one reconciliation."""
        calls.append(profile)
        applied.release()

    monkeypatch.setattr(worker_schedules, "apply_schedules", record)
    stop = threading.Event()
    reconciler = worker_schedules.ScheduleReconciler(
        WorkerProfile.IO, Settings(), stop=stop, interval_seconds=0.3
    )
    reconciler.start()
    assert calls == []
    assert applied.acquire(timeout=_SETTLE_SECONDS)
    assert applied.acquire(timeout=_SETTLE_SECONDS)
    stop.set()
    assert reconciler.join(_SETTLE_SECONDS)
    assert set(calls) == {WorkerProfile.IO}


def test_the_reconciler_waits_one_interval_before_its_first_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stop within the first interval ends the thread with nothing reconciled."""
    calls: list[WorkerProfile] = []
    monkeypatch.setattr(
        worker_schedules, "apply_schedules", lambda profile, settings: calls.append(profile)
    )
    stop = threading.Event()
    reconciler = worker_schedules.ScheduleReconciler(WorkerProfile.IO, Settings(), stop=stop)
    reconciler.start()
    stop.set()
    assert reconciler.join(_SETTLE_SECONDS)
    assert calls == []


def test_a_failed_pass_is_logged_and_the_next_one_still_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unavailable authority warns, a defect errors, and neither ends the healing loop."""
    failures: list[Exception] = [
        ScheduleAuthorityUnavailableError("held elsewhere"),
        RuntimeError("a defect"),
    ]
    healed = threading.Event()

    def flaky(profile: WorkerProfile, settings: Settings) -> None:
        """Fail with each queued failure in turn, then succeed."""
        if failures:
            raise failures.pop(0)
        healed.set()

    monkeypatch.setattr(worker_schedules, "apply_schedules", flaky)
    stop = threading.Event()
    with _worker_logs(monkeypatch) as logs:
        reconciler = worker_schedules.ScheduleReconciler(
            WorkerProfile.IO, Settings(), stop=stop, interval_seconds=0.05
        )
        reconciler.start()
        assert healed.wait(_SETTLE_SECONDS)
        stop.set()
        assert reconciler.join(_SETTLE_SECONDS)
    failed = [log for log in logs if log["event"] == "worker.schedule_reconciliation_failed"]
    assert [(log["level"], log["error_type"]) for log in failed] == [
        ("warning", "ScheduleAuthorityUnavailableError"),
        ("error", "RuntimeError"),
    ]


def test_the_cpu_profile_starts_no_reconciler() -> None:
    """It declares no schedule, so it has nothing to heal; the I/O profile has a thread."""
    stop = threading.Event()
    assert (
        worker_schedules.start_schedule_reconciler(WorkerProfile.CPU, Settings(), stop=stop) is None
    )
    reconciler = worker_schedules.start_schedule_reconciler(WorkerProfile.IO, Settings(), stop=stop)
    assert reconciler is not None
    stop.set()
    assert reconciler.join(_SETTLE_SECONDS)


def test_the_interval_defaults_to_ten_minutes_and_must_be_positive() -> None:
    """A zero interval would spin; the default bounds drift to one interval."""
    assert Settings().schedule_reconciliation_interval_seconds == 600
    with pytest.raises(ValueError, match="greater than 0"):
        Settings(schedule_reconciliation_interval_seconds=0)
