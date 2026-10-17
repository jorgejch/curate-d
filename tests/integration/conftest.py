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


def _latest_version(settings: Settings) -> str | None:
    """Return DBOS's latest version for this application, or None if there is none to read.

    None too when the database is unreachable or not migrated: a test that needs neither
    still runs, and no test can then have moved the latest version.
    """
    client = dbos_app.client(settings, retry_connection_errors=False)
    try:
        return client.get_latest_application_version()["version_name"]
    except (dbos_error.DBOSException, SQLAlchemyError):
        return None
    finally:
        client.destroy()


def _restore_latest_version(settings: Settings, previous: str, suite_version: str) -> None:
    """Promote `previous` again if the suite's version is still DBOS's latest.

    Raises:
        RuntimeError: Reading or promoting the latest version failed; the message names
            the command that restores it by hand.
    """
    client = dbos_app.client(settings, retry_connection_errors=False)
    try:
        if client.get_latest_application_version()["version_name"] == suite_version:
            promote.promote(client, settings, previous)
    except Exception as exc:
        raise RuntimeError(
            f"could not hand DBOS's latest version back to {previous!r} from the suite's "
            f"{suite_version!r}; run `python -m curate_d.runtime.promote {previous}`"
        ) from exc
    finally:
        client.destroy()


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
    the suite's version still holds it. A database with no version yet has nothing to
    restore: the stack's first launch on it registers its own version as latest. Nor
    does one whose latest is another run's `+test.` version, which nothing serves.
    """
    original = dbos_app.APPLICATION_VERSION
    suite_version = f"{original}+test.{uuid4().hex[:8]}"
    previous = _latest_version(settings)
    dbos_app.APPLICATION_VERSION = suite_version
    try:
        yield
    finally:
        dbos_app.APPLICATION_VERSION = original
        # Another run's suite version, left latest by a concurrent or older suite, has no
        # executor once that run exits: promoting it again would strand the stack too.
        if previous is not None and "+test." not in previous:
            _restore_latest_version(settings, previous, suite_version)


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
