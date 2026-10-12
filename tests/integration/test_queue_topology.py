"""Queue registration against the system database.

The properties that can only be checked here: the limits actually land, one provider's
partition saturating does not delay another's, a workflow a previous version left PENDING
does not consume a partition's budget, and a restart does not overwrite a limit the
control plane changed.

Probe queues get fresh names and are deleted afterwards. Registration is `never_update`,
so re-registering a name an earlier run left behind would check that row, not this code.
"""

import asyncio
from collections.abc import AsyncIterator, Callable
from uuid import uuid4

import pytest
from dbos import DBOS, Queue, SetEnqueueOptions, SetWorkflowID
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from curate_d.config import Settings
from curate_d.domain import HashAlgorithm, StrongFingerprint, derive_destination_id
from curate_d.runtime.dbos_app import SYSTEM_SCHEMA, configure
from curate_d.runtime.queues import (
    DEFAULT_CPU_CONCURRENCY,
    DEFAULT_VERIFY_CONCURRENCY,
    QUEUE_SPECS,
    SPECS_BY_NAME,
    QueueName,
    QueueSpec,
    _registration_kwargs,
    register_all_async,
)
from curate_d.runtime.workflow_ids import placement_workflow_id

pytestmark = pytest.mark.integration


@pytest.fixture
async def dbos(settings: Settings) -> AsyncIterator[None]:
    """Launch a DBOS instance against the test settings.

    Yields:
        Nothing; DBOS is destroyed again once the test completes.
    """
    DBOS.destroy()
    configure(settings, executor_id="queue-test")
    DBOS.launch()
    yield
    DBOS.destroy()


@pytest.fixture
async def probe_name(dbos: None) -> AsyncIterator[Callable[[str], str]]:
    """Hand out queue names no earlier run has registered, and delete those queues after.

    Yields:
        A function turning a prefix into a fresh queue name.
    """
    names: list[str] = []

    def fresh(prefix: str) -> str:
        """Return `prefix` with a random suffix, remembered for deletion."""
        name = f"{prefix}-{uuid4().hex[:8]}"
        names.append(name)
        return name

    yield fresh
    for name in names:
        await DBOS.delete_queue_async(name)


async def _register_probe(spec: QueueSpec, name: str) -> Queue:
    """Register `spec` exactly as `register_async` would, under a fresh name."""
    return await DBOS.register_queue_async(name, **_registration_kwargs(spec))  # type: ignore[arg-type]


async def _queue_row(engine: AsyncEngine, name: str) -> dict[str, object]:
    """Fetch the DBOS system database's row for a registered queue."""
    async with engine.connect() as connection:
        result = await connection.execute(
            text(f"SELECT * FROM {SYSTEM_SCHEMA}.queues WHERE name = :name"),
            {"name": name},
        )
        return dict(result.mappings().one())


async def test_the_configured_limits_are_applied_at_registration(
    dbos: None, ledger_engine: AsyncEngine
) -> None:
    """Registering a queue writes its configured worker_concurrency to the system database."""
    await register_all_async()
    row = await _queue_row(ledger_engine, QueueName.FINGERPRINT.value)
    assert row["worker_concurrency"] == DEFAULT_CPU_CONCURRENCY


async def test_the_verify_queue_registers_an_unpartitioned_per_worker_limit(
    dbos: None, probe_name: Callable[[str], str], ledger_engine: AsyncEngine
) -> None:
    """Verify lands as a plain per-worker limit, with no provider partitioning."""
    probe = probe_name("verify-probe")
    await _register_probe(SPECS_BY_NAME[QueueName.VERIFY], probe)
    row = await _queue_row(ledger_engine, probe)
    assert row["worker_concurrency"] == DEFAULT_VERIFY_CONCURRENCY
    assert row["partition_queue"] is False
    assert row["concurrency"] is None


async def test_every_registered_queue_reaches_the_system_database(
    dbos: None, ledger_engine: AsyncEngine
) -> None:
    """Every spec in QUEUE_SPECS ends up registered in the system database."""
    await register_all_async()
    async with ledger_engine.connect() as connection:
        registered = {
            row[0]
            for row in await connection.execute(text(f"SELECT name FROM {SYSTEM_SCHEMA}.queues"))
        }
    assert {spec.name.value for spec in QUEUE_SPECS} <= registered


