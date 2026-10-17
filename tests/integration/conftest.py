"""Shared fixtures for tests that need the local stack.

The stack runs the application as well as the database, so these tests are not the only
DBOS executor against that database — see `_isolate_application_version` for why that
matters and what keeps the two apart.
"""

from collections.abc import AsyncIterator, Iterator
from uuid import uuid4

import pytest
from dbos import error as dbos_error
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from curate_d.config import Settings
from curate_d.runtime import dbos_app, promote

# A default run means the local engine CI uses, plus OVH.
_STAGING_BACKENDS = ("rustfs", "ovh")
_DEFAULT_STAGING_BACKENDS = "rustfs,ovh"
_LOCAL_ENGINE_NAMES = {"rustfs": "RustFS"}


def _ranked_versions(settings: Settings) -> list[str] | None:
    """Return this application's versions, latest first, or None if they can't be read.

    An empty list is a registry no version has joined yet. None is an unreachable or
    unmigrated database: a test that needs neither still runs.
    """
    client = dbos_app.client(settings, retry_connection_errors=False)
    try:
        return [info["version_name"] for info in client.list_application_versions()]
    except (dbos_error.DBOSException, SQLAlchemyError):
        return None
    finally:
        client.destroy()


def _restore_latest_version(settings: Settings, previous: str, suite_version: str) -> None:
    """Promote `previous` again if only the suite's version has superseded it.

    DBOS ranks versions by when each was last registered or promoted, so the suite's
    version must rank first and `previous` straight after it. Any other version between
    them was registered or promoted during the run, and is left latest.

    Raises:
        RuntimeError: Reading or promoting the latest version failed; the message names
            the command that restores it by hand.
    """
    client = dbos_app.client(settings, retry_connection_errors=False)
    try:
        ranked = [info["version_name"] for info in client.list_application_versions()]
        if ranked[:2] == [suite_version, previous]:
            promote.promote(client, settings, previous)
    except Exception as exc:
        raise RuntimeError(
            f"could not hand DBOS's latest version back to {previous!r} from the suite's "
            f"{suite_version!r}; run `python -m curate_d.runtime.promote {previous}`"
        ) from exc
    finally:
        client.destroy()


def _check_unread_latest(settings: Settings, suite_version: str) -> None:
    """Fail if the suite's version holds latest on a database whose versions went unread.

    Raises:
        RuntimeError: The versions could not be read before the first test, and the
            suite's version is now latest, so the version to hand back is unknown.
    """
    ranked = _ranked_versions(settings)
    if ranked and ranked[0] == suite_version:
        raise RuntimeError(
            f"the suite's version {suite_version!r} is DBOS's latest, and the versions "
            f"could not be read before the first test; if a stack ran on this database, "
            f"run `python -m curate_d.runtime.promote <its version>`"
        )


@pytest.fixture(scope="package", autouse=True)
def _isolate_application_version(settings: Settings) -> Iterator[None]:
    """Run the suite under an application version no deployed worker serves.

    Package-scoped so the real version is restored when the last integration test
    finishes: session scope leaked the test version into unit tests collected in the
    same run (`pytest tests`), which assert the packaged version.

    The local stack's worker containers are live DBOS executors on this same database,
    under the same application name and — without this — the same version. Anything a
    test puts on a queue is therefore fair game for them, including a workflow resumed
    by `resume_workflow`, which goes back through DBOS's internal queue. A container
    that wins that race cannot run the work, because the test's workflow functions are
    registered in the test process and nowhere else, so the test waits on a result that
    will never arrive.

    Dequeue is filtered by application version, so giving the suite its own version is
    what makes the two structurally unable to touch each other's work. Isolating this
    way rather than by asking developers to stop their stack keeps the suite correct
    regardless of what else happens to be connected.

    The suite's first DBOS launch registers its version, and DBOS makes a newly
    registered version its latest; nothing a single test restores can undo that. Left
    so, the stack's workers stop applying their schedules and every tick is enqueued on a
    version nothing runs once the suite exits. So the latest version found before the
    first test is promoted again after the last, through the operator's own command, if
    only the suite's version has superseded it. A database with no version yet has
    nothing to restore: the stack's first launch on it registers its own version as
    latest. Nor does one whose latest is another run's `+test.` version, which nothing
    serves. One whose versions could not be read fails the run if the suite's version
    ends up latest, since the version to hand back is unknown.
    """
    original = dbos_app.APPLICATION_VERSION
    suite_version = f"{original}+test.{uuid4().hex[:8]}"
    before = _ranked_versions(settings)
    dbos_app.APPLICATION_VERSION = suite_version
    try:
        yield
    finally:
        dbos_app.APPLICATION_VERSION = original
        if before is None:
            _check_unread_latest(settings, suite_version)
        # Another run's suite version, left latest by a concurrent or older suite, has no
        # executor once that run exits: promoting it again would strand the stack too.
        elif before and "+test." not in before[0]:
            _restore_latest_version(settings, before[0], suite_version)


@pytest.fixture(scope="session")
def settings() -> Settings:
    """Resolve application settings from the environment once for the whole test session.

    Returns:
        The process-wide `Settings` instance shared by all tests in the session.
    """
    return Settings()


@pytest.fixture
async def ledger_engine(settings: Settings) -> AsyncIterator[AsyncEngine]:
    """Open an async engine against the configured ledger database.

    Yields:
        The engine, disposed after the test completes.
    """
    engine = create_async_engine(str(settings.ledger_database_url), future=True)
    try:
        yield engine
    finally:
        await engine.dispose()


def pytest_addoption(parser: pytest.Parser) -> None:
    """Select the staging backends without silently skipping unconfigured OVH checks."""
    parser.addoption(
        "--staging-backend",
        action="append",
        choices=_STAGING_BACKENDS,
        help="Run a staging backend; repeat for several (default: rustfs and ovh).",
    )


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    """Run identical staging contract bodies for each explicitly selected backend."""
    if "staging_backend_name" not in metafunc.fixturenames:
        return
    import os

    selected = metafunc.config.getoption("staging_backend")
    if selected is None:
        selected = os.environ.get(
            "CURATE_D_TEST_STAGING_BACKENDS", _DEFAULT_STAGING_BACKENDS
        ).split(",")
    names = ", ".join(_STAGING_BACKENDS)
    if not isinstance(selected, list) or not selected:
        raise pytest.UsageError(f"Select at least one staging backend: {names}")
    for backend in selected:
        if backend not in _STAGING_BACKENDS:
            raise pytest.UsageError(f"Staging backends must be one of: {names}")
    metafunc.parametrize("staging_backend_name", selected)


def pytest_report_header(config: pytest.Config) -> str | None:
    """Keep local-only contract runs visibly distinct from two-backend acceptance."""
    import os

    selected = config.getoption("staging_backend")
    if selected is None:
        selected = os.environ.get(
            "CURATE_D_TEST_STAGING_BACKENDS", _DEFAULT_STAGING_BACKENDS
        ).split(",")
    if selected and "ovh" not in selected:
        engines = " and ".join(_LOCAL_ENGINE_NAMES.get(name, name) for name in selected)
        return (
            f"Staging: {engines} only; OVH verification PENDING (not selected), activation blocked."
        )
    return None
