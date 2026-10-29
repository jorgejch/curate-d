"""The alerting test route fails as an unhandled server error, and only where enabled."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from logfire.testing import TestExporter
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from sqlalchemy.ext.asyncio import AsyncEngine

from curate_d.api.app import ALERTING_TEST_PATH, AlertingTestError, create_app
from curate_d.api.backends import Backends
from curate_d.config import DeploymentEnvironment, Settings
from curate_d.observability import trace_requests
from tests.unit.api.fakes import StubEngine, backends
from tests.unit.observability.capture import traced_to


def _app(settings: Settings) -> FastAPI:
    """Build the app under `settings`, over fake backends and a stub engine."""

    @asynccontextmanager
    async def fake_backends(
        _settings: Settings, _engine: AsyncEngine
    ) -> AsyncGenerator[Backends, None]:
        """Yield fake backends; these tests touch no backend."""
        yield backends()

    return create_app(
        settings,
        engine_factory=lambda _settings: StubEngine(),  # type: ignore[arg-type,return-value]
        backends_factory=fake_backends,
    )


def _client(settings: Settings, *, raise_server_exceptions: bool = True) -> TestClient:
    """Build a TestClient for the app under `settings`."""
    return TestClient(_app(settings), raise_server_exceptions=raise_server_exceptions)


def _dev() -> Settings:
    """Return dev settings, built inside a test so `.env` is already detached."""
    return Settings(environment=DeploymentEnvironment.DEV)


def test_the_route_answers_500_with_the_exceptions_message() -> None:
    """The route's body is a Problem whose detail is the alerting test's message."""
    with _client(_dev(), raise_server_exceptions=False) as client:
        response = client.get(ALERTING_TEST_PATH)
    assert response.status_code == 500
    detail = response.json()["detail"]
    assert detail.startswith("deliberate internal error for testing server-error alerting")
    assert "Traceback" not in response.text


def test_other_unhandled_errors_keep_the_generic_body() -> None:
    """Another unhandled error's message never reaches the client where the route is served."""
    app = _app(_dev())

    @app.get("/boom")
    async def boom() -> None:
        """Fail with a message that must stay on the server."""
        raise RuntimeError("postgresql://curate_d:hunter2@db/curate_d")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/boom")
    assert response.status_code == 500
    assert response.text == "Internal Server Error"


def test_the_route_raises_an_unhandled_alerting_test_error() -> None:
    """The exception escapes the app, naming itself a deliberate alerting test."""
    with (
        _client(_dev()) as client,
        pytest.raises(AlertingTestError, match="deliberate internal error"),
    ):
        client.get(ALERTING_TEST_PATH)


def test_the_request_span_records_the_exception() -> None:
    """The traced request's span carries the exception type and its alerting-test message."""
    exporter = TestExporter()
    with traced_to(SimpleSpanProcessor(exporter)):
        app = _app(_dev())
        trace_requests(app)
        with TestClient(app, raise_server_exceptions=False) as client:
            assert client.get(ALERTING_TEST_PATH).status_code == 500
    events = [
        dict(event.attributes or {})
        for span in exporter.exported_spans
        for event in span.events
        if event.name == "exception"
    ]
    assert any(
        str(event.get("exception.type", "")).endswith("AlertingTestError")
        and "deliberate internal error" in str(event.get("exception.message", ""))
        for event in events
    ), events


def test_the_route_is_absent_in_prod_by_default() -> None:
    """Off, the path answers 404 like any unknown path."""
    with _client(Settings(environment=DeploymentEnvironment.PROD)) as client:
        assert client.get(ALERTING_TEST_PATH).status_code == 404


def test_the_route_is_served_in_prod_when_forced_on() -> None:
    """The prod switch mounts the route."""
    forced = Settings(
        environment=DeploymentEnvironment.PROD, alerting_test_routes_force_on_in_prod=True
    )
    with _client(forced, raise_server_exceptions=False) as client:
        assert client.get(ALERTING_TEST_PATH).status_code == 500


def test_the_route_stays_out_of_the_schema() -> None:
    """The OpenAPI document never lists the alerting test route."""
    with _client(_dev()) as client:
        assert ALERTING_TEST_PATH not in client.get("/openapi.json").json()["paths"]