async def test_partitioned_queues_persist_a_per_worker_partition_limit(
    dbos: None, probe_name: Callable[[str], str], ledger_engine: AsyncEngine
) -> None:
    """A partitioned spec lands as partition_worker_concurrency with no global partition cap."""
    for name in (QueueName.SCAN, QueueName.TRANSFER_SMALL, QueueName.TRANSFER_LARGE):
        spec = SPECS_BY_NAME[name]
        probe = probe_name(f"{name.value}-probe")
        await _register_probe(spec, probe)
        row = await _queue_row(ledger_engine, probe)
        assert row["partition_worker_concurrency"] == spec.partition_worker_concurrency
        assert row["partition_concurrency"] is None
        assert row["concurrency"] is None
        assert row["partition_queue"] is True


async def test_re_registration_preserves_a_runtime_adjusted_limit(
    dbos: None, probe_name: Callable[[str], str], ledger_engine: AsyncEngine
) -> None:
    """A worker restart must not undo what the control plane set."""
    spec = SPECS_BY_NAME[QueueName.TRANSFER_SMALL]
    probe = probe_name("rereg-probe")
    queue = await _register_probe(spec, probe)

    # The per-worker cap must be raised first: DBOS rejects a partition limit above it.
    await queue.set_worker_concurrency_async(11)
    await queue.set_partition_worker_concurrency_async(11)
    row = await _queue_row(ledger_engine, probe)
    assert (row["worker_concurrency"], row["partition_worker_concurrency"]) == (11, 11)

    # Startup registration runs again, as it would on the next pod.
    await _register_probe(spec, probe)
    row = await _queue_row(ledger_engine, probe)
    assert (row["worker_concurrency"], row["partition_worker_concurrency"]) == (11, 11)


async def test_saturating_one_provider_partition_does_not_delay_another(
    dbos: None,
    probe_name: Callable[[str], str],
) -> None:
    """One provider's burst must not consume the budget another provider needs."""
    queue = await DBOS.register_queue_async(
        probe_name("partition-probe"),
        partition_worker_concurrency=1,
        on_conflict="never_update",
    )

    started: list[str] = []
    release = asyncio.Event()

    @DBOS.workflow()
    async def hold(provider: str) -> str:
        """Record its provider and, for the slow one, block until released."""
        started.append(provider)
        if provider == "slow-provider":
            await release.wait()
        return provider

    # Two workflows for one provider saturate its partition at concurrency 1.
    blocked = []
    for _index in range(2):
        with (
            SetEnqueueOptions(queue_partition_key="slow-provider"),
            SetWorkflowID(f"slow-{uuid4()}"),
        ):
            blocked.append(await DBOS.enqueue_workflow_async(queue.name, hold, "slow-provider"))

    with SetEnqueueOptions(queue_partition_key="other-provider"), SetWorkflowID(f"fast-{uuid4()}"):
        unblocked = await DBOS.enqueue_workflow_async(queue.name, hold, "other-provider")

    # The other provider's work completes while the first provider's partition is full.
    assert await asyncio.wait_for(unblocked.get_result(), timeout=30) == "other-provider"

    release.set()
    for handle in blocked:
        await asyncio.wait_for(handle.get_result(), timeout=30)


async def _strand_a_previous_version_workflow(
    engine: AsyncEngine, queue: Queue, partition: str
) -> str:
    """Leave a PENDING workflow in `partition` under another version, as a drain does.

    Returns:
        The stranded workflow's ID, for the caller to cancel.
    """

    @DBOS.workflow()
    async def stranded() -> None:
        """Never runs here: it belongs to a version this process does not serve."""

    workflow_id = f"stranded-{uuid4()}"
    with (
        SetEnqueueOptions(queue_partition_key=partition, app_version="0.0.0-previous"),
        SetWorkflowID(workflow_id),
    ):
        await DBOS.enqueue_workflow_async(queue.name, stranded)
    # A previous version's worker dequeued it and has not finished: PENDING, not ours.
    async with engine.begin() as connection:
        await connection.execute(
            text(
                f"UPDATE {SYSTEM_SCHEMA}.workflow_status SET status = 'PENDING' "
                "WHERE workflow_uuid = :id"
            ),
            {"id": workflow_id},
        )
    return workflow_id


