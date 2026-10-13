"""The schedule authority: its key, its lock's life, and the DBOS calls it writes through.

The lock runs against an in-memory lock server here and against PostgreSQL in
tests/integration/test_schedule_authority.py.
"""

import re
from pathlib import Path
from typing import cast

import psycopg
import pytest
from dbos import DBOS, ScheduleInput, VersionInfo, WorkflowSchedule

from curate_d.config import Settings
from curate_d.runtime import schedule_authority as authority_module
from curate_d.runtime.dbos_app import APPLICATION_NAME
from curate_d.runtime.lease import lease_key
from curate_d.runtime.schedule_authority import (
    ScheduleAuthorityUnavailableError,
    StoredSchedule,
    schedule_authority,
    schedule_authority_key,
)
from curate_d.worker import schedules as schedules_module
from tests.unit.runtime.fake_lease import FakeLockServer, FakeSession

KEY = schedule_authority_key(APPLICATION_NAME)


def test_the_key_is_pinned() -> None:
    """Workers of two builds contend on one key only while its derivation stays put."""
    assert schedule_authority_key("curate-d") == -4476472716236588759


def test_the_key_is_stable_and_apart_from_the_executor_lease() -> None:
    """Every worker must contend on the same key, and never on an executor's lease."""
    key = schedule_authority_key("curate-d")
    assert key == schedule_authority_key("curate-d")
    assert key != schedule_authority_key("another-app")
    assert key != lease_key("curate-d", "curate-d")
    assert -(2**63) <= key < 2**63


@pytest.fixture
def server() -> FakeLockServer:
    """Return an in-memory lock server standing in for the DBOS system database."""
    return FakeLockServer()


def test_the_authority_is_held_for_the_block_and_given_back_after(
    server: FakeLockServer,
) -> None:
    """The lock is taken before the block runs, then unlocked and its session closed."""
    with schedule_authority(Settings(), connect=server.connect, timeout_seconds=7) as authority:
        assert server.mode(KEY) == "exclusive"
        assert authority.still_held()
    assert server.waits == [7]
    assert server.mode(KEY) is None
    assert [session.closed for session in server.sessions] == [True]


def test_a_held_authority_times_out_naming_its_holder(server: FakeLockServer) -> None:
    """A stuck holder fails the waiting worker visibly, and its session is closed."""
    holder = server.connect()
    assert holder.try_lock(KEY)
    with (
        pytest.raises(ScheduleAuthorityUnavailableError, match=r"over 0\.3s: pid 42 \(fake\)"),
        schedule_authority(Settings(), connect=server.connect, timeout_seconds=0.3),
    ):
        pytest.fail("the block ran without the authority")
    assert server.holders[KEY] is holder
    assert server.sessions[-1].closed


def test_a_failing_block_still_gives_the_authority_back(server: FakeLockServer) -> None:
    """An exception from the block unlocks and closes the session before propagating."""
    with (
        pytest.raises(RuntimeError, match="reconciliation failed"),
        schedule_authority(Settings(), connect=server.connect),
    ):
        raise RuntimeError("reconciliation failed")
    assert server.mode(KEY) is None
    assert server.sessions[0].closed


def test_a_session_broken_in_the_block_is_closed_without_raising(server: FakeLockServer) -> None:
    """Unlocking a dead session fails, and must not replace the block's own outcome."""
    with schedule_authority(Settings(), connect=server.connect):
        server.sessions[0].close()
    assert server.sessions[0].closed
    assert server.mode(KEY) is None


def test_a_lost_lock_reads_as_lost(server: FakeLockServer) -> None:
    """A terminated session's lock is gone; `still_held` must say so, not assume."""
    with schedule_authority(Settings(), connect=server.connect) as authority:
        del server.holders[KEY]
        assert not authority.still_held()


def test_a_failed_probe_reads_as_lost(server: FakeLockServer) -> None:
    """A session that cannot answer cannot vouch for its lock."""
    with schedule_authority(Settings(), connect=server.connect) as authority:
        server.fail_probes = True
        assert not authority.still_held()


def test_a_session_that_cannot_open_raises_before_the_block(server: FakeLockServer) -> None:
    """No session, no authority: the block never runs."""
    server.refuse_connections = 1
    with (
        pytest.raises(psycopg.OperationalError),
        schedule_authority(Settings(), connect=server.connect),
    ):
        pytest.fail("the block ran without a session")


def test_the_default_session_is_dedicated_and_named_for_the_executor(
    monkeypatch: pytest.MonkeyPatch, server: FakeLockServer
) -> None:
    """The authority opens its own connection, named so a stuck holder can be traced."""
    opened: list[tuple[str, str]] = []

    def connect_session(conninfo: str, *, application_name: str) -> FakeSession:
        """Record how the session was asked for, then open a fake one."""
        opened.append((conninfo, application_name))
        return server.connect()

    monkeypatch.setattr(authority_module, "connect_session", connect_session)
    monkeypatch.setattr(authority_module, "lease_conninfo", lambda settings: "postgresql://db")
    monkeypatch.setattr(DBOS, "executor_id", "worker-io-0")
    with schedule_authority(Settings()):
        pass
    assert opened == [("postgresql://db", "curate-d schedule authority worker-io-0")]


def test_the_authority_reads_and_writes_through_dbos_public_schedule_api(
    monkeypatch: pytest.MonkeyPatch, server: FakeLockServer
) -> None:
    """Latest version, upsert, listing and deletion are DBOS's own calls, nothing private."""
    calls: list[tuple[str, object]] = []
    stored = cast(
        WorkflowSchedule, {"schedule_name": "kept", "workflow_name": "curate_d.prune_history"}
    )
    monkeypatch.setattr(
        DBOS,
        "get_latest_application_version",
        lambda: cast(VersionInfo, {"version_name": "1.1.0"}),
    )
    monkeypatch.setattr(DBOS, "apply_schedules", lambda entries: calls.append(("apply", entries)))
    monkeypatch.setattr(DBOS, "list_schedules", lambda: [stored])
    monkeypatch.setattr(DBOS, "delete_schedule", lambda name: calls.append(("delete", name)))
    entry = cast(ScheduleInput, {"schedule_name": "kept", "schedule": "0 0 * * *"})

    with schedule_authority(Settings(), connect=server.connect) as authority:
        assert authority.latest_version() == "1.1.0"
        authority.apply((entry,))
        assert authority.stored() == [StoredSchedule("kept", "curate_d.prune_history")]
        authority.delete("retired")

    assert calls == [("apply", [entry]), ("delete", "retired")]


_INTERNALS = re.compile(r"\bdbos\._|_get_dbos_instance|_sys_db|_registry|GlobalParams")


@pytest.mark.parametrize("module", [authority_module, schedules_module])
def test_the_module_imports_no_dbos_internals(module: object) -> None:
    """Schedule reconciliation uses DBOS's public API only, so an upgrade cannot break it."""
    source = Path(cast(str, getattr(module, "__file__", ""))).read_text(encoding="utf-8")
    assert not _INTERNALS.findall(source)
