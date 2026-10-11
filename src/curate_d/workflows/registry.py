"""Which persisted schedules each worker profile applies.

Importing this module imports every workflow module, which is what registers their
functions with DBOS before launch; a workflow DBOS has not seen cannot be recovered.
"""

from dbos import ScheduleInput

from curate_d.config import Settings
from curate_d.runtime.profiles import WorkerProfile
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


def schedules_for(profile: WorkerProfile, settings: Settings) -> list[ScheduleInput]:
    """Return the schedules `profile` applies after launch.

    Only the I/O profile applies any: every scheduled pass here either calls a provider
    or enqueues work for the profile that does, and applying the same definitions from
    two profiles would only race to write identical rows. The history prune needs only
    the database and runs there with the rest.
    """
    if profile is WorkerProfile.IO:
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
    return []


def declared_schedule_names(settings: Settings) -> frozenset[str]:
    """Return the name of every schedule any profile declares.

    The set a worker keeps when it deletes undeclared schedules: one profile's own list
    would delete another profile's.
    """
    return frozenset(
        entry["schedule_name"]
        for profile in WorkerProfile
        for entry in schedules_for(profile, settings)
    )