async def test_a_previous_versions_pending_work_does_not_hold_a_per_worker_partition(
    dbos: None, probe_name: Callable[[str], str], ledger_engine: AsyncEngine
) -> None:
    """A half-drained deployment must not consume the new version's partition budget."""

    @DBOS.workflow()
    async def probe(provider: str) -> str:
        """Return its provider, to show it was dequeued and ran."""
        return provider

    spec = SPECS_BY_NAME[QueueName.TRANSFER_LARGE]
    assert spec.partition_worker_concurrency == 1, "the probe saturates at one"
    queue = await _register_probe(spec, probe_name("drain-probe"))
    partition = f"provider-{uuid4().hex[:8]}"
    stranded = await _strand_a_previous_version_workflow(ledger_engine, queue, partition)
    try:
        with SetEnqueueOptions(queue_partition_key=partition), SetWorkflowID(f"new-{uuid4()}"):
            handle = await DBOS.enqueue_workflow_async(queue.name, probe, partition)
        assert await asyncio.wait_for(handle.get_result(), timeout=30) == partition
    finally:
        await DBOS.cancel_workflow_async(stranded)


async def test_a_global_partition_limit_is_held_by_a_previous_versions_pending_work(
    dbos: None, probe_name: Callable[[str], str], ledger_engine: AsyncEngine
) -> None:
    """Pin the DBOS behaviour the per-worker limits exist to avoid."""

    @DBOS.workflow()
    async def probe(provider: str) -> str:
        """Return its provider, to show it was dequeued and ran."""
        return provider

    queue = await DBOS.register_queue_async(
        probe_name("global-drain-probe"),
        partition_concurrency=1,
        on_conflict="never_update",
    )
    partition = f"provider-{uuid4().hex[:8]}"
    stranded = await _strand_a_previous_version_workflow(ledger_engine, queue, partition)
    waiting = f"starved-{uuid4()}"
    try:
        with SetEnqueueOptions(queue_partition_key=partition), SetWorkflowID(waiting):
            handle = await DBOS.enqueue_workflow_async(queue.name, probe, partition)
        # Several polling intervals: enough for a dequeue to have happened if it could.
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(handle.get_result(), timeout=5)
        assert (await handle.get_status()).status == "ENQUEUED"
    finally:
        await DBOS.cancel_workflows_async([stranded, waiting])


async def test_enqueueing_the_same_placement_twice_runs_it_once(
    dbos: None, probe_name: Callable[[str], str]
) -> None:
    """The reconciler enqueues optimistically on every pass; the duplicates must collapse."""
    queue = await DBOS.register_queue_async(
        probe_name("idempotency-probe"),
        worker_concurrency=4,
        on_conflict="never_update",
    )

    executions: list[str] = []

    @DBOS.workflow()
    async def place(locator: str) -> str:
        """Record that it ran for `locator`."""
        executions.append(locator)
        return locator

    # A fresh digest per run: a workflow id is permanent, so a fixed one would collide
    # with an earlier run instead of demonstrating collapse within this one.
    fingerprint = StrongFingerprint(algorithm=HashAlgorithm.SHA256, digest=uuid4().hex * 2)
    workflow_id = placement_workflow_id(
        fingerprint, derive_destination_id("pcloud", "acct-1", "/Archive")
    )

    handles = []
    for _ in range(3):
        with SetWorkflowID(workflow_id):
            handles.append(await DBOS.enqueue_workflow_async(queue.name, place, "/Archive/x.png"))

    results = [await asyncio.wait_for(handle.get_result(), timeout=30) for handle in handles]

    assert results == ["/Archive/x.png"] * 3
    assert executions == ["/Archive/x.png"], "the placement executed more than once"
