"""Rollback promotion against the real system database.

A rollback relaunches a build DBOS already knows, and DBOS does not retime it, so the newer
build stays latest. These tests reproduce that (V1 registered, then V2, and an instance
launched on V1) and check that `promote` makes V1 latest again and that the two things a
stale latest version stalls, version-less enqueues and schedule ticks, then run on V1.

The fixture restores the previous latest version and deletes the test versions, their rows
and every schedule under the test prefix.
"""

import time
from collections.abc import Iterator
from datetime import datetime
from uuid import uuid4

import pytest
import sqlalchemy as sa
from dbos import DBOS, DBOSClient, ScheduleInput

from curate_d.config import Settings
from curate_d.runtime import dbos_app, promote
from curate_d.runtime.dbos_app import build_dbos_config, system_database_url
from curate_d.runtime.profiles import WorkerProfile
from curate_d.worker import schedules as worker_schedules

pytestmark = pytest.mark.integration

_RUN = uuid4().hex[:8]
V1 = f"9.9.1-promotion-{_RUN}"
V2 = f"9.9.2-promotion-{_RUN}"
QUEUE = f"promotion-{_RUN}"
PREFIX = f"promotion-test-{_RUN}-"


@DBOS.workflow()
def promotion_doubles(value: int) -> int:
    """Return twice `value`; a version-less enqueue of it runs on the latest version."""
    return value * 2


@DBOS.workflow()
def promotion_tick(scheduled_time: datetime, context: object) -> None:
    """Do nothing; a tick of it shows which version a schedule enqueues onto."""


def _latest(engine: sa.Engine) -> str | None:
    """Return this application's latest version as DBOS ranks it, if any."""
    with engine.connect() as connection:
        latest = connection.scalar(
            sa.text(
                "SELECT version_name FROM dbos.application_versions"
                " WHERE application_name = :app ORDER BY version_timestamp DESC LIMIT 1"
            ),
            {"app": dbos_app.application_name(Settings().environment)},
        )
    return None if latest is None else str(latest)


def _register_newest(engine: sa.Engine, version: str) -> None:
    """Register `version` as newer than every other, as a newer build's first worker does."""
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO dbos.application_versions"
                " (version_id, version_name, version_timestamp, application_name)"
                " SELECT :id, :v, COALESCE(max(version_timestamp), 0) + 1, :app"
                " FROM dbos.application_versions"
            ),
            {
                "id": str(uuid4()),
                "v": version,
                "app": dbos_app.application_name(Settings().environment),
            },
        )


@pytest.fixture
def system_engine(settings: Settings) -> Iterator[sa.Engine]:
    """A plain engine on the DBOS system database, outside DBOS's own pool."""
    engine = sa.create_engine(system_database_url(settings))
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def rolled_back(settings: Settings, system_engine: sa.Engine) -> Iterator[DBOSClient]:
    """Register V1 then V2, and launch an instance on V1, as a rollback does."""
    previous_latest = _latest(system_engine)
    _register_newest(system_engine, V1)
    time.sleep(0.01)
    _register_newest(system_engine, V2)
    DBOS.destroy()
    config = build_dbos_config(settings, executor_id=f"promotion-{_RUN}")
    config["application_version"] = V1
    DBOS(config=config)
    DBOS.launch()
    DBOS.register_queue(QUEUE)
    client = dbos_app.client(settings)
    try:
        yield client
    finally:
        for stored in DBOS.list_schedules(schedule_name_prefix=PREFIX):
            DBOS.delete_schedule(stored["schedule_name"])
        rows = DBOS.list_workflows(app_version=[V1, V2]) + DBOS.list_workflows(queue_name=QUEUE)
        if rows:
            DBOS.cancel_workflows([row.workflow_id for row in rows])
            DBOS.delete_workflows([row.workflow_id for row in rows])
        if previous_latest is not None:
            DBOS.set_latest_application_version(previous_latest)
        DBOS.destroy()
        client.destroy()
        with system_engine.begin() as connection:
            connection.execute(
                sa.text("DELETE FROM dbos.application_versions WHERE version_name = ANY(:v)"),
                {"v": [V1, V2]},
            )


def test_a_relaunched_build_stays_behind_until_promoted(
    rolled_back: DBOSClient, settings: Settings, system_engine: sa.Engine
) -> None:
    """DBOS does not retime a known version; promotion does, and reports what it replaced."""
    assert _latest(system_engine) == V2

    replaced = promote.promote(rolled_back, settings, V1)

    assert replaced == V2
    assert _latest(system_engine) == V1


def test_a_version_less_enqueue_waits_for_promotion_then_runs_on_v1(
    rolled_back: DBOSClient, settings: Settings
) -> None:
    """Only the latest version's workers dequeue it, so it waits while V2 is latest."""
    workflow_id = f"promotion-enqueue-{uuid4()}"
    rolled_back.enqueue(
        {
            "workflow_name": promotion_doubles.__qualname__,
            "queue_name": QUEUE,
            "workflow_id": workflow_id,
        },
        21,
    )
    time.sleep(3)
    status = rolled_back.retrieve_workflow(workflow_id).get_status()
    assert status.status == "ENQUEUED", "a V1 instance ran work while V2 was latest"

    promote.promote(rolled_back, settings, V1)

    assert rolled_back.retrieve_workflow(workflow_id).get_result(polling_interval_sec=0.2) == 42
    assert rolled_back.retrieve_workflow(workflow_id).get_status().app_version == V1


def test_schedules_apply_and_tick_on_the_promoted_version(
    rolled_back: DBOSClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After promotion a V1 worker applies its schedules, and their ticks run on V1."""
    promote.promote(rolled_back, settings, V1)
    name = f"{PREFIX}tick"
    declared: list[ScheduleInput] = [
        {
            "schedule_name": name,
            "workflow_fn": promotion_tick,
            "schedule": "*/2 * * * * *",
            "queue_name": QUEUE,
        }
    ]
    everything_else = frozenset(stored["schedule_name"] for stored in DBOS.list_schedules())
    monkeypatch.setattr(worker_schedules, "schedules_for", lambda profile, settings: declared)
    monkeypatch.setattr(
        worker_schedules, "declared_schedule_names", lambda settings: everything_else | {name}
    )

    worker_schedules.apply_schedules(WorkerProfile.IO, settings)

    assert [
        stored["schedule_name"] for stored in DBOS.list_schedules(schedule_name_prefix=PREFIX)
    ] == [name]
    for _ in range(100):
        done = DBOS.list_workflows(schedule_name=name, status="SUCCESS")
        if done:
            break
        time.sleep(0.2)
    assert done, "no tick of the applied schedule completed"
    assert done[0].app_version == V1
