"""Request tracing, exported to Logfire.

Export needs a token: `LOGFIRE_TOKEN` in a deployment, or `.logfire/` written by the
Logfire CLI on a developer's machine. Without one, spans are created and dropped, so a
process with no token behaves exactly as it did before tracing existed.

Logfire's own scrubbing is extended to the key fragments `redaction.py` masks in logs, and
two things its defaults would send are kept out:

- Endpoint argument values. They include request bodies and OAuth callback parameters;
  only the shape of a validation error is recorded.
- Query strings. Logfire treats URL attributes as safe and never scrubs them, but the
  OAuth callback carries its authorization code there. They are stripped twice: as the
  span starts, before Logfire snapshots it as a pending span, and again from the request
  hook, because the ASGI middleware sets every request attribute a second time after
  starting the span.
"""

from collections.abc import Mapping, Sequence

import logfire
from fastapi import FastAPI, Request, WebSocket
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.sdk.trace import Span, SpanProcessor

from curate_d.observability.redaction import MASK

# Patterns the default scrubber lacks that `redaction.py` treats as credentials.
_EXTRA_SCRUB_PATTERNS = ("token", "apikey")

# Attributes HTTP instrumentations fill with a URL or target, by semantic-convention version.
_URL_ATTRIBUTES = ("http.target", "http.url", "url.full")
_QUERY_ATTRIBUTE = "url.query"

# Kubernetes probes these every few seconds; traced, they would outnumber real requests.
# A failing readiness probe shows as the pod going NotReady, not in traces.
_UNTRACED_PATHS = r"/health/(live|ready)(\?|$)"

# What survives of a validation error: where and why, never the rejected input.
_ERROR_FIELDS = ("type", "loc", "msg")


def strip_query_string(span: trace.Span, _scope: object = None) -> None:
    """Rewrite a span's URL attributes without their query; mask a separate query attribute.

    Also the ASGI server request hook, hence the unused scope.
    """
    attributes = getattr(span, "attributes", None)
    if not isinstance(attributes, Mapping):
        return
    for key in _URL_ATTRIBUTES:
        value = attributes.get(key)
        if isinstance(value, str) and "?" in value:
            span.set_attribute(key, value.split("?", 1)[0])
    if attributes.get(_QUERY_ATTRIBUTE):
        span.set_attribute(_QUERY_ATTRIBUTE, MASK)


class QueryStringStripper(SpanProcessor):
    """Strip query strings from every span as it starts.

    Logfire runs additional processors before the one that snapshots a pending span at
    start, so the pending span never carries the query.
    """

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        """Strip the starting span's URL attributes."""
        strip_query_string(span)


def configure_tracing(
    *,
    service_name: str,
    service_version: str,
    environment: str | None = None,
    local_processor: SpanProcessor | None = None,
) -> None:
    """Configure the process's Logfire exporter. Call once, before instrumenting.

    Args:
        service_name: The `service.name` resource attribute.
        service_version: The `service.version` resource attribute.
        environment: The `deployment.environment.name` resource attribute. None leaves it
            to `LOGFIRE_ENVIRONMENT`, and unset when that is too.
        local_processor: Receives every span instead of the Logfire exporter, which is
            then disabled. Tests pass one over an in-memory exporter.
    """
    logfire.configure(
        send_to_logfire="if-token-present" if local_processor is None else False,
        service_name=service_name,
        service_version=service_version,
        environment=environment,
        # structlog already writes this process's output to stderr.
        console=False,
        scrubbing=logfire.ScrubbingOptions(extra_patterns=list(_EXTRA_SCRUB_PATTERNS)),
        # First, so every later processor and exporter sees the stripped URL.
        additional_span_processors=[
            QueryStringStripper(),
            *([local_processor] if local_processor is not None else []),
        ],
    )


def trace_requests(app: FastAPI) -> None:
    """Create a span for every request `app` serves, except health probes."""
    logfire.instrument_fastapi(
        app,
        request_attributes_mapper=validation_errors_only,
        server_request_hook=strip_query_string,
        excluded_urls=_UNTRACED_PATHS,
    )


def validation_errors_only(
    _request: Request | WebSocket, attributes: dict[str, object]
) -> dict[str, object] | None:
    """Keep the location and type of each validation error, dropping argument values.

    Returns:
        The trimmed errors, or None when the request validated, which tells Logfire to
        record nothing.
    """
    errors = attributes.get("errors")
    if not isinstance(errors, Sequence) or not errors:
        return None
    return {
        "errors": [
            {field: error[field] for field in _ERROR_FIELDS if field in error}
            for error in errors
            if isinstance(error, Mapping)
        ]
    }


__all__ = [
    "QueryStringStripper",
    "configure_tracing",
    "strip_query_string",
    "trace_requests",
    "validation_errors_only",
]
