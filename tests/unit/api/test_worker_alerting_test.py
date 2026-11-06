"""The worker alerting test route enqueues a failure for one worker profile, only where enabled."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import cast

from dbos import DBOSClient, EnqueueOptions
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine

from curate_d.api.alerting_test import DbosWorkerAlertingTest
from curate_d.api.app import create_app
from curate_d.api.backends import Backends
from curate_d.config import DeploymentEnvironment, Settings
from curate_d.runtime.profiles import WorkerProfile
from curate_d.workflows.alerting_test import ALERTING_TEST_WORKFLOW
from tests.unit.api.fakes import FakeWorkerAlertingTest, StubEngine, backends

_IO_PATH = "/__alerting_test_worker_error__/io"
_CPU_PATH = "/__alerting_test_worker_error__/cpu"


def _client(settings: Settings, wired: Backends) -> TestClient:
    """Build a TestClient for the app under `settings`, over `wired`."""

    @asynccontextmanager
    async def factory(_settings: Settings, _engine: AsyncEngine) -> AsyncGenerator[Backends, None]:
        """Hand out `wired`."""
        yield wired

    return TestClient(
        create_app(
            settings,
            engine_factory=lambda _settings: StubEngine(),  # type: ignore[arg-type,return-value]
            backends_factory=factory,
        )
    )


def _dev() -> Settings:
    """Return dev settings, built inside a test so `.env` is already detached."""
    return Settings(environment=DeploymentEnvironment.DEV)


def _fake(wired: Backends) -> FakeWorkerAlertingTest:
    """Return the fake worker alerting test `wired` holds."""
    return cast(FakeWorkerAlertingTest, wired.worker_alerting_test)


def test_the_route_accepts_and_names_the_enqueued_failure() -> None:
    """202, with the profile, its queue and the workflow ID to look for in Logfire."""
    wired = backends()
    with _client(_dev(), wired) as client:
        response = client.post(_IO_PATH)
    assert response.status_code == 202
    assert response.json() == {
        "profile": "io",
        "queue": "maintenance",
        "workflow_id": "alerting-test:worker-io:20261103T0600Z",
    }
    assert _fake(wired).failed == [WorkerProfile.IO]


def test_each_profile_is_its_own_test() -> None:
    """The CPU path asks the CPU profile, not the I/O one."""
    wired = backends()
    with _client(_dev(), wired) as client:
        assert client.post(_CPU_PATH).status_code == 202
    assert _fake(wired).failed == [WorkerProfile.CPU]


def test_an_unknown_profile_is_rejected_before_anything_is_enqueued() -> None:
    """Only the deployed profiles can be asked."""
    wired = backends()
    with _client(_dev(), wired) as client:
        assert client.post("/__alerting_test_worker_error__/gpu").status_code == 422
    assert _fake(wired).failed == []


def test_a_read_enqueues_nothing() -> None:
    """GET is not the trigger, so a crawler or a prefetch cannot fire the alert."""
    wired = backends()
    with _client(_dev(), wired) as client:
        assert client.get(_IO_PATH).status_code == 405
    assert _fake(wired).failed == []


def test_the_route_is_absent_in_prod_by_default() -> None:
    """Off, the path answers 404 like any unknown path, and nothing is enqueued."""
    wired = backends()
    with _client(Settings(environment=DeploymentEnvironment.PROD), wired) as client:
        assert client.post(_IO_PATH).status_code == 404
    assert _fake(wired).failed == []


def test_the_route_is_served_in_prod_when_forced_on() -> None:
    """The prod switch mounts it beside the internal-error route."""
    forced = Settings(
        environment=DeploymentEnvironment.PROD, alerting_test_routes_force_on_in_prod=True
    )
    with _client(forced, backends()) as client:
        assert client.post(_IO_PATH).status_code == 202


def test_the_route_stays_out_of_the_schema() -> None:
    """The OpenAPI document never lists the worker alerting test route."""
    with _client(_dev(), backends()) as client:
        paths = client.get("/openapi.json").json()["paths"]
    assert not [path for path in paths if path.startswith("/__alerting_test")]


class _Workflows:
    """Records enqueues."""

    def __init__(self) -> None:
        """Start with nothing enqueued."""
        self.enqueued: list[tuple[EnqueueOptions, str]] = []

    async def enqueue_async(self, options: EnqueueOptions, profile: str) -> None:
        """Record the enqueue."""
        self.enqueued.append((options, profile))


async def test_each_profile_is_enqueued_on_a_queue_only_it_dequeues() -> None:
    """I/O on maintenance, CPU on fingerprint, by the registered name, keyed on the minute."""
    workflows = _Workflows()
    backend = DbosWorkerAlertingTest(
        cast(DBOSClient, workflows), clock=lambda: datetime(2026, 11, 3, 6, 0, 42, tzinfo=UTC)
    )
    io = await backend.fail(WorkerProfile.IO)
    cpu = await backend.fail(WorkerProfile.CPU)
    assert (io.queue, io.workflow_id) == ("maintenance", "alerting-test:worker-io:20261103T0600Z")
    assert (cpu.queue, cpu.workflow_id) == (
        "fingerprint",
        "alerting-test:worker-cpu:20261103T0600Z",
    )
    assert workflows.enqueued == [
        (
            {
                "workflow_name": ALERTING_TEST_WORKFLOW,
                "queue_name": "maintenance",
                "workflow_id": "alerting-test:worker-io:20261103T0600Z",
            },
            "io",
        ),
        (
            {
                "workflow_name": ALERTING_TEST_WORKFLOW,
                "queue_name": "fingerprint",
                "workflow_id": "alerting-test:worker-cpu:20261103T0600Z",
            },
            "cpu",
        ),
    ]


async def test_requests_within_a_minute_share_one_failure() -> None:
    """The same minute derives the same workflow ID, so DBOS runs it once."""
    workflows = _Workflows()
    minutes = iter(
        [
            datetime(2026, 11, 3, 6, 0, 1, tzinfo=UTC),
            datetime(2026, 11, 3, 6, 0, 59, tzinfo=UTC),
            datetime(2026, 11, 3, 6, 1, 0, tzinfo=UTC),
        ]
    )
    backend = DbosWorkerAlertingTest(cast(DBOSClient, workflows), clock=lambda: next(minutes))
    ids = [(await backend.fail(WorkerProfile.IO)).workflow_id for _ in range(3)]
    assert ids[0] == ids[1] != ids[2]
