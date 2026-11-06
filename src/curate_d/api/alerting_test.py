"""Asking a worker profile to fail on purpose, to test the worker error alert."""

from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from dbos import DBOSClient, EnqueueOptions
from pydantic import BaseModel

from curate_d.clock import utc_now
from curate_d.runtime.profiles import WorkerProfile
from curate_d.runtime.workflow_ids import alerting_test_workflow_id
from curate_d.workflows.alerting_test import ALERTING_TEST_WORKFLOW, alerting_test_queue

_MINUTE_FORMAT = "%Y%m%dT%H%MZ"


class WorkerAlertingTest(BaseModel):
    """A deliberate worker failure, enqueued: which profile, on which queue, under which ID."""

    profile: WorkerProfile
    queue: str
    workflow_id: str


class WorkerAlertingTestBackend(Protocol):
    """Enqueues the deliberate worker failure."""

    async def fail(self, profile: WorkerProfile) -> WorkerAlertingTest:
        """Enqueue one failure for `profile`'s workers and say where it went."""
        ...


class DbosWorkerAlertingTest:
    """Enqueues the failing workflow through the workflow system database."""

    def __init__(self, workflows: DBOSClient, *, clock: Callable[[], datetime] = utc_now) -> None:
        """Enqueue through `workflows`, keyed on the minute `clock` reads."""
        self._workflows = workflows
        self._clock = clock

    async def fail(self, profile: WorkerProfile) -> WorkerAlertingTest:
        """Enqueue the failing workflow on a queue only `profile` dequeues, once a minute."""
        queue = alerting_test_queue(profile).value
        workflow_id = alerting_test_workflow_id(
            profile.value, self._clock().strftime(_MINUTE_FORMAT)
        )
        options: EnqueueOptions = {
            "workflow_name": ALERTING_TEST_WORKFLOW,
            "queue_name": queue,
            "workflow_id": workflow_id,
        }
        await self._workflows.enqueue_async(options, profile.value)
        return WorkerAlertingTest(profile=profile, queue=queue, workflow_id=workflow_id)
