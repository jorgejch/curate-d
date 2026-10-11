"""Shared fixtures for tests that need the local stack.

The stack runs the application as well as the database, so these tests are not the only
DBOS executor against that database — see `_isolate_application_version` for why that
matters and what keeps the two apart.
"""

from collections.abc import AsyncIterator, Iterator
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from curate_d.config import Settings
from curate_d.runtime import dbos_app

# A default run means the local engine CI uses, plus OVH.
_STAGING_BACKENDS = ("rustfs", "ovh")
_DEFAULT_STAGING_BACKENDS = "rustfs,ovh"
_LOCAL_ENGINE_NAMES = {"rustfs": "RustFS"}


@pytest.fixture(scope="package", autouse=True)
def _isolate_application_version() -> Iterator[None]:
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
    """
    original = dbos_app.APPLICATION_VERSION
    dbos_app.APPLICATION_VERSION = f"{original}+test.{uuid4().hex[:8]}"
    try:
        yield
    finally:
        dbos_app.APPLICATION_VERSION = original


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
