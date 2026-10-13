"""The operator sweep end to end, through DBOSClient, against the real system database.

No DBOS instance is launched: the sweep runs exactly as the module entry point does in a
pod. The suite's version is the target, registered in DBOS and `lease_versions` and made
latest for the test; a source version registered before it holds the seeded rows. The
previous latest version is restored afterwards, and every test row and every registration
the fixture added is deleted; a registration it found is kept.
"""

import json
from collections.abc import Iterator
from uuid import uuid4

import pytest
import sqlalchemy as sa
from dbos import DBOS
from dbos._utils import GlobalParams

from curate_d.config import Settings
from curate_d.runtime import dbos_app, operator_sweep
from curate_d.runtime.dbos_app import system_database_url
from curate_d.runtime.identity import POD_NAME_ENV
from curate_d.runtime.lease import ExecutorLease
from curate_d.runtime.sweep import recovery_successor_id

pytestmark = pytest.mark.integration

_RUN = uuid4().hex[:8]
SOURCE = f"9.9.4-operator-{_RUN}"
RETIRED_EXECUTOR = f"operator-retired-{_RUN}"


def _app() -> str:
    """Return the DBOS application name the client and the seeded rows use."""
    return dbos_app.application_name(Settings().environment)


@pytest.fixture
def system_engine(settings: Settings) -> Iterator[sa.Engine]:
    """A plain engine on the DBOS system database, outside DBOS's own pool."""
    engine = sa.create_engine(system_database_url(settings))
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def target(system_engine: sa.Engine) -> Iterator[str]:
    """Make the suite's version the registered, latest target, with an older source version."""
    version = dbos_app.APPLICATION_VERSION
    DBOS.destroy()
    with system_engine.begin() as connection:
        previous = connection.scalar(
            sa.text(
                "SELECT version_name FROM dbos.application_versions"
                " WHERE application_name = :app ORDER BY version_timestamp DESC LIMIT 1"
            ),
            {"app": _app()},
        )
        inserted_target = (
            connection.scalar(
                sa.text(
                    "INSERT INTO lease_versions (version) VALUES (:target)"
                    " ON CONFLICT (version) DO NOTHING RETURNING version"
                ),
                {"target": version},
            )
            is not None
        )
        connection.execute(
            sa.text(
                "INSERT INTO lease_versions (version, first_registered)"
                " SELECT :source, first_registered - interval '1 hour'"
                " FROM lease_versions WHERE version = :target"
                " ON CONFLICT (version) DO NOTHING"
            ),
            {"source": SOURCE, "target": version},
        )
        connection.execute(
            sa.text("DELETE FROM dbos.application_versions WHERE version_name = :v"),
            {"v": version},
        )
        connection.execute(
            sa.text(
                "INSERT INTO dbos.application_versions"
                " (version_id, version_name, version_timestamp, application_name)"
                " SELECT :id, :v, max(version_timestamp) + 1, :app FROM dbos.application_versions"
            ),
            {"id": str(uuid4()), "v": version, "app": _app()},
        )
    try:
        yield version
    finally:
        with system_engine.begin() as connection:
            connection.execute(
                sa.text(
                    "DELETE FROM dbos.workflow_status WHERE application_version = :source"
                    " OR workflow_uuid LIKE 'operator-%' OR forked_from LIKE 'operator-%'"
                ),
                {"source": SOURCE},
            )
            connection.execute(
                sa.text("DELETE FROM lease_versions WHERE version = ANY(:v)"),
                {"v": [SOURCE, version] if inserted_target else [SOURCE]},
            )
            if previous is not None and previous != version:
                connection.execute(
                    sa.text(
                        "UPDATE dbos.application_versions SET version_timestamp ="
                        " (SELECT max(version_timestamp) + 1 FROM dbos.application_versions)"
                        " WHERE version_name = :v"
                    ),
                    {"v": previous},
                )


