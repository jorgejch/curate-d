"""Stage limits adjusted through the API while workers run, and across a restart.

The test process runs the workers. Two workflows that wait on a gate are queued on the
`verify` stage: with its limit at one, only one starts; raising the limit lets the second
start without restarting anything. Each test puts the stage's limits back.
"""

import asyncio
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from dbos import DBOS, SetWorkflowID
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine

from curate_d.api.app import create_app
from curate_d.config import Settings
from curate_d.runtime.dbos_app import client
from curate_d.runtime.queues import QUEUE_SPECS, QueueName, _registration_kwargs
from tests.integration.pipeline_harness import running_pipeline

pytestmark = pytest.mark.integration

_GATES: dict[str, asyncio.Event] = {}
_DEADLINE_SECONDS = 15.0


@DBOS.workflow()
async def held_for_throughput(gate: str) -> None:
    """Run until the test opens `gate`."""
    await _GATES[gate].wait()


@pytest.fixture
def http(settings: Settings) -> Iterator[TestClient]:
    """The app over the ledger and the workflow system database."""
    with TestClient(create_app(settings)) as client:
        yield client


@pytest.fixture
async def registered_stages(settings: Settings) -> None:
    """Register the stage queues with the limits a worker's launch registers.

    The route lists registered stages only, and on a fresh database nothing registers
    them until a worker launches: in a CI shard this module may run first. A client
    writes the rows without launching an executor that would dequeue or tick anything.
    Registration is `never_update`, so a limit an earlier test left stays as it was.
    """
    workflows = client(settings)
    try:
        for spec in QUEUE_SPECS:
            await workflows.register_queue_async(spec.name.value, **_registration_kwargs(spec))  # type: ignore[arg-type]
    finally:
        workflows.destroy()


@pytest.fixture
def restore_verify(http: TestClient, registered_stages: None) -> Iterator[None]:
    """Put back the verify stage's worker limit."""
    [before] = [s for s in http.get("/throughput").json() if s["stage"] == "verify"]
    yield
    http.patch("/throughput/verify", json={"worker_concurrency": before["worker_concurrency"]})


async def _statuses(workflow_ids: list[str]) -> list[str]:
    """Each workflow's status, in order."""
    found = {
        s.workflow_id: s.status
        for s in await DBOS.list_workflows_async(
            workflow_ids=workflow_ids, load_input=False, load_output=False
        )
    }
    return [found.get(workflow_id, "MISSING") for workflow_id in workflow_ids]


async def _until(predicate: Callable[[list[str]], bool], workflow_ids: list[str]) -> list[str]:
    """Poll the statuses until `predicate` holds or the deadline passes."""
    deadline = asyncio.get_running_loop().time() + _DEADLINE_SECONDS
    while True:
        statuses = await _statuses(workflow_ids)
        if predicate(statuses) or asyncio.get_running_loop().time() > deadline:
            return statuses
        await asyncio.sleep(0.2)


@pytest.fixture
async def gates() -> AsyncIterator[list[str]]:
    """Two closed gates, opened at the end whatever happens."""
    keys = [uuid4().hex for _ in range(2)]
    for key in keys:
        _GATES[key] = asyncio.Event()
    yield keys
    for key in keys:
        _GATES.pop(key).set()


async def test_raising_a_limit_starts_waiting_work_without_a_restart(
    settings: Settings,
    ledger_engine: AsyncEngine,
    tmp_path: Path,
    http: TestClient,
    restore_verify: None,
    gates: list[str],
) -> None:
    """At one, the second workflow waits; at two, it starts."""
    assert http.patch("/throughput/verify", json={"worker_concurrency": 1}).status_code == 200
    async with running_pipeline(
        settings, ledger_engine, tmp_path, listen=(QueueName.VERIFY.value,)
    ) as pipeline:
        workflow_ids = [f"test-throughput-{gate}" for gate in gates]
        for gate, workflow_id in zip(gates, workflow_ids, strict=True):
            with SetWorkflowID(workflow_id):
                await DBOS.enqueue_workflow_async(QueueName.VERIFY.value, held_for_throughput, gate)

        one_running = await _until(lambda s: "PENDING" in s, workflow_ids)
        await asyncio.sleep(2.0)
        still = await _statuses(workflow_ids)
        raised = http.patch("/throughput/verify", json={"worker_concurrency": 2})
        both_running = await _until(lambda s: s == ["PENDING", "PENDING"], workflow_ids)
        for gate in gates:
            _GATES[gate].set()
        await pipeline.settle()

    assert sorted(one_running) == ["ENQUEUED", "PENDING"]
    assert sorted(still) == ["ENQUEUED", "PENDING"]
    assert raised.json()["worker_concurrency"] == 2
    assert both_running == ["PENDING", "PENDING"]


async def test_an_adjusted_limit_survives_a_worker_restart(
    settings: Settings,
    ledger_engine: AsyncEngine,
    tmp_path: Path,
    http: TestClient,
    restore_verify: None,
) -> None:
    """Registration at startup does not put the default back."""
    async with running_pipeline(settings, ledger_engine, tmp_path):
        assert http.patch("/throughput/verify", json={"worker_concurrency": 5}).status_code == 200
    async with running_pipeline(settings, ledger_engine, tmp_path):
        queue = await DBOS.retrieve_queue_async(QueueName.VERIFY.value)
        assert queue is not None
        assert await queue.get_worker_concurrency_async() == 5
    [verify] = [s for s in http.get("/throughput").json() if s["stage"] == "verify"]
    assert verify["worker_concurrency"] == 5


async def test_an_out_of_range_limit_is_refused_and_the_previous_one_stays(
    http: TestClient, restore_verify: None
) -> None:
    """The permitted range is stated; the stored limit is unchanged."""
    [before] = [s for s in http.get("/throughput").json() if s["stage"] == "verify"]
    refused = http.patch("/throughput/verify", json={"worker_concurrency": -3})
    [after] = [s for s in http.get("/throughput").json() if s["stage"] == "verify"]
    assert refused.status_code == 422
    assert "greater than or equal to 1" in refused.text
    assert after == before
