"""The schedule authority's key, and the DBOS internals it writes schedules through.

The transaction itself runs against PostgreSQL in tests/integration/test_schedule_authority.py.
What is pinned here is what a DBOS upgrade could change underneath it: the row
`DBOS.apply_schedules` builds, which `stored_form` reproduces so it can write on the
authority's connection, and the internal signatures that writing relies on.
"""

import inspect
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace
from typing import cast

import pytest
from dbos import DBOS, ScheduleInput
from dbos import _dbos as dbos_internals
from dbos._schemas.system_database import SystemSchema
from dbos._serialization import DefaultSerializer
from dbos._sys_db import SystemDatabase, WorkflowSchedule

from curate_d.config import Settings
from curate_d.runtime import schedule_authority as authority_module
from curate_d.runtime.lease import lease_key
from curate_d.runtime.profiles import WorkerProfile
from curate_d.runtime.schedule_authority import schedule_authority_key, stored_form
from curate_d.workflows import retention
from curate_d.workflows.registry import schedules_for


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


class _RecordingSystemDatabase:
    """The parts of DBOS's system database that building and upserting a schedule touch."""

    def __init__(self) -> None:
        self.serializer = DefaultSerializer()
        self.upserted: list[WorkflowSchedule] = []

    @property
    def engine(self) -> "_RecordingSystemDatabase":
        return self

    @contextmanager
    def begin(self) -> Iterator[object]:
        yield object()

    def upsert_schedule(self, schedule: WorkflowSchedule, conn: object = None) -> None:
        self.upserted.append(schedule)

    def get_queue(self, name: str) -> object | None:
        return None if name == "undeclared-queue" else object()


@pytest.fixture
def system_database(monkeypatch: pytest.MonkeyPatch) -> _RecordingSystemDatabase:
    """Stand a recording system database in for a launched DBOS, for both builders."""
    sys_db = _RecordingSystemDatabase()
    instance = SimpleNamespace(
        _registry=dbos_internals._get_or_create_dbos_registry(), _sys_db=sys_db
    )
    monkeypatch.setattr(dbos_internals, "_get_dbos_instance", lambda: instance)
    monkeypatch.setattr(authority_module, "_get_dbos_instance", lambda: instance)
    return sys_db


def _without_id(row: WorkflowSchedule) -> dict[str, object]:
    """Strip a built row of its fresh UUID, which the upsert keeps from an existing row anyway."""
    return {field: value for field, value in row.items() if field != "schedule_id"}


def _every_option() -> ScheduleInput:
    """Return a declaration exercising each optional field."""
    return {
        "schedule_name": "every-option",
        "workflow_fn": retention.prune_history,
        "schedule": "0 30 2 * * *",
        "context": {"batch": 3},
        "automatic_backfill": True,
        "cron_timezone": "Europe/Berlin",
        "queue_name": "maintenance",
    }


def test_stored_form_builds_the_row_dbos_apply_schedules_builds(
    system_database: _RecordingSystemDatabase,
) -> None:
    """Pins DBOS 3.2's builder: an upgrade that changes it must fail here, not drift silently."""
    entries = [*schedules_for(WorkerProfile.IO, Settings()), _every_option()]
    DBOS.apply_schedules(entries)
    built = [stored_form(index, entry) for index, entry in enumerate(entries)]
    assert [_without_id(row) for row in built] == [
        _without_id(row) for row in system_database.upserted
    ]
    assert set(built[0]) == set(WorkflowSchedule.__annotations__)


def _unregistered(scheduled_time: datetime, context: object) -> None:
    """Do nothing; no DBOS decorator registered this function."""


@pytest.mark.parametrize(
    "entry",
    [
        {"workflow_fn": retention.prune_history, "schedule": "0 0 * * *"},
        {"schedule_name": "no-workflow", "schedule": "0 0 * * *"},
        {"schedule_name": "no-cron", "workflow_fn": retention.prune_history},
        {"schedule_name": "bad-cron", "workflow_fn": retention.prune_history, "schedule": "x"},
        {
            "schedule_name": "bad-zone",
            "workflow_fn": retention.prune_history,
            "schedule": "0 0 * * *",
            "cron_timezone": "Mars/Olympus",
        },
        {"schedule_name": "unregistered", "workflow_fn": _unregistered, "schedule": "0 0 * * *"},
        {
            "schedule_name": "no-queue",
            "workflow_fn": retention.prune_history,
            "schedule": "0 0 * * *",
            "queue_name": "undeclared-queue",
        },
    ],
    ids=["name", "workflow", "cron", "bad-cron", "zone", "unregistered", "queue"],
)
def test_stored_form_refuses_what_dbos_apply_schedules_refuses(
    system_database: _RecordingSystemDatabase, entry: dict[str, object]
) -> None:
    """Same exception type and message, so a bad declaration reads the same either way."""
    declaration = cast(ScheduleInput, entry)
    with pytest.raises(Exception) as from_dbos:
        DBOS.apply_schedules([declaration])
    with pytest.raises(type(from_dbos.value)) as from_ours:
        stored_form(0, declaration)
    assert str(from_ours.value) == str(from_dbos.value)
    assert system_database.upserted == []


@pytest.mark.parametrize("method", ["upsert_schedule", "list_schedules", "delete_schedule"])
def test_the_system_database_writes_schedules_on_a_callers_connection(method: str) -> None:
    """Pins a DBOS internal: without `conn` these would commit outside the authority."""
    assert "conn" in inspect.signature(getattr(SystemDatabase, method)).parameters


def test_the_version_lookup_internals_still_exist() -> None:
    """Pins DBOS internals the latest-version read on the authority's connection uses."""
    columns = SystemSchema.application_versions.c
    assert {"version_name", "version_timestamp", "application_name"} <= set(columns.keys())
    assert callable(SystemDatabase._name_filter)
