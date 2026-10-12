"""The schedule fence's key and the check it runs as each armed transaction begins.

The check against PostgreSQL, inside the client's own transactions, is in
tests/integration/test_schedule_deploy.py.
"""

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import cast

import pytest
import sqlalchemy as sa
from psycopg.errors import LockNotAvailable

from curate_d.runtime.lease import lease_key
from curate_d.runtime.schedule_fence import (
    ScheduleAuthorityUnavailableError,
    ScheduleFence,
    StaleScheduleDeploymentError,
    schedule_authority_key,
)


def test_the_key_is_pinned() -> None:
    """The key the worker-side authority took: a rollout from such a build serializes."""
    assert schedule_authority_key("curate-d") == -4476472716236588759
    assert schedule_authority_key() == schedule_authority_key("curate-d")


def test_the_key_is_apart_from_the_executor_lease() -> None:
    """One advisory lock space: the domains keep the keys from aliasing."""
    key = schedule_authority_key()
    assert key != schedule_authority_key("another-app")
    assert key != lease_key("curate-d", "curate-d")
    assert -(2**63) <= key < 2**63


class _Result:
    """A one-value result."""

    def __init__(self, value: object) -> None:
        self._value = value

    def scalar(self) -> object:
        """Return the value."""
        return self._value

    def first(self) -> object:
        """Return the value as a row."""
        return self._value


class _Connection:
    """A connection that records statements and answers the fence's queries."""

    def __init__(self, *, latest: str | None, lock_free: bool = True) -> None:
        self.latest = latest
        self.lock_free = lock_free
        self.statements: list[str] = []
        self.engine = self

    def execute(self, statement: sa.TextClause, parameters: dict[str, object]) -> _Result:
        """Record `statement` and answer it."""
        text = str(statement)
        self.statements.append(text)
        if "pg_advisory_xact_lock" in text and not self.lock_free:
            raise sa.exc.OperationalError(text, parameters, LockNotAvailable("lock timeout"))
        if "pg_locks" in text:
            return _Result((42, "curate-d-schedules", "10.0.0.1", "2026-10-11 00:00:00"))
        if "application_versions" in text:
            return _Result(self.latest)
        return _Result(None)

    @contextmanager
    def connect(self) -> Iterator["_Connection"]:
        """Open a second connection on the same fake server."""
        yield self


def _check(fence: ScheduleFence, connection: _Connection) -> None:
    """Run the fence's begin hook on `connection`."""
    fence.check(cast(sa.Connection, connection))


def test_an_unarmed_transaction_runs_no_statement() -> None:
    """Waiting for the version to register reads through the same engine, unfenced."""
    fence = ScheduleFence(application="curate-d-local", version="1.0.0")
    connection = _Connection(latest="0.9.0")
    _check(fence, connection)
    assert connection.statements == []


def test_an_armed_transaction_bounds_locks_takes_the_authority_then_checks() -> None:
    """In that order, so the check reads the latest version while holding the lock."""
    fence = ScheduleFence(application="curate-d-local", version="1.0.0", timeout_seconds=2)
    connection = _Connection(latest="1.0.0")
    with fence.armed():
        _check(fence, connection)
    lock_timeout, lock, latest = connection.statements
    assert "lock_timeout" in lock_timeout
    assert "pg_advisory_xact_lock" in lock
    assert "application_versions" in latest


def test_a_stale_version_is_refused() -> None:
    """A newer version registered: the client's write never runs."""
    fence = ScheduleFence(application="curate-d-local", version="1.0.0")
    with fence.armed(), pytest.raises(StaleScheduleDeploymentError, match=r"'1\.1\.0'"):
        _check(fence, _Connection(latest="1.1.0"))


def test_no_registered_version_is_refused() -> None:
    """An empty version table cannot make anyone the owner."""
    fence = ScheduleFence(application="curate-d-local", version="1.0.0")
    with fence.armed(), pytest.raises(StaleScheduleDeploymentError):
        _check(fence, _Connection(latest=None))


def test_a_held_lock_names_its_holder() -> None:
    """The deploy job's log says which backend kept it waiting."""
    fence = ScheduleFence(application="curate-d-local", version="1.0.0", timeout_seconds=0.5)
    with (
        fence.armed(),
        pytest.raises(ScheduleAuthorityUnavailableError, match=r"over 0\.5s: pid 42"),
    ):
        _check(fence, _Connection(latest="1.0.0", lock_free=False))
    assert not fence.is_armed


def test_arming_is_per_thread_and_restored_on_exit() -> None:
    """Another thread sharing the engine is not fenced by this one's block."""
    fence = ScheduleFence(application="curate-d-local", version="1.0.0")
    seen: list[bool] = []
    with fence.armed():
        assert fence.is_armed
        worker = threading.Thread(target=lambda: seen.append(fence.is_armed))
        worker.start()
        worker.join()
    assert seen == [False]
    assert not fence.is_armed
