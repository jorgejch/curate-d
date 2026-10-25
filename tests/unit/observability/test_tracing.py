"""Process telemetry: what a span carries to Logfire, and what a failing export costs.

Isolated: tracing is configured with an in-memory exporter in place of Logfire's, so
nothing is sent (`capture.traced_to`). The exporter also receives the pending span Logfire
emits when a span starts, which is where a value set before any hook runs would show up.
"""

import logging
import uuid
from collections.abc import Callable, Iterator, Sequence

import logfire
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from logfire.testing import TestExporter
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult

from curate_d.observability import MASK, configure_logging, get_logger, trace_requests
from curate_d.observability.tracing import validation_errors_only
from tests.unit.observability.capture import json_lines, stderr_captured, traced_to

SECRET = "s3cr3t-code"
BEARER = "s3cr3t-token-value"


@pytest.fixture
def exporter() -> Iterator[TestExporter]:
    """Configure tracing over an in-memory exporter for one test and return it."""
    captured = TestExporter()
    with traced_to(SimpleSpanProcessor(captured)):
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
    with traced_to(SimpleSpanProcessor(captured)):
        logfire.info("inside")
    inside = len(captured.exported_spans)
    logfire.info("after")
    assert inside > 0
    assert len(captured.exported_spans) == inside


def test_spans_carry_the_environment() -> None:
    """Label every span's resource with the configured deployment environment."""
    captured = TestExporter()
    with traced_to(SimpleSpanProcessor(captured), environment="dev"):
        logfire.info("labelled")
    resources = [span.resource.attributes for span in captured.exported_spans]
    assert resources
    assert all(r.get("deployment.environment.name") == "dev" for r in resources)


def test_health_probes_are_not_traced(client: TestClient, exporter: TestExporter) -> None:
    """Create no span for a health probe, so probes do not drown out real requests."""
    assert client.get("/health/live").status_code == 200
    assert exporter.exported_spans == []


def test_spans_carry_the_given_instance_id() -> None:
    """Label every span's resource with the instance id the process passes."""
    captured = TestExporter()
    with traced_to(SimpleSpanProcessor(captured), instance_id="curate-d-worker-io-0"):
        logfire.info("labelled")
    resources = [span.resource.attributes for span in captured.exported_spans]
    assert resources
    assert all(r.get("service.instance.id") == "curate-d-worker-io-0" for r in resources)


def test_no_instance_id_keeps_logfires_generated_one() -> None:
    """Passing no instance id leaves Logfire's generated one in place."""
    captured = TestExporter()
    with traced_to(SimpleSpanProcessor(captured)):
        logfire.info("unlabelled")
    [instance_id] = {
        span.resource.attributes.get("service.instance.id") for span in captured.exported_spans
    }
    assert isinstance(instance_id, str)
    uuid.UUID(instance_id)


class _RaisingExporter(SpanExporter):
    """An exporter that fails every export, as an unreachable backend would."""

    def __init__(self) -> None:
        """Start with no export attempted."""
        self.attempts = 0

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        """Count the attempt and raise."""
        self.attempts += 1
        raise ConnectionError("export failed")


class _RaisingProcessor(SpanProcessor):
    """A span processor that raises on every finished span."""

    def __init__(self) -> None:
        """Start with no span seen."""
        self.ended = 0

    def on_end(self, span: ReadableSpan) -> None:
        """Count the span and raise."""
        self.ended += 1
        raise RuntimeError("processor failed")


def _log_exported_event(processor: SpanProcessor) -> str:
    """Log one event with export on over `processor`; return what reached stderr."""
    with (
        pytest.MonkeyPatch.context() as patch,
        traced_to(processor),
        stderr_captured() as stderr,
    ):
        # Logfire re-raises its internal errors while PYTEST_CURRENT_TEST is set, to fail
        # its own tests loudly; without it, it logs and swallows them, as in production.
        patch.delenv("PYTEST_CURRENT_TEST")
        configure_logging(to_logfire=True)
        returned = get_logger("curate_d.test.failing_export").info(
            "staging.writer_lock_held", caller="ingest"
        )
        assert returned is None
    return stderr.getvalue()


def test_a_raising_exporter_leaves_logging_intact() -> None:
    """A raising exporter drops the record; the stderr line is written, nothing raises."""
    raising = _RaisingExporter()
    written = _log_exported_event(SimpleSpanProcessor(raising))
    assert raising.attempts >= 1
    assert '"event": "staging.writer_lock_held"' in written


def test_a_raising_span_processor_leaves_logging_intact() -> None:
    """A raising span processor drops the record; the stderr line is written, nothing raises."""
    raising = _RaisingProcessor()
    written = _log_exported_event(raising)
    assert raising.ended >= 1
    assert '"event": "staging.writer_lock_held"' in written


@pytest.mark.parametrize(
    "processor",
    [
        pytest.param(lambda: SimpleSpanProcessor(_RaisingExporter()), id="exporter"),
        pytest.param(_RaisingProcessor, id="span_processor"),
    ],
)
def test_a_telemetry_failure_while_logging_an_exception_leaks_nothing(
    processor: Callable[[], SpanProcessor],
) -> None:
    """A telemetry failure inside an except block prints the handled exception masked."""
    with (
        pytest.MonkeyPatch.context() as patch,
        traced_to(processor()),
        stderr_captured() as stderr,
    ):
        patch.delenv("PYTEST_CURRENT_TEST")
        # pytest's root handlers capture what a process without them prints through
        # `logging.lastResort`, the path a deployed worker's library diagnostics take.
        patch.setattr(logging.getLogger(), "handlers", [])
        configure_logging(to_logfire=True)
        try:
            raise RuntimeError(f"upstream refused Bearer {BEARER}")
        except RuntimeError:
            get_logger("curate_d.test.failing_export").exception("provider.call_failed")
    assert BEARER not in stderr.getvalue()
    records = json_lines(stderr)
    (event,) = [record for record in records if record["event"] == "provider.call_failed"]
    assert f"Bearer {MASK}" in str(event["exception"])
    (diagnostic,) = [record for record in records if record is not event]
    assert str(diagnostic["logger"]).startswith(("logfire", "opentelemetry"))
    assert f"Bearer {MASK}" in str(diagnostic["exception"])
