"""Queue topology.

One queue per pipeline stage, each limited by whatever resource actually constrains it
rather than by a single global parallelism number. DBOS counts workflows, but the real
constraint on transfers is in-flight bytes against scratch capacity — sixteen videos and
sixteen invoices are not comparable loads — so transfers are split by size class, which
approximates a byte semaphore and independently stops a batch of large files starving
small ones behind it.

Two properties are easy to get wrong and expensive to diagnose:

**Limits are per worker, not global.** A global limit counts every pending workflow
against it, including ones left over from a previous application version, so a
half-drained deployment quietly consumes the transfer budget with no visible symptom.
Per-worker limits do not have this property, and they are enforced from each process's
own in-memory count of running workflows, so they ask nothing of the deployment.
(Executor identity is a separate concern, and a recovery one — see `identity.py`.) This
holds for the partitioned queues too: they set DBOS's `partition_worker_concurrency`, never
`partition_concurrency`, whose count is every PENDING workflow in the partition from any
application version. Their per-partition rate limit stays global on purpose: a provider's
quota is spent by every worker alike, and a rate window only counts recent starts, so a
workflow stranded by an old version ages out of it instead of holding it.

**Registration must not clobber runtime changes.** The correct transfer concurrency is a
function of observed disk pressure and provider behaviour, which is not knowable before
running against a real library, so the control plane adjusts these at runtime. Startup
registration therefore uses `never_update`: the values below are what a fresh database
gets, not what every restart reasserts.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Literal, TypedDict

from dbos import DBOS, Queue

# Startup registration must not overwrite a limit the control plane set at runtime.
REGISTRATION_CONFLICT_POLICY: Final[Literal["never_update"]] = "never_update"


class RateLimitSpec(TypedDict):
    """The rate limiter DBOS takes and reports: at most `limit` starts per `period` seconds.

    The shape of DBOS's own limiter dict, declared here so callers need no private DBOS
    module; TypedDicts compare structurally, so DBOS accepts it as its own.
    """

    limit: int
    period: float


class QueueName(StrEnum):
    """The stages work is queued on."""

    SCAN = "scan"
    STAGE = "stage"
    FINGERPRINT = "fingerprint"
    CLASSIFY_CHEAP = "classify-cheap"
    CLASSIFY_LLM = "classify-llm"
    TRANSFER_SMALL = "transfer-small"
    TRANSFER_LARGE = "transfer-large"
    VERIFY = "verify"
    MAINTENANCE = "maintenance"


@dataclass(frozen=True)
class RateLimit:
    """At most `limit` workflows started per `period` seconds."""

    limit: int
    period: float


@dataclass(frozen=True)
class QueueSpec:
    """One queue's registration parameters: name, concurrency, and partitioning.

    `limit_driver` names the resource the concurrency limit approximates (CPU cores,
    scratch storage, spend cap, ...), for readers of `QUEUE_SPECS` rather than for any
    runtime check. Every concurrency limit is per worker process: `worker_concurrency`
    across the whole queue, `partition_worker_concurrency` within each partition. There is
    deliberately no global form of either — see the module docstring. A partitioned spec
    must set `partition_worker_concurrency`, since a partitioned queue with no partition
    limit is an unpartitioned queue with extra bookkeeping.
    """

    name: QueueName
    limit_driver: str
    worker_concurrency: int | None = None
    partitioned: bool = False
    partition_worker_concurrency: int | None = None
    partition_limiter: RateLimit | None = None

    def __post_init__(self) -> None:
        """Reject a partitioned spec with no per-worker partition concurrency limit.

        Raises:
            ValueError: `partitioned` is True but `partition_worker_concurrency` is None.
        """
        if self.partitioned and self.partition_worker_concurrency is None:
            raise ValueError(
                f"{self.name}: a partitioned queue whose partitions have no limit is just "
                f"an unpartitioned queue with extra bookkeeping"
            )


# Concurrency defaults, each per worker process (and, on a partitioned queue, per
# partition). Deliberately conservative: they are the values a fresh database starts
# from, and the right numbers are discovered by running against a real library.
DEFAULT_CPU_CONCURRENCY = 2
DEFAULT_TRANSFER_SMALL_CONCURRENCY = 1
DEFAULT_TRANSFER_LARGE_CONCURRENCY = 1

# Transfers are bounded by the pod's own scratch storage, whatever provider they come from,
# so each transfer queue also caps the worker as a whole: without it a pod would run
# (providers times partition limit) at once. Started at the minimum workable value and raised
# from observation. DBOS requires it to be at least the partition limit.
DEFAULT_TRANSFER_WORKER_CONCURRENCY = 1

# Verification reads no content: it compares digests the providers report and records a
# verification level, so what it spends is ledger writes, not cores or scratch.
DEFAULT_VERIFY_CONCURRENCY = 2

# Scheduled housekeeping — token refresh, the reconciliation sweep, full rescans — which
# enqueues the real work rather than doing it, so one at a time per worker is plenty.
DEFAULT_MAINTENANCE_CONCURRENCY = 1

# Provider-facing queues are partitioned on provider identity rather than duplicated per
# provider: a burst against one provider cannot stall another, and adding a provider is a
# new partition key instead of a new queue registration.
DEFAULT_PROVIDER_SCAN_CONCURRENCY = 4
DEFAULT_PROVIDER_SCAN_RATE = RateLimit(limit=60, period=60.0)
# Fetching content into staging: bounded per provider by download bandwidth, and per worker
# by the staging capacity every upload reserves before it starts.
DEFAULT_PROVIDER_STAGE_CONCURRENCY = 2
DEFAULT_STAGE_WORKER_CONCURRENCY = 4
DEFAULT_PROVIDER_STAGE_RATE = RateLimit(limit=120, period=60.0)
DEFAULT_PROVIDER_TRANSFER_RATE = RateLimit(limit=120, period=60.0)

# The remote classification tier is governed by spend as well as rate; the spend cap
# lives in the classification tier itself, this bounds request rate.
DEFAULT_LLM_RATE = RateLimit(limit=30, period=60.0)

QUEUE_SPECS: tuple[QueueSpec, ...] = (
    QueueSpec(
        name=QueueName.SCAN,
        limit_driver="provider request quota",
        partitioned=True,
        partition_worker_concurrency=DEFAULT_PROVIDER_SCAN_CONCURRENCY,
        partition_limiter=DEFAULT_PROVIDER_SCAN_RATE,
    ),
    QueueSpec(
        name=QueueName.STAGE,
        limit_driver="provider download bandwidth and staging capacity",
        worker_concurrency=DEFAULT_STAGE_WORKER_CONCURRENCY,
        partitioned=True,
        partition_worker_concurrency=DEFAULT_PROVIDER_STAGE_CONCURRENCY,
        partition_limiter=DEFAULT_PROVIDER_STAGE_RATE,
    ),
    QueueSpec(
        name=QueueName.FINGERPRINT,
        limit_driver="CPU cores",
        worker_concurrency=DEFAULT_CPU_CONCURRENCY,
    ),
    QueueSpec(
        name=QueueName.CLASSIFY_CHEAP,
        limit_driver="CPU cores",
        worker_concurrency=DEFAULT_CPU_CONCURRENCY,
    ),
    QueueSpec(
        name=QueueName.CLASSIFY_LLM,
        limit_driver="spend cap and request rate",
        worker_concurrency=DEFAULT_CPU_CONCURRENCY,
        partitioned=False,
    ),
    QueueSpec(
        name=QueueName.TRANSFER_SMALL,
        limit_driver="scratch storage",
        worker_concurrency=DEFAULT_TRANSFER_WORKER_CONCURRENCY,
        partitioned=True,
        partition_worker_concurrency=DEFAULT_TRANSFER_SMALL_CONCURRENCY,
        partition_limiter=DEFAULT_PROVIDER_TRANSFER_RATE,
    ),
    QueueSpec(
        name=QueueName.TRANSFER_LARGE,
        limit_driver="scratch storage and bandwidth",
        worker_concurrency=DEFAULT_TRANSFER_WORKER_CONCURRENCY,
        partitioned=True,
        partition_worker_concurrency=DEFAULT_TRANSFER_LARGE_CONCURRENCY,
        partition_limiter=DEFAULT_PROVIDER_TRANSFER_RATE,
    ),
    QueueSpec(
        name=QueueName.VERIFY,
        limit_driver="ledger writes; comparison only",
        worker_concurrency=DEFAULT_VERIFY_CONCURRENCY,
    ),
    QueueSpec(
        name=QueueName.MAINTENANCE,
        limit_driver="scheduled housekeeping; enqueues work rather than doing it",
        worker_concurrency=DEFAULT_MAINTENANCE_CONCURRENCY,
    ),
)

SPECS_BY_NAME: dict[QueueName, QueueSpec] = {spec.name: spec for spec in QUEUE_SPECS}

PARTITIONED_QUEUES: frozenset[QueueName] = frozenset(
    spec.name for spec in QUEUE_SPECS if spec.partitioned
)

# Which profile dequeues from which stage. The split exists for credential blast radius
# and resource shape, not for concurrency — see design.md. Verification sits with the
# transfer: it compares digests the I/O profile collects from the providers (rclone
# `operations/stat` with hashes, or an `operations/hashsumfile` read-back) and decodes
# no content, so it needs neither the CPU profile's cores nor its isolation.
IO_QUEUES: tuple[QueueName, ...] = (
    QueueName.SCAN,
    QueueName.STAGE,
    QueueName.CLASSIFY_LLM,
    QueueName.TRANSFER_SMALL,
    QueueName.TRANSFER_LARGE,
    QueueName.VERIFY,
    # Scheduled workflows are routed here rather than to DBOS's internal queue, which
    # every worker listens to: token refresh calls providers, which only this profile can
    # reach.
    QueueName.MAINTENANCE,
)
CPU_QUEUES: tuple[QueueName, ...] = (
    QueueName.FINGERPRINT,
    QueueName.CLASSIFY_CHEAP,
)


def _registration_kwargs(spec: QueueSpec) -> dict[str, object]:
    """Return the arguments that express one spec, shared by the sync and async registrars."""
    limiter: RateLimitSpec | None = (
        RateLimitSpec(limit=spec.partition_limiter.limit, period=spec.partition_limiter.period)
        if spec.partition_limiter is not None
        else None
    )
    kwargs: dict[str, object] = {
        "worker_concurrency": spec.worker_concurrency,
        "on_conflict": REGISTRATION_CONFLICT_POLICY,
    }
    if spec.partitioned:
        # Setting a partition_* limit is what makes a queue partitioned; DBOS derives its
        # partition_queue column from them and takes no separate flag. The worker form,
        # not partition_concurrency: that one counts every PENDING workflow in the
        # partition, including a previous version's, against all executors at once.
        kwargs["partition_worker_concurrency"] = spec.partition_worker_concurrency
        kwargs["partition_limiter"] = limiter
    else:
        kwargs["limiter"] = limiter
    return kwargs


def register(spec: QueueSpec) -> Queue:
    """Register one queue, preserving any limit already set at runtime."""
    return DBOS.register_queue(spec.name.value, **_registration_kwargs(spec))  # type: ignore[arg-type]


async def register_async(spec: QueueSpec) -> Queue:
    """Async form, for entry points that register from inside a running event loop."""
    return await DBOS.register_queue_async(spec.name.value, **_registration_kwargs(spec))  # type: ignore[arg-type]


def register_all(specs: Sequence[QueueSpec] = QUEUE_SPECS) -> dict[QueueName, Queue]:
    """Register the whole topology. Call after DBOS.launch()."""
    return {spec.name: register(spec) for spec in specs}


async def register_all_async(
    specs: Sequence[QueueSpec] = QUEUE_SPECS,
) -> dict[QueueName, Queue]:
    """Async form of `register_all`, for entry points already inside an event loop."""
    return {spec.name: await register_async(spec) for spec in specs}
