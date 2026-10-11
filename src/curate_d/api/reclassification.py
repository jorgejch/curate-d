"""Asking the workers to classify again what a new taxonomy version would accept."""

from collections.abc import Awaitable, Callable

from dbos import DBOSClient, EnqueueOptions

from curate_d.runtime.queues import QueueName
from curate_d.workflows.classify import RECLASSIFY_WORKFLOW, reclassification_workflow_id

type Reclassify = Callable[[int], Awaitable[None]]


async def nothing(_version: int) -> None:
    """Re-classify nothing: for backends wired without workers to ask."""


def through(workflows: DBOSClient) -> Reclassify:
    """Enqueue re-classification for a taxonomy version, once per version."""

    async def reclassify(version: int) -> None:
        """Enqueue `reclassify_rejected` on the workers' maintenance queue."""
        options: EnqueueOptions = {
            "workflow_name": RECLASSIFY_WORKFLOW,
            "queue_name": QueueName.MAINTENANCE.value,
            "workflow_id": reclassification_workflow_id(version),
        }
        await workflows.enqueue_async(options, version)

    return reclassify
