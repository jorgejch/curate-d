"""Turning Conductor off with the key still provisioned.

A worker connected to the fake Conductor restarts under the same executor ID with the flag
off and the key still set: it opens no connection, logs `conductor.disabled` once, and keeps
executing workflows. Conductor is read-only, so nothing it sent before the restart is left
for the worker to finish.
"""

from collections.abc import Iterator
from typing import Final
from uuid import uuid4

import pytest
from dbos import DBOS, SetWorkflowID

from curate_d.config import Settings
from curate_d.runtime.profiles import WorkerProfile
from tests.integration.conductor_harness import (
    FakeConductor,
    capture_gate_logs,
    launch_worker,
    stop_worker,
    workflow_status,
)

pytestmark = pytest.mark.integration

KEY: Final = "test-conductor-key"


@DBOS.workflow()
def conductor_disable_echo(value: str) -> str:
    """Return the value: proof that the worker executes workflows."""
    return value


@pytest.fixture
def fake_conductor() -> Iterator[FakeConductor]:
    """Serve a fake Conductor for the test."""
    server = FakeConductor()
    yield server
    server.close()


def test_a_worker_restarted_with_conductor_off_never_connects_and_still_executes(
    monkeypatch: pytest.MonkeyPatch, fake_conductor: FakeConductor, settings: Settings
) -> None:
    """Off, the worker opens no connection, though the key is set, and runs its workflows."""
    executor_id = f"conductor-disable-{uuid4().hex[:8]}"
    monkeypatch.setenv("CURATE_D_CONDUCTOR_ENABLED", "true")
    monkeypatch.setenv("CURATE_D_CONDUCTOR_KEY", KEY)
    monkeypatch.setenv("CURATE_D_ENVIRONMENT", "local")
    monkeypatch.setenv("CURATE_D_CONDUCTOR_URL", fake_conductor.url)
    monkeypatch.delenv("CURATE_D_CONDUCTOR_PROXY_URL", raising=False)
    try:
        assert launch_worker(Settings(), WorkerProfile.IO, executor_id) is not None
        assert fake_conductor.executor_info()["executor_id"] == executor_id
        stop_worker()
        handshakes = list(fake_conductor.handshakes)

        logs = capture_gate_logs(monkeypatch)
        monkeypatch.setenv("CURATE_D_CONDUCTOR_ENABLED", "false")
        assert launch_worker(Settings(), WorkerProfile.IO, executor_id) is None
        workflow_id = f"conductor-disable-echo-{uuid4()}"
        with SetWorkflowID(workflow_id):
            assert conductor_disable_echo("while off") == "while off"

        assert workflow_status(settings, workflow_id) == "SUCCESS"
        assert fake_conductor.handshakes == handshakes
        assert [entry["event"] for entry in logs] == ["conductor.disabled"]
    finally:
        stop_worker()
