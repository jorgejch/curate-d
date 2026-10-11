"""Request tracing: what a request span carries to Logfire.

Isolated: tracing is configured with an in-memory exporter in place of Logfire's, so
nothing is sent. The exporter also receives the pending span Logfire emits when a span
starts, which is where a value set before any hook runs would show up. Logfire's
configuration is process-wide, so each test detaches its exporter when it ends.
"""

from collections.abc import Iterator
from contextlib import contextmanager

import logfire
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from logfire.testing import TestExporter
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

from curate_d.observability import configure_tracing, trace_requests
from curate_d.observability.tracing import validation_errors_only

SECRET = "s3cr3t-code"


@contextmanager
def _traced_to(captured: TestExporter, environment: str | None = None) -> Iterator[None]:
    """Configure tracing over `captured`, then reconfigure Logfire to export nowhere.

    Logfire has no public reset. Reconfiguring shuts down the previous tracer provider,
    so spans from later tests cannot reach `captured`.
    """
    configure_tracing(
        service_name="test",
        service_version="0",
        environment=environment,
        local_processor=SimpleSpanProcessor(captured),
    )
    try:
        yield
    finally:
        logfire.configure(send_to_logfire=False, console=False, metrics=False)


@pytest.fixture
def exporter() -> Iterator[TestExporter]:
    """Configure tracing over an in-memory exporter for one test and return it."""
    captured = TestExporter()
    with _traced_to(captured):
        yield captured


@pytest.fixture
def client(exporter: TestExporter) -> Iterator[TestClient]:
    """A traced app with an OAuth-callback-shaped endpoint."""
    app = FastAPI()

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        """Answer like the real liveness probe."""
        return {"status": "ok"}

    @app.get("/callback")
    async def callback(code: str, attempts: int = 1) -> dict[str, int]:
        """Accept a secret-bearing query parameter."""
        return {"attempts": attempts}

    trace_requests(app)
    with TestClient(app) as test_client:
        yield test_client


def _attributes(exporter: TestExporter) -> list[dict[str, object]]:
    """Return the attributes of every exported span, pending spans included."""
    return [dict(span.attributes or {}) for span in exporter.exported_spans]


def test_request_creates_a_server_span(client: TestClient, exporter: TestExporter) -> None:
    """Record a span named after the route for a served request."""
    assert client.get("/callback", params={"code": SECRET}).status_code == 200
    assert "GET /callback" in [span.name for span in exporter.exported_spans]


def test_query_string_is_not_exported(client: TestClient, exporter: TestExporter) -> None:
    """Strip the query string, which carries the OAuth code, from every span."""
    client.get("/callback", params={"code": SECRET})
    attributes = _attributes(exporter)
    assert SECRET not in repr(attributes)
    assert any(a.get("http.target") == "/callback" for a in attributes)


def test_argument_values_are_not_exported(client: TestClient, exporter: TestExporter) -> None:
    """Record no endpoint argument values for a request that validated."""
    client.get("/callback", params={"code": SECRET, "attempts": "7"})
    assert not any("fastapi.arguments.values" in a for a in _attributes(exporter))


def test_validation_error_keeps_location_not_input(
    client: TestClient, exporter: TestExporter
) -> None:
    """Record where validation failed without the rejected input."""
    response = client.get("/callback", params={"code": SECRET, "attempts": "many"})
    assert response.status_code == 422
    attributes = _attributes(exporter)
    errors = [a["fastapi.arguments.errors"] for a in attributes if "fastapi.arguments.errors" in a]
    assert errors
    assert "attempts" in repr(errors)
    assert "many" not in repr(attributes)
    assert SECRET not in repr(attributes)


def test_token_fields_are_scrubbed(exporter: TestExporter) -> None:
    """Scrub a `token` attribute, which Logfire's default patterns would send."""
    logfire.info("refreshed", refresh_token=SECRET)
    assert SECRET not in repr(_attributes(exporter))


def test_mapper_records_nothing_without_errors() -> None:
    """Tell Logfire to record nothing when the request had no validation errors."""
    attributes: dict[str, object] = {"values": {"code": SECRET}, "errors": []}
    assert validation_errors_only(None, attributes) is None  # type: ignore[arg-type]


def test_exporter_is_detached_after_the_test() -> None:
    """Send no span to a test's exporter once its tracing scope has ended."""
    captured = TestExporter()
    with _traced_to(captured):
        logfire.info("inside")
    inside = len(captured.exported_spans)
    logfire.info("after")
    assert inside > 0
    assert len(captured.exported_spans) == inside


def test_spans_carry_the_environment() -> None:
    """Label every span's resource with the configured deployment environment."""
    captured = TestExporter()
    with _traced_to(captured, environment="dev"):
        logfire.info("labelled")
    resources = [span.resource.attributes for span in captured.exported_spans]
    assert resources
    assert all(r.get("deployment.environment.name") == "dev" for r in resources)


def test_health_probes_are_not_traced(client: TestClient, exporter: TestExporter) -> None:
    """Create no span for a health probe, so probes do not drown out real requests."""
    assert client.get("/health/live").status_code == 200
    assert exporter.exported_spans == []