def _seed(engine: sa.Engine, workflow_id: str, *, executor_id: str, version: str) -> None:
    """Write a PENDING row on a queue no worker consumes."""
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO dbos.workflow_status"
                " (workflow_uuid, status, name, executor_id, application_version,"
                "  application_name, inputs, serialization, queue_name)"
                " VALUES (:id, 'PENDING', 'tests.operator.stranded', :executor, :version,"
                "  :app, NULL, 'py_pickle', 'operator-unconsumed')"
            ),
            {
                "id": workflow_id,
                "executor": executor_id,
                "version": version,
                "app": GlobalParams.app_name or _app(),
            },
        )


def _row(engine: sa.Engine, workflow_id: str) -> dict[str, object] | None:
    """Return the status columns of one row, or None if it does not exist."""
    with engine.connect() as connection:
        row = (
            connection.execute(
                sa.text(
                    "SELECT status, executor_id, application_version, queue_name, forked_from"
                    " FROM dbos.workflow_status WHERE workflow_uuid = :id"
                ),
                {"id": workflow_id},
            )
            .mappings()
            .one_or_none()
        )
    return None if row is None else dict(row)


def _counts(engine: sa.Engine) -> tuple[int, int]:
    """Return the number of DBOS versions and registered versions."""
    with engine.connect() as connection:
        versions = connection.scalar(sa.text("SELECT count(*) FROM dbos.application_versions"))
        registered = connection.scalar(sa.text("SELECT count(*) FROM lease_versions"))
    return int(versions or 0), int(registered or 0)


def test_a_mismatched_target_writes_nothing(target: str, system_engine: sa.Engine) -> None:
    """Exit 2, and the seeded row, the versions and the registry are as they were."""
    workflow_id = f"operator-mismatch-{uuid4()}"
    _seed(system_engine, workflow_id, executor_id=RETIRED_EXECUTOR, version=SOURCE)
    before, counts = _row(system_engine, workflow_id), _counts(system_engine)

    assert operator_sweep.main(["--target-version", SOURCE]) == operator_sweep.EXIT_REFUSED

    assert _row(system_engine, workflow_id) == before
    assert _row(system_engine, recovery_successor_id(workflow_id)) is None
    assert _counts(system_engine) == counts


def test_a_matching_run_recovers_through_the_client(
    target: str, system_engine: sa.Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    """The stranded row is forked under its deterministic successor, without launching DBOS."""
    workflow_id = f"operator-stranded-{uuid4()}"
    _seed(system_engine, workflow_id, executor_id=RETIRED_EXECUTOR, version=SOURCE)

    status = operator_sweep.main(["--target-version", target])

    printed = json.loads(capsys.readouterr().out)
    assert [workflow_id, recovery_successor_id(workflow_id)] in printed["forked"]
    assert status in {0, operator_sweep.EXIT_FAILED_ROWS}
    assert workflow_id not in [wid for wid, _ in printed["failed"]]
    successor = _row(system_engine, recovery_successor_id(workflow_id))
    assert successor is not None
    assert successor["forked_from"] == workflow_id
    assert successor["application_version"] == target
    assert successor["queue_name"] == "operator-unconsumed"
    original = _row(system_engine, workflow_id)
    assert original is not None and original["status"] == "CANCELLED"


def test_a_live_workers_pending_row_is_left_alone(
    target: str,
    settings: Settings,
    system_engine: sa.Engine,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Run in a live I/O worker's pod, the sweep leaves that worker's own rows untouched."""
    executor = f"operator-live-{_RUN}"
    monkeypatch.setenv(POD_NAME_ENV, executor)
    workflow_id = f"operator-running-{uuid4()}"
    _seed(system_engine, workflow_id, executor_id=executor, version=target)
    worker = ExecutorLease.for_settings(settings, executor, version=target)
    worker.acquire(timeout=10)
    try:
        before = _row(system_engine, workflow_id)
        operator_sweep.main(["--target-version", target])
        printed = json.loads(capsys.readouterr().out)
        assert printed["left_running"] >= 1
        assert workflow_id not in printed["reenqueued"]
        assert _row(system_engine, workflow_id) == before
    finally:
        worker.release()
