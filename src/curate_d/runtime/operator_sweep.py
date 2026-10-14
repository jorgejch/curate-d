"""Operator entry point: run the gated recovery sweep without launching DBOS.

For a scale-down after a hard kill, or whenever the scheduled sweep cannot run: paused,
deleted by a rollback to a build that predates it, or with no I/O worker on the latest
version to tick. It drives DBOS through `DBOSClient` only, so it runs no startup recovery,
starts no queue or scheduler threads and registers no version.

It writes nothing unless `--target-version` is both DBOS's latest version and this build's
own, so run it in a pod of the latest build: a local checkout's version never matches a
deployed one. It holds the target's standalone version lease for the run and stops before
its next write once that lease is found lost. Successor IDs, executor leases and version
locks arbitrate between it, live workers and a scheduled sweep.

    python -m curate_d.runtime.operator_sweep --target-version <version>
"""

import argparse
import asyncio
import dataclasses
import json
import logging
import sys
from collections.abc import Sequence
from typing import Final

from dbos import DBOSClient

from curate_d.config import Settings, get_settings
from curate_d.observability import configure_logging, get_logger
from curate_d.runtime import dbos_app, version_lease
from curate_d.runtime.lease import ExecutorLeaseUnavailableError
from curate_d.runtime.sweep import ClientRecoveryControl, SweepOutcome, run_recovery_sweep

logger = get_logger(__name__)

EXIT_FAILED_ROWS: Final = 1
"""The sweep completed but reported at least one row as failed."""
EXIT_REFUSED: Final = 2
"""Refused before any write: a missing, non-latest, foreign or unregistered target, or a
version lease that could not be taken."""
EXIT_LEASE_LOST: Final = 3
"""The version lease was lost mid-run; nothing was written after the loss was detected."""


class _RefusedError(Exception):
    """The run is refused before any write; the message says why."""


async def _sweep(settings: Settings, client: DBOSClient, target: str) -> int:
    """Check the target, hold its version lease, sweep, and print the outcome as JSON.

    Returns:
        `EXIT_LEASE_LOST` if lease loss was detected, otherwise `EXIT_FAILED_ROWS` if
        any row failed, or 0. Lease loss takes precedence over row failures.

    Raises:
        _RefusedError: The target is not both latest and this build's version, is not
            registered, or its lease could not be acquired. No recovery writes occur.
        Exception: Version reads, monitoring or the sweep failed outside per-row handling;
            these errors propagate rather than becoming an exit status.
    """
    latest = (await client.get_latest_application_version_async())["version_name"]
    own = dbos_app.APPLICATION_VERSION
    if target != latest or target != own:
        raise _RefusedError(
            f"--target-version {target!r} must be both DBOS's latest version ({latest!r}) "
            f"and this build's ({own!r}); run this in a pod of the latest build"
        )
    if target not in await version_lease.registrations(settings):
        raise _RefusedError(
            f"target {target!r} never registered in lease_versions: no worker of it has run "
            f"with the version lease, so it cannot be a recovery target"
        )

    lease = version_lease.standalone_version_lease(settings, target)
    try:
        await asyncio.to_thread(lease.acquire)
    except ExecutorLeaseUnavailableError as error:
        raise _RefusedError(str(error)) from error
    try:
        lease.monitor(lambda: logger.error("operator_sweep.version_lease_lost", version=target))
        outcome = await run_recovery_sweep(
            settings,
            control=ClientRecoveryControl(client, before_write=lease.ensure_held),
            own_version=own,
        )
        lost = not lease.held
    finally:
        await asyncio.to_thread(lease.release)

    _print(outcome)
    if lost:
        logger.error("operator_sweep.stopped_on_lease_loss", version=target)
        return EXIT_LEASE_LOST
    return EXIT_FAILED_ROWS if outcome.failed else 0


def _print(outcome: SweepOutcome) -> None:
    """Print every outcome field as JSON on stdout."""
    sys.stdout.write(json.dumps(dataclasses.asdict(outcome), indent=2, sort_keys=True) + "\n")


def main(argv: Sequence[str] | None = None) -> int:
    """Run one operator sweep and return the process exit status.

    Print the completed sweep's outcome as JSON on stdout. Errors escaping the sweep
    propagate unless they are a target or lease refusal converted to `EXIT_REFUSED`.

    Returns:
        0 when no row failed, `EXIT_FAILED_ROWS` when any did, `EXIT_REFUSED` when the
        target or the lease is refused before anything is written, `EXIT_LEASE_LOST` when
        the version lease was lost mid-run.
    """
    parser = argparse.ArgumentParser(
        prog="python -m curate_d.runtime.operator_sweep",
        description="Recover stranded workflows onto the latest version, without launching DBOS.",
    )
    parser.add_argument(
        "--target-version",
        required=True,
        help="DBOS's latest version, which must also be this build's.",
    )
    arguments = parser.parse_args(argv)
    configure_logging(level=logging.INFO)

    settings = get_settings()
    client = dbos_app.client(settings)
    try:
        return asyncio.run(_sweep(settings, client, arguments.target_version))
    except _RefusedError as refusal:
        logger.error(
            "operator_sweep.refused", target_version=arguments.target_version, reason=str(refusal)
        )
        return EXIT_REFUSED
    finally:
        client.destroy()


if __name__ == "__main__":
    sys.exit(main())
