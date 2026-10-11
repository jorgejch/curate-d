"""Control plane application.

The API performs no authentication of its own; deploy it only behind an authenticating
reverse proxy or on a private network.

Liveness and readiness are separated on purpose. Liveness answers "is this process
wedged", and must not depend on the database: a database outage that fails liveness gets
every API pod restarted, which is the opposite of helpful. Readiness answers "should
traffic come here", and does depend on the database, because an API that cannot read the
ledger has nothing useful to say.

Every other area — connections, configuration, observability, review queue, audit, dry
runs, throughput — is a router over a backend from `backends.py`, and every backend error
is answered through `errors.py`.
"""

from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from curate_d.api import (
    audit,
    backends,
    configuration,
    connections,
    dry_runs,
    errors,
    jobs,
    review,
    throughput,
)
from curate_d.api.backends import BackendsFactory, ledger_backends
from curate_d.config import Settings, get_settings
from curate_d.observability import get_logger, trace_requests
from curate_d.runtime.dbos_app import APPLICATION_VERSION

logger = get_logger(__name__)


class Liveness(BaseModel):
    """Process-level health. Deliberately says nothing about dependencies."""

    status: str
    version: str


class Readiness(BaseModel):
    """Whether this instance should receive traffic."""

    ready: bool
    ledger_reachable: bool
    detail: str | None = None


# Lets tests substitute a fake/in-memory engine for `create_app` without touching `app.state`.
type EngineFactory = Callable[[Settings], AsyncEngine]


def _default_engine(settings: Settings) -> AsyncEngine:
    """Create the async SQLAlchemy engine for the ledger database."""
    return create_async_engine(str(settings.ledger_database_url), future=True)


def create_app(
    settings: Settings | None = None,
    *,
    engine_factory: EngineFactory = _default_engine,
    backends_factory: BackendsFactory = ledger_backends,
) -> FastAPI:
    """Build the application.

    `engine_factory` and `backends_factory` are injectable so the endpoints can be
    exercised without a database. Reaching into `app.state` from a test instead would
    break the moment the lifespan runs twice, which is exactly what a test client context
    manager does.
    """
    resolved = settings if settings is not None else get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        """Create the ledger engine for the app's lifetime and dispose it on shutdown."""
        engine = engine_factory(resolved)
        app.state.ledger_engine = engine
        try:
            async with backends_factory(resolved, engine) as wired:
                app.state.backends = wired
                yield
        finally:
            await engine.dispose()

    app = FastAPI(
        title="curate.d control plane",
        version=APPLICATION_VERSION,
        lifespan=lifespan,
    )
    errors.install(app)
    app.include_router(connections.router(backends.connections))
    app.include_router(configuration.router(backends.configuration))
    app.include_router(jobs.router(backends.jobs))
    app.include_router(review.router(backends.review))
    app.include_router(audit.router(backends.audit))
    app.include_router(dry_runs.router(backends.dry_runs))
    app.include_router(throughput.router(backends.throughput))

    @app.get("/health/live", response_model=Liveness)
    async def live() -> Liveness:
        """Report process-level liveness without touching any dependency."""
        return Liveness(status="ok", version=APPLICATION_VERSION)

    @app.get("/health/ready", response_model=Readiness)
    async def ready(response: Response) -> Readiness:
        """Report whether the ledger is reachable, setting 503 on failure."""
        engine: AsyncEngine = app.state.ledger_engine
        try:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        except Exception as error:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            # The message is redacted downstream, but the DSN can carry a password, so
            # only the exception type crosses the boundary.
            return Readiness(
                ready=False,
                ledger_reachable=False,
                detail=f"ledger unreachable: {type(error).__name__}",
            )
        return Readiness(ready=True, ledger_reachable=True, detail=None)

    return app


def create_traced_app() -> FastAPI:
    """Build the application with a span per request; the uvicorn factory.

    Kept apart from `create_app` so tests build the app without touching the process's
    tracer provider.
    """
    app = create_app()
    trace_requests(app)
    return app
