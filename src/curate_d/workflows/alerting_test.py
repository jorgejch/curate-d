"""A workflow that always fails, to test the worker error alert end to end.

The control plane enqueues it on a queue only one worker profile dequeues, so the failure
happens in that profile's process and is exported under its service name. It fails the way
an unanticipated bug does: a step raises and nothing catches it, so DBOS ends the step and
workflow spans with an error status, logs the exception at ERROR, and records the workflow
as ERROR. It reads and writes no ledger state; its workflow row is the only trace it leaves,
and the workflow-history prune removes it like any other.
"""

from typing import Final

from dbos import DBOS

from curate_d.runtime.profiles import WorkerProfile
from curate_d.runtime.queues import QueueName

ALERTING_TEST_WORKFLOW: Final = "fail_for_alerting_test"

# A queue each profile dequeues from and no other does, so the failure lands in the
# profile asked for. Both are lightly loaded in dev; a test waits behind any work already
# running there.
_QUEUES: Final[dict[WorkerProfile, QueueName]] = {
    WorkerProfile.IO: QueueName.MAINTENANCE,
    WorkerProfile.CPU: QueueName.FINGERPRINT,
}


class WorkerAlertingTestError(RuntimeError):
    """A deliberate failure, raised only to test worker-error alerting."""


def alerting_test_queue(profile: WorkerProfile) -> QueueName:
    """Return the queue that routes an alerting test to `profile`'s workers."""
    return _QUEUES[profile]


@DBOS.step()
async def _fail_step(profile: str) -> None:
    """Fail as a step with a bug would. Not retried: DBOS retries a step only when asked.

    Raises:
        WorkerAlertingTestError: always.
    """
    raise WorkerAlertingTestError(
        f"deliberate worker error for testing worker-error alerting (curate-d-worker-{profile})"
    )


@DBOS.workflow(name=ALERTING_TEST_WORKFLOW)
async def fail_for_alerting_test(profile: str) -> None:
    """Run the failing step and let its error end the workflow.

    Args:
        profile: The profile the test was routed to, named in the error's message.

    Raises:
        WorkerAlertingTestError: always.
    """
    await _fail_step(profile)
