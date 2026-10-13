"""Each stage's concurrency and rate limits, viewed and adjusted while workers run.

Limits live in the workflow system database. Workers reread their queue's row on every
poll, so a change applies without a restart; work already running finishes, since a
lower limit only stops new work starting. Worker startup registers queues without
overwriting a stored row, so an adjusted value survives restarts.

Concurrency is per worker process, as registered (`runtime/queues.py`): a partitioned
queue's worker limit must stay at least its per-partition limit. A partitioned queue's
rate is per partition; an unpartitioned queue's is for the whole queue.
"""

from collections.abc import Callable, Sequence
from typing import Annotated, Final, Protocol

from dbos import DBOSClient, Queue
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from curate_d.api.errors import NOT_FOUND, REJECTED, NotFoundError, RejectedError, problems
from curate_d.runtime.queues import (
    QUEUE_SPECS,
    SPECS_BY_NAME,
    QueueName,
    QueueSpec,
    RateLimitSpec,
)

MAX_CONCURRENCY: Final = 64
MAX_RATE: Final = 10_000
MAX_PERIOD_SECONDS: Final = 3600.0


class Rate(BaseModel):
    """At most `limit` workflows started per `period` seconds."""

    limit: int = Field(ge=1, le=MAX_RATE)
    period: float = Field(gt=0, le=MAX_PERIOD_SECONDS)


class StageLimits(BaseModel):
    """One stage's limits as stored."""

    stage: QueueName
    limit_driver: str
    partitioned: bool
    worker_concurrency: int | None = None
    partition_worker_concurrency: int | None = None
    rate: Rate | None = None


class LimitsChange(BaseModel):
    """The limits to change; those left out keep their value."""

    worker_concurrency: int | None = Field(default=None, ge=1, le=MAX_CONCURRENCY)
    partition_worker_concurrency: int | None = Field(default=None, ge=1, le=MAX_CONCURRENCY)
    rate: Rate | None = None


def apply_change(spec: QueueSpec, current: StageLimits, change: LimitsChange) -> StageLimits:
    """The limits after `change`, checked against the stage's shape.

    Raises:
        RejectedError: a partition limit for an unpartitioned stage, or a worker limit
            below the partition limit.
    """
    if change.partition_worker_concurrency is not None and not spec.partitioned:
        raise RejectedError(f"{spec.name.value} is not partitioned; it has no partition limit")
    updated = current.model_copy(
        update={
            name: value
            for name, value in (
                ("worker_concurrency", change.worker_concurrency),
                ("partition_worker_concurrency", change.partition_worker_concurrency),
                ("rate", change.rate),
            )
            if value is not None
        }
    )
    worker, partition = updated.worker_concurrency, updated.partition_worker_concurrency
    if worker is not None and partition is not None and worker < partition:
        raise RejectedError(
            f"{spec.name.value}: worker_concurrency {worker} is below "
            f"partition_worker_concurrency {partition}; it must be at least that"
        )
    return updated


class ThroughputBackend(Protocol):
    """Stage limits in the workflow system database."""

    async def limits(self) -> Sequence[StageLimits]:
        """Every stage's limits, in pipeline order."""
        ...

    async def change(self, stage: QueueName, change: LimitsChange) -> StageLimits:
        """Apply `change` to a stage's limits.

        Raises:
            NotFoundError: no worker has registered the stage yet.
            RejectedError: the change does not fit the stage; nothing was written.
        """
        ...


def router(backend: Callable[[Request], ThroughputBackend]) -> APIRouter:
    """The throughput endpoints over `backend`."""
    routes = APIRouter(prefix="/throughput", tags=["throughput"])
    uses = Depends(backend)

    @routes.get("", response_model=list[StageLimits])
    async def get_limits(throughput: Annotated[ThroughputBackend, uses]) -> Sequence[StageLimits]:
        """Every stage's concurrency and rate limits."""
        return await throughput.limits()

    @routes.patch("/{stage}", response_model=StageLimits, responses=problems(NOT_FOUND, REJECTED))
    async def change_limits(
        stage: QueueName, change: LimitsChange, throughput: Annotated[ThroughputBackend, uses]
    ) -> StageLimits:
        """Adjust a stage's limits; workers pick them up on their next poll."""
        return await throughput.change(stage, change)

    return routes


def _rate(limiter: RateLimitSpec | None) -> Rate | None:
    """A stored rate limit."""
    return None if limiter is None else Rate(limit=limiter["limit"], period=limiter["period"])


class DbosThroughput:
    """Stage limits read and written through a workflow client."""

    def __init__(self, workflows: DBOSClient) -> None:
        """Bind the workflow client."""
        self._workflows = workflows

    async def _queue(self, stage: QueueName) -> Queue:
        """The stage's registered queue.

        Raises:
            NotFoundError: no worker has registered it yet.
        """
        queue = await self._workflows.retrieve_queue_async(stage.value)
        if queue is None:
            raise NotFoundError("registered queue", stage.value)
        return queue

    async def _limits(self, spec: QueueSpec, queue: Queue) -> StageLimits:
        """What is stored for one stage."""
        limiter = (
            await queue.get_partition_limiter_async()
            if spec.partitioned
            else await queue.get_limiter_async()
        )
        return StageLimits(
            stage=spec.name,
            limit_driver=spec.limit_driver,
            partitioned=spec.partitioned,
            worker_concurrency=await queue.get_worker_concurrency_async(),
            partition_worker_concurrency=await queue.get_partition_worker_concurrency_async(),
            rate=_rate(limiter),
        )

    async def limits(self) -> Sequence[StageLimits]:
        """Every registered stage's limits, in pipeline order."""
        found = []
        for spec in QUEUE_SPECS:
            queue = await self._workflows.retrieve_queue_async(spec.name.value)
            if queue is not None:
                found.append(await self._limits(spec, queue))
        return found

    async def change(self, stage: QueueName, change: LimitsChange) -> StageLimits:
        """Check the change, then write the fields it sets."""
        spec = SPECS_BY_NAME[stage]
        queue = await self._queue(stage)
        current = await self._limits(spec, queue)
        updated = apply_change(spec, current, change)
        # DBOS checks the partition limit against the worker limit on every write, so
        # lowering the worker limit below the stored partition limit writes that first.
        partition_first = (
            updated.worker_concurrency is not None
            and current.partition_worker_concurrency is not None
            and updated.worker_concurrency < current.partition_worker_concurrency
        )
        if partition_first and change.partition_worker_concurrency is not None:
            await queue.set_partition_worker_concurrency_async(change.partition_worker_concurrency)
        if change.worker_concurrency is not None:
            await queue.set_worker_concurrency_async(change.worker_concurrency)
        if not partition_first and change.partition_worker_concurrency is not None:
            await queue.set_partition_worker_concurrency_async(change.partition_worker_concurrency)
        if change.rate is not None:
            limiter: RateLimitSpec = {"limit": change.rate.limit, "period": change.rate.period}
            if spec.partitioned:
                await queue.set_partition_limiter_async(limiter)
            else:
                await queue.set_limiter_async(limiter)
        return await self._limits(spec, queue)
