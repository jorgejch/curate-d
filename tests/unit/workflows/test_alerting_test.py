"""The deliberate worker failure: where it is routed and how it fails."""

import pytest

from curate_d.runtime.profiles import WorkerProfile
from curate_d.workflows import alerting_test
from curate_d.workflows.alerting_test import WorkerAlertingTestError, alerting_test_queue


def test_each_profile_is_routed_to_a_queue_only_it_dequeues() -> None:
    """The failure lands in the profile asked for, never the other one."""
    for profile in WorkerProfile:
        queue = alerting_test_queue(profile)
        assert queue in profile.queues
        others = [other for other in WorkerProfile if other is not profile]
        assert all(queue not in other.queues for other in others)


async def test_the_step_raises_an_alerting_test_error_naming_the_worker() -> None:
    """Outside a launched DBOS the step runs as a plain coroutine and raises."""
    with pytest.raises(WorkerAlertingTestError, match=r"deliberate worker error .*worker-cpu"):
        await alerting_test._fail_step("cpu")
