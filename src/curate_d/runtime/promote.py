"""Operator entry point: make an already-registered version DBOS's latest.

DBOS registers a version again without retiming it, so a build deployed a second time, by
rollback or by rolling forward to it, stays non-latest: schedule ticks and version-less
enqueues then go to a version nothing runs, and the recovery sweep targets it. Run this
before the target build's workers start, so they apply their schedules at startup; run
afterwards, the workers need a restart. A first deploy needs none: its first launch
registers it as latest.

Workers never promote themselves: a worker cannot tell a rollback from newer replicas
crash-looping mid-rollout, and a wrong promotion persists until someone promotes again.

The change runs under the promotion fence, held exclusively (version_lease.py): it waits
for a recovery sweep's forks to finish, so no sweep keeps forking onto the version it
replaced, and gives up after `--fence-timeout` seconds without writing.

    python -m curate_d.runtime.promote [--fence-timeout <seconds>] <version>
"""

import argparse
import logging
import math
import sys
import time
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from datetime import datetime
from typing import Final

import psycopg
from dbos import DBOSClient, error
from sqlalchemy.exc import SQLAlchemyError

from curate_d.config import Settings, get_settings
from curate_d.observability import configure_logging, get_logger
from curate_d.runtime import dbos_app
from curate_d.runtime.dbos_app import APPLICATION_NAME
from curate_d.runtime.lease import connect_dedicated, lease_conninfo
from curate_d.runtime.version_lease import PromotionFenceTimeoutError, hold_fence_exclusive

logger = get_logger(__name__)

MAX_CLOCK_SKEW_SECONDS: Final = 1.0
"""DBOS stamps a promotion with the caller's clock, so a slow clock may not promote at all."""

FENCE_TIMEOUT_SECONDS: Final = 120.0
"""How long to wait for a recovery sweep holding the promotion fence, by default."""

EXIT_REFUSED: Final = 1
"""The promotion was refused, failed, or did not read back."""

EXIT_FENCED: Final = 3
"""A recovery sweep held the promotion fence past the timeout; nothing was written."""


class PromotionRefusedError(RuntimeError):
    """The promotion was refused before anything was written."""


class PromotionReadBackError(RuntimeError):
    """The promotion was written but the latest version reads back as another."""


def _database_now(settings: Settings) -> datetime:
    """Return the database's clock, read on a short-lived dedicated connection.

    Raises:
        PromotionRefusedError: The query returned no row or a non-datetime value.
        psycopg.Error: Connecting to or querying the database failed.
    """
    connection = connect_dedicated(
        lease_conninfo(settings), application_name=f"{APPLICATION_NAME} promote"
    )
    try:
        row = connection.execute("SELECT now()").fetchone()
    finally:
        connection.close()
    if row is None or not isinstance(row[0], datetime):
        raise PromotionRefusedError(f"the database returned no time: {row!r}")
    return row[0]


def _check_clock(settings: Settings, *, clock: Callable[[], float] = time.time) -> float:
    """Return the skew between this clock and the database's, refusing more than 1 s.

    The skew is absolute, in seconds, measured against the midpoint of the database
    round trip. Exactly `MAX_CLOCK_SKEW_SECONDS` is accepted.

    Args:
        settings: Resolved configuration naming the database.
        clock: Reads this machine's clock in epoch seconds; substituted by tests.

    Raises:
        PromotionRefusedError: The database returned no valid time, or the clocks differ
            by more than `MAX_CLOCK_SKEW_SECONDS`.
        psycopg.Error: Reading the database clock failed.
    """
    before = clock()
    database = _database_now(settings).timestamp()
    after = clock()
    skew = abs(database - (before + after) / 2)
    if skew > MAX_CLOCK_SKEW_SECONDS:
        raise PromotionRefusedError(
            f"this clock differs from the database's by {skew:.3f}s, more than "
            f"{MAX_CLOCK_SKEW_SECONDS:.0f}s; DBOS stamps the promotion with this clock, so it "
            f"could rank the version below the current latest. Fix the clock and retry"
        )
    return skew


def _latest(client: DBOSClient) -> str:
    """Return the name of DBOS's latest version for this application."""
    return client.get_latest_application_version()["version_name"]


