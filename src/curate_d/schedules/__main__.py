"""`python -m curate_d.schedules`: the deployment command that owns persisted schedules.

Subcommands:

- `reconcile`: wait for this build's version to be the latest and its queues to exist,
  apply its schedules and delete this application's undeclared ones. The deploy job runs
  it after the workers roll out.
- `promote`: make this build's version the latest, then reconcile. For a rollback, after
  the workers are rolled back.
- `show`: print what `reconcile` would change, writing nothing.

Exit status: 0 done; 1 unexpected failure; 2 usage error (argparse's); 3 the version was
not registered in time; 4 another version is the latest (0 with `--allow-superseded`); 5 a
queue was not registered in time; 6 the schedule authority stayed held. Every status
leaves the schedules either as they were or partly reconciled in a state a re-run
converges from.
"""

import argparse
import logging
import sys
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from typing import Final, TextIO

from curate_d.config import Settings, get_settings
from curate_d.observability import configure_logging, get_logger
from curate_d.runtime import dbos_app
from curate_d.runtime.dbos_app import application_name
from curate_d.runtime.schedule_fence import (
    SCHEDULE_AUTHORITY_TIMEOUT_SECONDS,
    ScheduleAuthorityUnavailableError,
    ScheduleFence,
    StaleScheduleDeploymentError,
    fenced_client,
)
from curate_d.schedules.deployment import (
    DEFAULT_WAIT_SECONDS,
    Patience,
    ScheduleClient,
    ScheduleDeploymentError,
    SchedulePlan,
    VersionSupersededError,
    plan,
    promote,
    reconcile,
)
from curate_d.workflows.registry import declared_schedules

logger = get_logger(__name__)

# Not 2: argparse exits 2 on a usage error, which a retry does not fix.
EXIT_AUTHORITY_UNAVAILABLE: Final = 6
EXIT_SUPERSEDED: Final = VersionSupersededError.exit_code

ClientOpener = Callable[[Settings, ScheduleFence], AbstractContextManager[ScheduleClient]]


def build_parser() -> argparse.ArgumentParser:
    """Return the command's argument parser."""
    parser = argparse.ArgumentParser(
        prog="python -m curate_d.schedules",
        description="Reconcile the application's persisted DBOS schedules at deployment.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name, summary in (
        ("reconcile", "apply this build's schedules and retire undeclared ones"),
        ("promote", "make this build's version the latest, then reconcile (rollback)"),
    ):
        command = commands.add_parser(name, help=summary)
        command.add_argument(
            "--wait-seconds",
            type=float,
            default=DEFAULT_WAIT_SECONDS,
            help="how long to wait for the version and its queues to register "
            "(default: %(default)s)",
        )
        command.add_argument(
            "--lock-timeout-seconds",
            type=float,
            default=SCHEDULE_AUTHORITY_TIMEOUT_SECONDS,
            help="how long one write waits for another writer (default: %(default)s)",
        )
        command.add_argument(
            "--allow-superseded",
            action="store_true",
            help="exit 0 instead of 4 when another version is the latest: for a deploy job "
            "that a newer rollout may overtake",
        )
    commands.add_parser("show", help="print what reconcile would change, writing nothing")
    return parser


def print_plan(outcome: SchedulePlan, *, version: str, out: TextIO) -> None:
    """Write `outcome` as one line per schedule, prefixed by the change it would get."""
    out.write(f"version {version}; latest {outcome.latest_version}\n")
    for prefix, names in (
        ("+", outcome.created),
        ("~", outcome.updated),
        ("=", outcome.unchanged),
        ("-", tuple(stored.name for stored in outcome.retired)),
        ("?", tuple(stored.name for stored in outcome.unclaimed)),
    ):
        for name in names:
            out.write(f"{prefix} {name}\n")


def run(
    argv: Sequence[str],
    *,
    settings: Settings | None = None,
    open_client: ClientOpener = fenced_client,
    out: TextIO = sys.stdout,
) -> int:
    """Run one subcommand and return the exit status.

    Args:
        argv: The arguments after the program name.
        settings: Resolved settings; read from the environment when omitted.
        open_client: Opens the client the command uses, behind the fence it is given.
        out: Where `show` writes.
    """
    arguments = build_parser().parse_args(argv)
    settings = settings or get_settings()
    application = application_name(settings.environment)
    # Read at call time: tests substitute the version.
    version = dbos_app.APPLICATION_VERSION
    declared = declared_schedules(settings)
    fence = ScheduleFence(
        application=application,
        version=version,
        timeout_seconds=getattr(
            arguments, "lock_timeout_seconds", SCHEDULE_AUTHORITY_TIMEOUT_SECONDS
        ),
    )
    try:
        with open_client(settings, fence) as client:
            if arguments.command == "show":
                print_plan(
                    plan(client, application=application, declared=declared),
                    version=version,
                    out=out,
                )
                return 0
            patience = Patience(seconds=arguments.wait_seconds)
            action = promote if arguments.command == "promote" else reconcile
            outcome = action(
                client,
                fence,
                application=application,
                version=version,
                declared=declared,
                patience=patience,
            )
    except ScheduleAuthorityUnavailableError as error:
        logger.error("schedules.authority_unavailable", error=str(error))
        return EXIT_AUTHORITY_UNAVAILABLE
    except (StaleScheduleDeploymentError, VersionSupersededError) as error:
        if getattr(arguments, "allow_superseded", False):
            logger.warning("schedules.superseded", version=version, error=str(error))
            return 0
        logger.error("schedules.superseded", version=version, error=str(error))
        return EXIT_SUPERSEDED
    except ScheduleDeploymentError as error:
        logger.error("schedules.not_ready", version=version, error=str(error))
        return error.exit_code
    logger.info(
        "schedules.reconciled",
        version=version,
        applied=len(outcome.applied),
        deleted=[stored.name for stored in outcome.deleted],
        unclaimed_left=[stored.name for stored in outcome.unclaimed_left],
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Configure logging and run the subcommand named on the command line."""
    configure_logging(level=logging.INFO)
    return run(sys.argv[1:] if argv is None else argv)


if __name__ == "__main__":
    sys.exit(main())
