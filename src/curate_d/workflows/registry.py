"""The persisted schedules the deployment declares, and every workflow module behind them.

Importing this module imports every workflow module, which is what registers their
functions with DBOS before launch; a workflow DBOS has not seen cannot be recovered.

The declarations are written by the deployment command (`python -m curate_d.schedules`),
never by a worker: see `curate_d.schedules` for why.
"""

from dbos import ClientScheduleInput

from curate_d.config import Settings
from curate_d.workflows import (
    classify,
    connections,
    dry_run,
    grouping,
    ingest,
    placement,
    reconcile,
    rescan,
    retention,
    scan,
)

# Imported for their DBOS registrations; nothing here calls them directly.
_REGISTERED = (dry_run, ingest, placement)


def declared_schedules(settings: Settings) -> list[ClientScheduleInput]:
    """Return every schedule this build of the application declares.

    Each names its workflow by the stable name the workflow registers under, and routes
    it to the maintenance queue, which only the I/O profile listens to: every scheduled
    pass here either calls a provider or enqueues work for the profile that does. The
    history prune needs only the database and runs there with the rest. None names an
    owner: the deployment command's client owns them all.
    """
    return [
        connections.schedule(),
        scan.schedule(settings),
        rescan.schedule(settings),
        classify.schedule(),
        grouping.schedule(),
        reconcile.schedule(settings),
        reconcile.check_schedule(settings),
        retention.schedule(settings),
    ]


def declared_schedule_names(settings: Settings) -> frozenset[str]:
    """Return the name of every declared schedule: the set reconciliation keeps."""
    return frozenset(entry["schedule_name"] for entry in declared_schedules(settings))
