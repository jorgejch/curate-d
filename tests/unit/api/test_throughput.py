"""Stage limits: viewing, adjusting, refusing what does not fit, and the write order."""

from dataclasses import replace
from typing import cast

import pytest
from dbos import DBOSClient

from curate_d.api.errors import NotFoundError, RejectedError
from curate_d.api.throughput import DbosThroughput, LimitsChange, Rate, StageLimits, apply_change
from curate_d.runtime.queues import SPECS_BY_NAME, QueueName
from tests.unit.api.fakes import FakeThroughput, backends, client


def _limits(stage: QueueName, worker: int | None, partition: int | None = None) -> StageLimits:
    """Stored limits for `stage`."""
    spec = SPECS_BY_NAME[stage]
    return StageLimits(
        stage=stage,
        limit_driver=spec.limit_driver,
        partitioned=spec.partitioned,
        worker_concurrency=worker,
        partition_worker_concurrency=partition,
    )


def test_a_change_keeps_what_it_leaves_out() -> None:
    """Only the fields given change."""
    current = _limits(QueueName.TRANSFER_SMALL, 2, 1).model_copy(
        update={"rate": Rate(limit=120, period=60)}
    )
    updated = apply_change(
        SPECS_BY_NAME[QueueName.TRANSFER_SMALL], current, LimitsChange(worker_concurrency=4)
    )
    assert (updated.worker_concurrency, updated.partition_worker_concurrency) == (4, 1)
    assert updated.rate == Rate(limit=120, period=60)


def test_an_unpartitioned_stage_has_no_partition_limit() -> None:
    """Refused rather than silently ignored."""
    with pytest.raises(RejectedError, match="not partitioned"):
        apply_change(
            SPECS_BY_NAME[QueueName.VERIFY],
            _limits(QueueName.VERIFY, 2),
            LimitsChange(partition_worker_concurrency=1),
        )


def test_a_worker_limit_below_the_partition_limit_is_refused() -> None:
    """A worker could never run what one partition is allowed."""
    with pytest.raises(RejectedError, match=r"worker_concurrency 1 is below .* 2"):
        apply_change(
            SPECS_BY_NAME[QueueName.STAGE],
            _limits(QueueName.STAGE, 4, 2),
            LimitsChange(worker_concurrency=1),
        )


def test_limits_are_listed_per_stage() -> None:
    """Each stage, with what its limit approximates."""
    with client() as http:
        stages = {s["stage"]: s for s in http.get("/throughput").json()}
    assert stages["verify"]["worker_concurrency"] == 2
    assert stages["scan"]["partitioned"] is True
    assert stages["fingerprint"]["limit_driver"] == "CPU cores"


def test_lowering_a_limit_takes_the_new_value() -> None:
    """Accepted and reported back."""
    fake = FakeThroughput()
    with client(replace(backends(), throughput=fake)) as http:
        response = http.patch("/throughput/verify", json={"worker_concurrency": 1})
    assert response.status_code == 200
    assert response.json()["worker_concurrency"] == 1
    assert fake.stages[QueueName.VERIFY].worker_concurrency == 1


@pytest.mark.parametrize(
    ("change", "bound"),
    [
        ({"worker_concurrency": -1}, "greater than or equal to 1"),
        ({"worker_concurrency": 0}, "greater than or equal to 1"),
        ({"worker_concurrency": 65}, "less than or equal to 64"),
        ({"rate": {"limit": 0, "period": 60}}, "greater than or equal to 1"),
        ({"rate": {"limit": 10, "period": 0}}, "greater than 0"),
    ],
)
def test_an_out_of_range_limit_is_refused_naming_the_range(
    change: dict[str, object], bound: str
) -> None:
    """The error states the permitted range; the stored limit is untouched."""
    fake = FakeThroughput()
    with client(replace(backends(), throughput=fake)) as http:
        response = http.patch("/throughput/verify", json=change)
    assert response.status_code == 422
    assert bound in response.text
    assert fake.stages[QueueName.VERIFY].worker_concurrency == 2


def test_a_change_the_stage_cannot_take_is_refused_with_its_reason() -> None:
    """Cross-field refusals come back as a problem."""
    with client() as http:
        response = http.patch("/throughput/verify", json={"partition_worker_concurrency": 1})
    assert response.status_code == 422
    assert "not partitioned" in response.json()["detail"]