def promote(
    client: DBOSClient,
    settings: Settings,
    version: str,
    *,
    fence: Callable[[], AbstractContextManager[None]] | None = None,
) -> str:
    """Make `version` DBOS's latest and return the version it replaced.

    Args:
        client: The DBOS client to read and set the latest version through.
        settings: Resolved configuration naming the system database.
        version: The registered version to promote.
        fence: Holds the promotion fence exclusively across the change. Defaults to
            `hold_fence_exclusive` with `FENCE_TIMEOUT_SECONDS`.

    Raises:
        PromotionRefusedError: `version` is not registered for this application, or the
            database clock is invalid or differs by more than 1 s; nothing was written.
        PromotionFenceTimeoutError: A recovery sweep held the promotion fence past the
            timeout; nothing was written.
        error.DBOSException: DBOS refused the promotion, as for another application's version.
        SQLAlchemyError: The DBOS client failed to read or set the latest version.
        psycopg.Error: Reading the database clock or taking the fence failed before
            promotion.
        PromotionReadBackError: The latest version read back is not `version`; the
            promotion was already written and is not rolled back.
    """
    known = {info["version_name"] for info in client.list_application_versions()}
    if version not in known:
        raise PromotionRefusedError(
            f"version {version!r} is not registered for this application "
            f"({len(known)} versions are); only a build deployed before can be promoted"
        )
    _check_clock(settings)
    hold = fence or (lambda: hold_fence_exclusive(settings, timeout=FENCE_TIMEOUT_SECONDS))
    with hold():
        previous = _latest(client)
        client.set_latest_application_version(version)
        current = _latest(client)
    if current != version:
        raise PromotionReadBackError(
            f"promoted {version!r} but the latest version reads back as {current!r}"
        )
    return previous


def _positive_seconds(value: str) -> float:
    """Parse a positive number of seconds for argparse.

    Raises:
        argparse.ArgumentTypeError: `value` is not a positive number.
    """
    try:
        seconds = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a number of seconds") from None
    if not (math.isfinite(seconds) and seconds > 0):
        raise argparse.ArgumentTypeError(f"{value!r} is not a positive number of seconds")
    return seconds


def main(argv: Sequence[str] | None = None) -> int:
    """Promote one version and return the process exit status.

    Returns:
        0 once the version reads back as latest; `EXIT_REFUSED` for a pre-write refusal,
        a DBOS, SQLAlchemy or psycopg error during promotion, or a read-back mismatch. A
        mismatch occurs after the promotion was written. `EXIT_FENCED` when a recovery
        sweep held the promotion fence past `--fence-timeout`; nothing was written.
    """
    parser = argparse.ArgumentParser(
        prog="python -m curate_d.runtime.promote",
        description="Make an already-registered application version DBOS's latest.",
    )
    parser.add_argument("version", help="The version to promote, as DBOS lists it.")
    parser.add_argument(
        "--fence-timeout",
        type=_positive_seconds,
        default=FENCE_TIMEOUT_SECONDS,
        metavar="SECONDS",
        help=(
            "How long to wait for a running recovery sweep to finish its forks "
            f"(default {FENCE_TIMEOUT_SECONDS:g})."
        ),
    )
    arguments = parser.parse_args(argv)
    configure_logging(level=logging.INFO)

    settings = get_settings()
    client = dbos_app.client(settings)
    try:
        previous = promote(
            client,
            settings,
            arguments.version,
            fence=lambda: hold_fence_exclusive(settings, timeout=arguments.fence_timeout),
        )
    except PromotionFenceTimeoutError as failure:
        logger.error("promote.fenced", version=arguments.version, reason=str(failure))
        return EXIT_FENCED
    except PromotionRefusedError as failure:
        logger.error("promote.refused", version=arguments.version, reason=str(failure))
        return EXIT_REFUSED
    except (error.DBOSException, SQLAlchemyError, psycopg.Error, PromotionReadBackError) as failure:
        logger.error("promote.failed", version=arguments.version, error=str(failure))
        return EXIT_REFUSED
    finally:
        client.destroy()
    logger.info("promote.promoted", version=arguments.version, previous_latest=previous)
    return 0


if __name__ == "__main__":
    sys.exit(main())
