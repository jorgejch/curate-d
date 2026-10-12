"""The scheduled recovery sweep.

`runtime/sweep.py` holds the sweep and its gates; this module runs it on a persisted
schedule, on the I/O profile's `maintenance` queue. The sweep runs in one step. Its DBOS
calls inside that step (forks, cancels, re-enqueues, status reads) are not checkpointed,
so a re-executed step repeats the whole sweep: safe because every fork is keyed by the
deterministic successor ID and every cancel and re-enqueue is gated on a re-read status
or on executor ownership. That departs from the dbos-python skill's rule against starting
workflows from a step, and CLAUDE.md records it.

The step returns the outcome as plain values, so its checkpoint pickles no class of ours.
"""

import dataclasses
from datetime import datetime

from dbos import DBOS, ScheduleInput

from curate_d.config import Settings
from curate_d.runtime.queues import QueueName
from curate_d.runtime.services import current
from curate_d.runtime.sweep import RECOVERY_SCHEDULE_NAME, DBOSRecoveryControl, run_recovery_sweep

type SweepSummary = dict[str, object]
"""`SweepOutcome` as plain tuples, ints and strings."""


@DBOS.step()
async def _recovery_sweep_step() -> SweepSummary:
    """Run one gated recovery sweep as a sweeper on this build's version.

    Returns:
        The sweep's outcome as a dict of plain values.
    """
    outcome = await run_recovery_sweep(
        current().settings,
        control=DBOSRecoveryControl(),
        own_version=DBOS.application_version,
    )
    return dataclasses.asdict(outcome)


@DBOS.workflow()
async def recover_stranded_workflows(scheduled_time: datetime, context: object) -> None:
    """Recover work no live worker will claim, once per tick; the step output records it."""
    await _recovery_sweep_step()


def schedule(settings: Settings) -> ScheduleInput:
    """Return the persisted schedule of the recovery sweep."""
    return {
        "schedule_name": RECOVERY_SCHEDULE_NAME,
        "workflow_fn": recover_stranded_workflows,
        "schedule": settings.recovery_sweep_schedule,
        "context": None,
        "queue_name": QueueName.MAINTENANCE.value,
    }