def test_an_unknown_stage_is_refused() -> None:
    """Only the pipeline's stages exist."""
    with client() as http:
        assert http.patch("/throughput/thumbnails", json={}).status_code == 422


class _Queue:
    """A queue that records writes and enforces DBOS's partition check."""

    def __init__(self, worker: int | None, partition: int | None) -> None:
        """Stored limits."""
        self.worker, self.partition = worker, partition
        self.limiter: dict[str, float] | None = None
        self.partition_limiter: dict[str, float] | None = None
        self.writes: list[str] = []

    async def get_worker_concurrency_async(self) -> int | None:
        """Stored worker limit."""
        return self.worker

    async def get_partition_worker_concurrency_async(self) -> int | None:
        """Stored partition limit."""
        return self.partition

    async def get_limiter_async(self) -> dict[str, float] | None:
        """Stored queue rate."""
        return self.limiter

    async def get_partition_limiter_async(self) -> dict[str, float] | None:
        """Stored partition rate."""
        return self.partition_limiter

    async def set_worker_concurrency_async(self, value: int) -> None:
        """Refuse a worker limit below the stored partition limit, as DBOS does."""
        if self.partition is not None and self.partition > value:
            raise ValueError("partition_worker_concurrency must be <= worker_concurrency")
        self.worker = value
        self.writes.append(f"worker={value}")

    async def set_partition_worker_concurrency_async(self, value: int) -> None:
        """Refuse a partition limit above the stored worker limit, as DBOS does."""
        if self.worker is not None and value > self.worker:
            raise ValueError("partition_worker_concurrency must be <= worker_concurrency")
        self.partition = value
        self.writes.append(f"partition={value}")

    async def set_limiter_async(self, value: dict[str, float]) -> None:
        """Store the queue rate."""
        self.limiter = value
        self.writes.append("rate")

    async def set_partition_limiter_async(self, value: dict[str, float]) -> None:
        """Store the partition rate."""
        self.partition_limiter = value
        self.writes.append("partition rate")


class _Workflows:
    """Retrieves queues by name."""

    def __init__(self, queues: dict[str, _Queue]) -> None:
        """The registered queues."""
        self.queues = queues

    async def retrieve_queue_async(self, name: str) -> _Queue | None:
        """A registered queue, or none."""
        return self.queues.get(name)


def _backend(queues: dict[str, _Queue]) -> DbosThroughput:
    """The backend over fake queues."""
    return DbosThroughput(cast(DBOSClient, _Workflows(queues)))


@pytest.mark.parametrize(
    ("change", "writes"),
    [
        (
            LimitsChange(worker_concurrency=8, partition_worker_concurrency=6),
            ["worker=8", "partition=6"],
        ),
        (
            LimitsChange(worker_concurrency=2, partition_worker_concurrency=1),
            ["partition=1", "worker=2"],
        ),
    ],
)
async def test_both_limits_are_written_in_an_order_dbos_accepts(
    change: LimitsChange, writes: list[str]
) -> None:
    """Raising writes the worker limit first; lowering below the partition limit, last."""
    queue = _Queue(worker=4, partition=3)
    updated = await _backend({"stage": queue}).change(QueueName.STAGE, change)
    assert queue.writes == writes
    assert (updated.worker_concurrency, updated.partition_worker_concurrency) == (
        change.worker_concurrency,
        change.partition_worker_concurrency,
    )


async def test_a_partitioned_stage_rate_is_per_partition() -> None:
    """Scan's rate is its partition limiter; verify's is the queue's."""
    scan, verify = _Queue(None, 4), _Queue(2, None)
    backend = _backend({"scan": scan, "verify": verify})
    await backend.change(QueueName.SCAN, LimitsChange(rate=Rate(limit=30, period=60)))
    await backend.change(QueueName.VERIFY, LimitsChange(rate=Rate(limit=10, period=1)))
    assert (scan.writes, verify.writes) == (["partition rate"], ["rate"])
    [listed_scan, listed_verify] = await backend.limits()
    assert listed_scan.rate == Rate(limit=30, period=60)
    assert listed_verify.rate == Rate(limit=10, period=1)


async def test_a_stage_no_worker_has_registered_is_not_found() -> None:
    """Workers register queues at startup; before that there is nothing to adjust."""
    with pytest.raises(NotFoundError, match="registered queue verify"):
        await _backend({}).change(QueueName.VERIFY, LimitsChange(worker_concurrency=1))
