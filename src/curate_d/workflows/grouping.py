"""The daily event-grouping pass over geotagged photos.

Clustering needs the whole library's photos at once, so it runs as a scheduled pass rather
than per item. One ledger transaction reads every geotagged photo's time and place,
clusters them, writes each photo's event group where it changed — clearing it for a photo
no longer in any event — and audits each change. A photo's facts change only here and
through review; a changed group is a changed fact, and reconciliation re-renders from it.
"""

from datetime import datetime
from typing import Final

from dbos import DBOS, ClientScheduleInput

from curate_d.classification.clustering import cluster
from curate_d.classification.store import ClassificationRepository
from curate_d.domain import AuditEntry, AuditEntryType, ContentObjectId
from curate_d.ledger import AuditRepository
from curate_d.observability import get_logger
from curate_d.runtime.queues import QueueName
from curate_d.runtime.services import current

logger = get_logger(__name__)

SCHEDULE_NAME: Final = "group-events"
# Explicit, because stored schedule rows start a workflow by name: renaming the
# function must not orphan them or their firing history.
GROUP_EVENTS_WORKFLOW: Final = "group_events"
DECIDED_BY: Final = "clustering"


@DBOS.step()
async def _grouping_now_step() -> datetime:
    """Read the clock once, so a replay records the same time."""
    return current().clock()


async def _group_tx(now: datetime) -> int:
    """Cluster every geotagged photo and record the groups that changed."""
    connection = await current().datasource.sql_session().connection()
    classifications = ClassificationRepository(connection)
    assigned = cluster(await classifications.geotagged_photos())
    wanted: dict[ContentObjectId, str | None] = dict(assigned)
    for content_object_id in await classifications.grouped():
        wanted.setdefault(content_object_id, None)
    changed = await classifications.set_event_groups(wanted)
    audit = AuditRepository(connection)
    for content_object_id in changed:
        await audit.append(
            AuditEntry(
                entry_type=AuditEntryType.CLASSIFICATION_DECIDED,
                occurred_at=now,
                decided_by=DECIDED_BY,
                details={"event_group": wanted[content_object_id]},
                content_object_id=content_object_id,
            )
        )
    return len(changed)


@DBOS.workflow(name=GROUP_EVENTS_WORKFLOW)
async def group_events(scheduled_time: datetime, context: object) -> None:
    """Recompute event groups across the library."""
    changed = await current().datasource.run_tx_step_async(
        {"name": "group_events"}, _group_tx, await _grouping_now_step()
    )
    logger.info("grouping.pass", changed=changed)


def schedule() -> ClientScheduleInput:
    """Return the persisted schedule of the daily grouping pass."""
    return {
        "schedule_name": SCHEDULE_NAME,
        "workflow_name": GROUP_EVENTS_WORKFLOW,
        "schedule": "45 2 * * *",
        "context": None,
        "queue_name": QueueName.MAINTENANCE.value,
    }
