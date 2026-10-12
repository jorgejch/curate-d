"""Operator entry point: make an already-registered version DBOS's latest.

DBOS registers a version again without retiming it, so a build deployed a second time, by
rollback or by rolling forward to it, stays non-latest: schedule ticks and version-less
enqueues then go to a version nothing runs, and the recovery sweep targets it. Run this
before the target build's workers start, so they apply their schedules at startup; run
afterwards, the workers need a restart. A first deploy needs none: its first launch
registers it as latest.

Workers never promote themselves: a worker cannot tell a rollback from newer replicas
crash-looping mid-rollout, and a wrong promotion persists until someone promotes again.

    python -m curate_d.runtime.promote <version>
"""

import argparse
import logging
import sys
import time
from collections.abc import Sequence
from datetime import datetime
from typing import Final

import psycopg
from dbos import DBOSClient
from dbos._error import DBOSException

from curate_d.config import Settings, get_settings
from curate_d.observability import configure_logging, get_logger
from curate_d.runtime import dbos_app
from curate_d.runtime.dbos_app import APPLICATION_NAME
from curate_d.runtime.lease import connect_dedicated, lease_conninfo

logger = get_logger(__name__)

MAX_CLOCK_SKEW_SECONDS: Final = 1.0
"""DBOS stamps a promotion with the caller's clock, so a slow clock may not promote at all."""

EXIT_REFUSED: Final = 1
"""The promotion was refused, failed, or did not read back."""


class PromotionRefusedError(RuntimeError):
    """The promotion was refused before anything was written."""


class PromotionReadBackError(RuntimeError):
    """The promotion was written but the latest version reads back as another."""


def _database_now(settings: Settings) -> datetime:
    """Return the database's clock, read on a short-lived dedicated connection."""
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


def _check_clock(settings: Settings) -> float:
    """Return the skew between this clock and the database's, refusing more than 1 s.

    Raises:
        PromotionRefusedError: The clocks differ by more than `MAX_CLOCK_SKEW_SECONDS`.
    """
    before = time.time()
    database = _database_now(settings).timestamp()
    after = time.time()
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


def promote(client: DBOSClient, settings: Settings, version: str) -> str:
    """Make `version` DBOS's latest and return the version it replaced.

    Raises:
        PromotionRefusedError: `version` is not registered for this application, or the
            clocks disagree; nothing was written.
        DBOSException: DBOS refused the promotion, as for another application's version.
        PromotionReadBackError: The latest version read back is not `version`.
    """
    known = {info["version_name"] for info in client.list_application_versions()}
    if version not in known:
        raise PromotionRefusedError(
            f"version {version!r} is not registered for this application "
            f"({len(known)} versions are); only a build deployed before can be promoted"
        )
    _check_clock(settings)
    previous = _latest(client)
    client.set_latest_application_version(version)
    current = _latest(client)
    if current != version:
        raise PromotionReadBackError(
            f"promoted {version!r} but the latest version reads back as {current!r}"
        )
    return previous


def main(argv: Sequence[str] | None = None) -> int:
    """Promote one version and return the process exit status.

    Returns:
        0 once the version reads back as latest; `EXIT_REFUSED` on any refusal or
        mismatch, having written nothing on a refusal.
    """
    parser = argparse.ArgumentParser(
        prog="python -m curate_d.runtime.promote",
        description="Make an already-registered application version DBOS's latest.",
    )
    parser.add_argument("version", help="The version to promote, as DBOS lists it.")
    arguments = parser.parse_args(argv)
    configure_logging(level=logging.INFO)

    settings = get_settings()
    client = dbos_app.client(settings)
    try:
        previous = promote(client, settings, arguments.version)
    except PromotionRefusedError as error:
        logger.error("promote.refused", version=arguments.version, reason=str(error))
        return EXIT_REFUSED
    except (DBOSException, psycopg.Error, PromotionReadBackError) as error:
        logger.error("promote.failed", version=arguments.version, error=str(error))
        return EXIT_REFUSED
    finally:
        client.destroy()
    logger.info("promote.promoted", version=arguments.version, previous_latest=previous)
    return 0


if __name__ == "__main__":
    sys.exit(main())
