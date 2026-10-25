"""Telemetry and stderr capture for the observability tests.

Tracing is configured with a local span processor in place of Logfire's exporter, so
nothing is sent. Logfire's and structlog's configurations are process-wide, so each is
put back when its scope ends.
"""

import io
import json
from collections.abc import Iterator
from contextlib import contextmanager

import logfire
import pytest
import structlog
from logfire.testing import TestExporter
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor

from curate_d.observability import configure_tracing


@contextmanager
def traced_to(
    processor: SpanProcessor,
    *,
    environment: str | None = None,
    instance_id: str | None = None,
) -> Iterator[None]:
    """Configure tracing over `processor`, then reconfigure Logfire to export nowhere.

    Logfire has no public reset. Reconfiguring shuts down the previous tracer provider,
    so spans from later tests cannot reach `processor`.
    """
    configure_tracing(
        service_name="test",
        service_version="0",
        environment=environment,
        instance_id=instance_id,
        local_processor=processor,
    )
    try:
        yield
    finally:
        logfire.configure(send_to_logfire=False, console=False, metrics=False)


def records_named(exporter: TestExporter, name: str) -> list[ReadableSpan]:
    """Return the exported records and spans named `name`."""
    return [span for span in exporter.exported_spans if span.name == name]


def exported_text(exporter: TestExporter) -> str:
    """Return every exported record and span serialized, attributes and resource included.

    A span's `repr` names only its class, so a secret check needs the serialized form.
    """
    return "\n".join(span.to_json() for span in exporter.exported_spans)


@contextmanager
def stderr_captured() -> Iterator[io.StringIO]:
    """Swap `sys.stderr` for a buffer; restore it and structlog's configuration afterwards."""
    saved = structlog.get_config()
    buffer = io.StringIO()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("sys.stderr", buffer)
        try:
            yield buffer
        finally:
            structlog.configure(**saved)


def json_lines(buffer: io.StringIO) -> list[dict[str, object]]:
    """Return every JSON line written to `buffer`, in order."""
    return [json.loads(line) for line in buffer.getvalue().splitlines() if line.startswith("{")]
