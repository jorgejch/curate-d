"""CI gate: fail unless the running workers applied this build's schedules.

`worker.ready` follows a schedule apply that may have written nothing: a worker applies
only when its version is DBOS's latest. The integration workflow runs this after the
workers log ready, and it fails the job unless the latest version is this build's, the
stored schedules are exactly the declared ones, and each stored cron matches its
declaration.

    python -m curate_d.worker.schedule_check
"""

import sys
from collections.abc import Mapping

from dbos import DBOSClient

from curate_d.config import Settings, get_settings
from curate_d.runtime import dbos_app
from curate_d.runtime.profiles import WorkerProfile
from curate_d.workflows.registry import schedules_for


def declared_crons(settings: Settings) -> dict[str, str]:
    """Return every schedule any profile declares, by name, with its cron."""
    return {
        entry["schedule_name"]: entry["schedule"]
        for profile in WorkerProfile
        for entry in schedules_for(profile, settings)
    }


def schedule_failures(
    *, latest: str, version: str, stored: Mapping[str, str], declared: Mapping[str, str]
) -> list[str]:
    """Return why the stored schedules are not this build's, or nothing when they are.

    Args:
        latest: DBOS's latest application version.
        version: This build's application version.
        stored: The stored schedules' crons, by schedule name.
        declared: The declared schedules' crons, by schedule name.
    """
    failures = []
    if latest != version:
        failures.append(f"latest version is {latest!r}, not the image's {version!r}")
    if stored.keys() != declared.keys():
        missing = sorted(declared.keys() - stored.keys())
        undeclared = sorted(stored.keys() - declared.keys())
        failures.append(f"missing {missing}, undeclared {undeclared}")
    stale = sorted(
        name for name in declared.keys() & stored.keys() if stored[name] != declared[name]
    )
    if stale:
        failures.append(f"stored cron differs from the declared one for {stale}")
    return failures


def check(client: DBOSClient, settings: Settings) -> list[str]:
    """Read the latest version and the stored schedules, and return the gate's failures."""
    latest = client.get_latest_application_version()["version_name"]
    stored = {entry["schedule_name"]: entry["schedule"] for entry in client.list_schedules()}
    return schedule_failures(
        latest=latest,
        version=dbos_app.APPLICATION_VERSION,
        stored=stored,
        declared=declared_crons(settings),
    )


def main() -> int:
    """Run the gate, print each failure as a GitHub error annotation, and return the exit status."""
    settings = get_settings()
    client = dbos_app.client(settings, retry_connection_errors=False)
    try:
        failures = check(client, settings)
    finally:
        client.destroy()
    for failure in failures:
        sys.stdout.write(f"::error::schedules not applied: {failure}\n")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
