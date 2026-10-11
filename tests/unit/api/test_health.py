"""Health endpoints.

Isolated: the engine is substituted at the boundary, because what is under test is the
liveness/readiness split, not PostgreSQL.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine

from curate_d.api.app import create_app
from curate_d.api.backends import Backends
from curate_d.config import Settings
from tests.unit.api.fakes import backends


class _StubConnection:
    """Stands in for an AsyncConnection, succeeding or failing on execute."""

    def __init__(self, failure: Exception | None) -> None:
        """Store the exception `execute` should raise, or None to succeed."""
        self._failure = failure

    async def execute(self, _statement: object) -> None:
        """Raise the configured failure, if any; otherwise do nothing."""
        if self._failure is not None:
            raise self._failure


class _StubEngine:
    """Stands in for an AsyncEngine, succeeding or failing on connect."""

    def __init__(self, failure: Exception | None = None) -> None:
        """Store the exception `connect` should raise, or None to succeed."""
        self._failure = failure

    @asynccontextmanager
    async def connect(self) -> AsyncGenerator[_StubConnection, None]:
        """Raise the configured failure, if any; otherwise yield a working stub connection."""
        if self._failure is not None:
            raise self._failure
        yield _StubConnection(None)

    async def dispose(self) -> None:
        """No-op, matching AsyncEngine.dispose's signature."""
        return None


def _client(engine: _StubEngine) -> TestClient:
    """Build a TestClient for the app wired to a stub engine instead of a real database."""

    @asynccontextmanager
    async def fake_backends(
        _settings: Settings, _engine: AsyncEngine
    ) -> AsyncGenerator[Backends, None]:
        """Fakes: these tests are about health, not the other areas."""
        yield backends()

    app = create_app(
        Settings(),
        engine_factory=lambda _settings: engine,  # type: ignore[arg-type,return-value]
        backends_factory=fake_backends,
    )
    return TestClient(app)


def test_liveness_does_not_depend_on_the_database() -> None:
    """A database outage that fails liveness restarts every API pod, which helps nobody."""
    with _client(_StubEngine(failure=OSError("connection refused"))) as client:
        response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_readiness_is_true_when_the_ledger_answers() -> None:
    """Readiness reports ready when the stub connection succeeds."""
    with _client(_StubEngine()) as client:
        response = client.get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {"ready": True, "ledger_reachable": True, "detail": None}


def test_readiness_fails_when_the_ledger_is_unreachable() -> None:
    """Readiness returns 503 and ready=False when the ledger connection fails."""
    with _client(_StubEngine(failure=OSError("connection refused"))) as client:
        response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["ready"] is False


def test_readiness_detail_does_not_echo_the_connection_string() -> None:
    """A DSN carries a password; the exception message is not safe to pass through."""
    secret = "postgresql://curate_d:hunter2@db:5432/curate_d"
    with _client(_StubEngine(failure=OSError(f"could not connect to {secret}"))) as client:
        detail = client.get("/health/ready").json()["detail"]
    assert "hunter2" not in detail
    assert detail == "ledger unreachable: OSError"


def test_the_reported_version_matches_the_dbos_application_version() -> None:
    """Workflow records are stamped with it; a mismatch makes them untraceable."""
    from curate_d.runtime.dbos_app import APPLICATION_VERSION

    with _client(_StubEngine()) as client:
        assert client.get("/health/live").json()["version"] == APPLICATION_VERSION


def test_the_schema_is_served() -> None:
    """The OpenAPI schema is served and lists the readiness endpoint."""
    with _client(_StubEngine()) as client:
        schema = client.get("/openapi.json")
    assert schema.status_code == 200
    assert "/health/ready" in schema.json()["paths"]


@pytest.mark.parametrize("path", ["/health/live", "/health/ready"])
def test_health_endpoints_are_documented(path: str) -> None:
    """Each health endpoint appears in the served OpenAPI schema."""
    with _client(_StubEngine()) as client:
        assert path in client.get("/openapi.json").json()["paths"]


def test_the_production_app_starts_live_while_the_database_is_down() -> None:
    """The real backends are built without touching the database; readiness reports it."""
    unreachable = Settings().model_copy(
        update={"ledger_database_url": "postgresql+psycopg://curate:x@127.0.0.1:9/curate"}
    )
    with TestClient(create_app(unreachable)) as client:
        assert client.get("/health/live").status_code == 200
        assert client.get("/health/ready").status_code == 503
