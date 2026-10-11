"""Queue topology shape.

The registration calls need a database; which queues exist, how they are limited, and
which profile listens to them do not.
"""

import pytest

from curate_d.runtime.queues import (
    CPU_QUEUES,
    IO_QUEUES,
    PARTITIONED_QUEUES,
    QUEUE_SPECS,
    REGISTRATION_CONFLICT_POLICY,
    SPECS_BY_NAME,
    QueueName,
    QueueSpec,
    RateLimit,
    _registration_kwargs,
)


def test_every_stage_has_a_queue() -> None:
    """Declare a queue spec for every value in QueueName."""
    assert {spec.name for spec in QUEUE_SPECS} == set(QueueName)


def test_no_queue_relies_on_a_global_concurrency_limit() -> None:
    """A global limit counts workflows left over from a previous version against it."""
    for spec in QUEUE_SPECS:
        assert (
            spec.worker_concurrency is not None or spec.partition_worker_concurrency is not None
        ), f"{spec.name} has neither a per-worker nor a per-partition limit"


def test_provider_facing_queues_are_partitioned_on_provider_identity() -> None:
    """A burst against one provider must not stall another."""
    assert {
        QueueName.SCAN,
        QueueName.STAGE,
        QueueName.TRANSFER_SMALL,
        QueueName.TRANSFER_LARGE,
    } == PARTITIONED_QUEUES


def test_transfers_are_split_by_size_class() -> None:
    """Approximates a byte semaphore, and stops large files starving small ones.

    Both start at the minimum workable limit and are raised from observation; large
    transfers are never allowed more concurrency than small ones.
    """
    small = next(s for s in QUEUE_SPECS if s.name is QueueName.TRANSFER_SMALL)
    large = next(s for s in QUEUE_SPECS if s.name is QueueName.TRANSFER_LARGE)
    assert small.partition_worker_concurrency is not None
    assert large.partition_worker_concurrency is not None
    assert large.partition_worker_concurrency <= small.partition_worker_concurrency


def test_registration_never_overwrites_a_runtime_adjustment() -> None:
    """Correct transfer concurrency is discovered by running, not configured up front."""
    assert REGISTRATION_CONFLICT_POLICY == "never_update"


def test_a_partitioned_queue_without_a_partition_limit_is_rejected() -> None:
    """Reject a partitioned QueueSpec that has no partition_worker_concurrency."""
    with pytest.raises(ValueError, match="extra bookkeeping"):
        QueueSpec(name=QueueName.SCAN, limit_driver="quota", partitioned=True)


def test_a_partition_rate_limit_alone_does_not_satisfy_the_partition_limit() -> None:
    """A rate limit bounds starts, not how many run at once, so it is not enough."""
    with pytest.raises(ValueError, match="extra bookkeeping"):
        QueueSpec(
            name=QueueName.SCAN,
            limit_driver="quota",
            partitioned=True,
            partition_limiter=RateLimit(limit=1, period=1.0),
        )


def test_partitioned_queues_register_a_per_worker_partition_limit() -> None:
    """Register partition limits per worker, never as a global partition_concurrency."""
    for spec in QUEUE_SPECS:
        if not spec.partitioned:
            continue
        kwargs = _registration_kwargs(spec)
        assert kwargs["partition_worker_concurrency"] == spec.partition_worker_concurrency
        assert "partition_concurrency" not in kwargs
        assert "global_concurrency" not in kwargs
        assert "concurrency" not in kwargs


def test_partitioned_queues_apply_their_rate_limit_per_partition() -> None:
    """Pass a partitioned spec's rate limit as partition_limiter, not the queue-wide one."""
    spec = QueueSpec(
        name=QueueName.SCAN,
        limit_driver="quota",
        partitioned=True,
        partition_worker_concurrency=3,
        partition_limiter=RateLimit(limit=10, period=5.0),
    )
    kwargs = _registration_kwargs(spec)
    assert kwargs["partition_limiter"] == {"limit": 10, "period": 5.0}
    assert "limiter" not in kwargs


def test_a_partitioned_queue_keeps_its_queue_wide_worker_limit() -> None:
    """Forward worker_concurrency on a partitioned spec instead of dropping it."""
    spec = QueueSpec(
        name=QueueName.TRANSFER_SMALL,
        limit_driver="scratch storage",
        worker_concurrency=8,
        partitioned=True,
        partition_worker_concurrency=4,
    )
    assert _registration_kwargs(spec)["worker_concurrency"] == 8


def test_unpartitioned_queues_register_no_partition_limits() -> None:
    """Register an unpartitioned spec with only its per-worker limit and queue-wide rate."""
    spec = QueueSpec(
        name=QueueName.CLASSIFY_LLM,
        limit_driver="spend cap",
        worker_concurrency=2,
        partition_limiter=RateLimit(limit=30, period=60.0),
    )
    kwargs = _registration_kwargs(spec)
    assert kwargs["worker_concurrency"] == 2
    assert kwargs["limiter"] == {"limit": 30, "period": 60.0}
    assert not any(key.startswith("partition_") for key in kwargs)


def test_every_registration_preserves_runtime_adjustments() -> None:
    """Pass the never_update conflict policy for every spec."""
    for spec in QUEUE_SPECS:
        assert _registration_kwargs(spec)["on_conflict"] == REGISTRATION_CONFLICT_POLICY


def test_the_provider_facing_queues_are_rate_limited() -> None:
    """Give every partitioned queue a positive RateLimit."""
    for spec in QUEUE_SPECS:
        if spec.partitioned:
            assert isinstance(spec.partition_limiter, RateLimit)
            assert spec.partition_limiter.limit > 0
            assert spec.partition_limiter.period > 0


def test_the_two_worker_profiles_cover_every_queue_without_overlap() -> None:
    """An unlistened queue silently accumulates work; a shared one defeats the split."""
    assert set(IO_QUEUES) | set(CPU_QUEUES) == set(QueueName)
    assert not set(IO_QUEUES) & set(CPU_QUEUES)


def test_the_credential_holding_profile_does_not_decode_untrusted_content() -> None:
    """The split exists so a decoder compromise lands where no provider tokens live."""
    assert QueueName.FINGERPRINT in CPU_QUEUES
    assert QueueName.SCAN in IO_QUEUES
    assert QueueName.TRANSFER_SMALL in IO_QUEUES
    assert QueueName.FINGERPRINT not in IO_QUEUES


def test_every_spec_names_the_resource_that_limits_it() -> None:
    """Set a non-empty limit_driver on every queue spec."""
    assert all(spec.limit_driver for spec in QUEUE_SPECS)


def test_verification_runs_on_the_io_profile_beside_the_transfer() -> None:
    """Verify compares provider-reported digests, so it is an I/O stage, not a CPU one."""
    assert QueueName.VERIFY in IO_QUEUES
    assert QueueName.VERIFY not in CPU_QUEUES


def test_verification_is_not_limited_by_cpu_cores() -> None:
    """Verify reads no content; its limit tracks ledger writes, not cores."""
    spec = SPECS_BY_NAME[QueueName.VERIFY]
    assert "CPU" not in spec.limit_driver
