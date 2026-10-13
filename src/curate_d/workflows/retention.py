"""The scheduled prune of completed workflow history.

`runtime/retention.py` holds the prune and the reasoning behind it; this module runs it on
a persisted schedule. The cutoff is measured from the tick's scheduled time, not the clock,
so a recovered or backfilled run applies the boundary its tick would have. The prune is
idempotent, so a re-executed step only finds less to delete.
"""

from datetime import datetime
from typing import Final

from dbos import DBOS, ScheduleInput

from curate_d.config import Settings
from curate_d.runtime.queues import QueueName
from curate_d.runtime.retention import prune_workflow_history
from curate_d.runtime.services import current

SCHEDULE_NAME: Final = "prune-workflow-history"


@DBOS.step()
async def _prune_history_step(scheduled_time: datetime) -> str:
    """Prune history that completed more than the retention period before `scheduled_time`.

    Returns:
        The cutoff applied, in ISO 8601.
    """
    window = await prune_workflow_history(current().settings, now=scheduled_time)
    return window.cutoff.isoformat()


@DBOS.workflow()
async def prune_history(scheduled_time: datetime, context: object) -> None:
    """Delete workflow records older than the configured retention period."""
    await _prune_history_step(scheduled_time)


def schedule(settings: Settings) -> ScheduleInput:
    """Return the persisted schedule of the workflow-history prune."""
    return {
        "schedule_name": SCHEDULE_NAME,
        "workflow_fn": prune_history,
        "schedule": settings.workflow_retention_schedule,
        "context": None,
        "queue_name": QueueName.MAINTENANCE.value,
    }
