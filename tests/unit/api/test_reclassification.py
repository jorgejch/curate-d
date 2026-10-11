"""Re-classification is enqueued for the workers once per taxonomy version."""

from typing import cast

from dbos import DBOSClient, EnqueueOptions

from curate_d.api.reclassification import through


class _Workflows:
    """Records enqueues."""

    def __init__(self) -> None:
        """Nothing enqueued yet."""
        self.enqueued: list[tuple[EnqueueOptions, int]] = []

    async def enqueue_async(self, options: EnqueueOptions, version: int) -> None:
        """Record the enqueue."""
        self.enqueued.append((options, version))


async def test_a_taxonomy_version_is_reclassified_on_maintenance_once() -> None:
    """By the registered name, under an identity of the version."""
    workflows = _Workflows()
    await through(cast(DBOSClient, workflows))(7)
    assert workflows.enqueued == [
        (
            {
                "workflow_name": "reclassify_rejected",
                "queue_name": "maintenance",
                "workflow_id": "reclassify:taxonomy-7",
            },
            7,
        )
    ]
