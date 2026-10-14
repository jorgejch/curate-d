"""DBOS configuration.

DBOS is used as a library, not a service: its system tables live in the ledger database,
in their own schema (`SYSTEM_SCHEMA`). That is a requirement, not a default. One dump of
the database then holds the whole state: ledger rows, audit trail, checkpoints and queues
together. It also keeps a transactional enqueue possible (`DBOSClient.enqueue_in_transaction`
needs the caller's connection to reach the system tables): nothing uses one today, and the
executor management layer (#5) may. There is no setting for a separate system database.

Exactly-once ledger writes are a different mechanism: the datasource's tracking table
commits in the same transaction as the ledger row (see datasource.py).

Configuration is built here and constructed by an entry point, never at import time: a
module-level DBOS() would connect during test collection.
"""

import os
import re
from importlib.metadata import version
from typing import Final

from dbos import DBOS, DBOSClient, DBOSConfig

from curate_d.config import DeploymentEnvironment, Settings
from curate_d.runtime.identity import resolve_executor_id

APPLICATION_NAME = "curate-d"
"""The base name: DBOS's per-environment name extends it, and advisory-lock keys use it bare."""


def application_name(environment: DeploymentEnvironment | None) -> str:
    """Return the name DBOS runs under in an environment, `curate-d-local` when unset.

    DBOS owns every workflow, queue, schedule and version under this name. Queue and
    schedule names are unique across applications, so environments sharing a system
    database would collide under one name; hence one name per environment, and a
    deployment that forgets its environment fails at startup instead of idling as another
    application.
    """
    return f"{APPLICATION_NAME}-{(environment or DeploymentEnvironment.LOCAL).value}"


# Set explicitly rather than left to DBOS's source hash, so that a version bump is a
# deployment decision and shows up in workflow records as one.
#
# Read from package metadata rather than written here, because DBOS pins in-flight
# workflows to this value: if it disagreed with the released version, a v0.2.0 image
# would still enqueue and recover work as 0.1.0, and nothing would report the mismatch.
# One source of truth, and the release pipeline checks the tag against it.
#
# A dev image adds its build to that version (see `application_version`), so every dev
# build is a distinct version: DBOS's latest version then names the newest build, which
# is what schedule ownership relies on. Release images carry no build revision.
BUILD_REVISION_ENV: Final = "CURATE_D_BUILD_REVISION"
_DEV_BUILD_REVISION: Final = re.compile(r"dev-(?P<stamp>\d{14})-(?P<sha>[0-9a-f]{7,40})")


def application_version(package_version: str, build_revision: str | None) -> str:
    """Return the DBOS application version for a build of `package_version`.

    Args:
        package_version: The packaged version, as the release pipeline checks it.
        build_revision: The image's dev tag, `dev-<UTC timestamp>-<sha>`, baked in by CI
            for dev builds; None or empty for a release image.

    Returns:
        `package_version` for a release image, else `<package>+dev.<timestamp>.<sha>`: a
        local version label, so the release it leads to still reads first.

    Raises:
        ValueError: `build_revision` is set but is not a dev tag.
    """
    if not build_revision:
        return package_version
    match = _DEV_BUILD_REVISION.fullmatch(build_revision)
    if match is None:
        raise ValueError(
            f"{BUILD_REVISION_ENV}={build_revision!r} is not a dev image tag "
            "(dev-<14-digit UTC timestamp>-<hex sha>)"
        )
    return f"{package_version}+dev.{match['stamp']}.{match['sha']}"


APPLICATION_VERSION = application_version(version("curate-d"), os.environ.get(BUILD_REVISION_ENV))

# DBOS keeps its own tables in a dedicated schema, which is what keeps `\dt` in the
# ledger database readable and makes the two sets of tables easy to tell apart in a dump.
SYSTEM_SCHEMA = "dbos"


def system_database_url(settings: Settings) -> str:
    """Return the URL of DBOS's system tables: the ledger database's (see module docstring)."""
    return str(settings.ledger_database_url)


def build_dbos_config(settings: Settings, *, executor_id: str | None = None) -> DBOSConfig:
    """Return the configuration a worker launches DBOS with.

    Args:
        settings: Application settings.
        executor_id: Recovery identity; resolved from the environment when omitted.
    """
    return DBOSConfig(
        name=application_name(settings.environment),
        application_version=APPLICATION_VERSION,
        system_database_url=system_database_url(settings),
        dbos_system_schema=SYSTEM_SCHEMA,
        # A recovery identity, not a concurrency one: per-worker queue limits are counted
        # in each process's memory. A shared identity lets one pod's restart recover
        # workflows a live peer is running — see identity.py.
        executor_id=executor_id or resolve_executor_id(),
    )


def configure(settings: Settings, *, executor_id: str | None = None) -> DBOS:
    """Construct the DBOS singleton.

    Call from an entry point, never at import time — see the module docstring.
    """
    return DBOS(config=build_dbos_config(settings, executor_id=executor_id))


def client(settings: Settings, *, retry_connection_errors: bool = True) -> DBOSClient:
    """Build a client of the workflow system database, for processes that do not run workflows.

    The control plane enqueues work, reads workflow outcomes and adjusts queue limits
    through it; it never launches DBOS, so it neither recovers nor executes anything.
    Lazy, so building one needs no database: a control plane starting during a database
    outage stays live, and readiness reports the outage.

    Args:
        settings: Resolved configuration.
        retry_connection_errors: False raises a lost connection instead of blocking until
            it recovers, for a caller with a deadline such as a worker's shutdown.
    """
    return DBOSClient(
        system_database_url=system_database_url(settings),
        dbos_system_schema=SYSTEM_SCHEMA,
        application_name=application_name(settings.environment),
        lazy=True,
        retry_connection_errors=retry_connection_errors,
    )
