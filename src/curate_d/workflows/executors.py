"""The operator's recovery of a vanished executor, run as a workflow.

The API enqueues it on the I/O workers' `maintenance` queue under an identity derived
from the operator's request ID, so a repeated request returns the first one's report, and
a recovery the API stopped waiting for still finishes. It runs on a worker because the
sweep's re-enqueue needs a launched DBOS (sweep.py).

The sweep is one step: re-executed after a crash, it finds nothing left to re-enqueue
(DBOS re-enqueues only rows the dead executor still owns) and recognises the forks it
already made by their deterministic successor IDs. It writes nothing to the ledger, and no
audit entry: recovery acts on DBOS workflow rows, and its record is the returned report,
the `recovery_sweep.*` logs and DBOS fork lineage.
"""

import dataclasses
from typing import Final

from dbos import DBOS

from curate_d.observability.logging import get_logger
from curate_d.runtime.services import current
from curate_d.runtime.sweep import run_recovery_sweep

logger = get_logger(__name__)

# Registered under this name, so the API, which does not run workflows, can enqueue it.
WORKFLOW_NAME: Final = "recover_executor"


@DBOS.step()
async def _recover_step(executor_id: str | None) -> dict[str, object]:
    """Run the recovery sweep for one executor, or every one, and return its outcome."""
    outcome = await run_recovery_sweep(current().settings, executor_id=executor_id)
    return dataclasses.asdict(outcome)


@DBOS.workflow(name=WORKFLOW_NAME)
async def recover_executor(
    executor_id: str | None, operator: str, request_id: str
) -> dict[str, object]:
    """Recover what `executor_id` left behind, or every executor's when None.

    Returns:
        The sweep's outcome, as `SweepOutcome`'s fields.
    """
    logger.info(
        "recovery_sweep.started", executor=executor_id, operator=operator, request_id=request_id
    )
    report = await _recover_step(executor_id)
    logger.info(
        "recovery_sweep.finished",
        executor=executor_id,
        operator=operator,
        request_id=request_id,
        refused=report.get("refused"),
        reenqueued=len(_items(report, "reenqueued")),
        forked=len(_items(report, "forked")),
        skipped=len(_items(report, "skipped")),
        failed=len(_items(report, "failed")),
    )
    return report


def _items(report: dict[str, object], name: str) -> tuple[object, ...]:
    """Return the sequence `report` holds under `name`, empty if it holds none."""
    value = report.get(name)
    return tuple(value) if isinstance(value, list | tuple) else ()
