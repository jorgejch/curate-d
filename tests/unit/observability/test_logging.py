"""Structured logging: the stderr stream and the export to Logfire.

Export is captured by an in-memory exporter in place of Logfire's (`capture.traced_to`).
structlog keeps the processor chain a logger was first used with, so every test takes a
fresh logger after configuring. pytest swaps `sys.stderr` between a test's setup and its
body, so stderr is captured inside the body.
"""

import io
import logging
import threading
from collections.abc import Iterator

import pytest
from logfire.testing import TestExporter
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

from curate_d.observability import MASK, configure_logging, get_logger
from tests.unit.observability.capture import (
    exported_text,
    json_lines,
    records_named,
    stderr_captured,
    traced_to,
)

SECRET = "s3cr3t-code"
EVENT = "staging.writer_lock_held"
FIELDS: dict[str, object] = {
    "caller": "ingest",
    "wait_seconds": 0.25,
    "held_seconds": 1.5,
    "entry_claims": 3,
}


@pytest.fixture
def exporter() -> Iterator[TestExporter]:
    """Configure tracing over an in-memory exporter for one test and return it."""
    captured = TestExporter()
    with traced_to(SimpleSpanProcessor(captured)):
        yield captured


def _is_masked(value: object) -> bool:
    """Return whether `value` is our mask or Logfire's scrub marker, which replaces it."""
    return isinstance(value, str) and (value == MASK or value.startswith("[Scrubbed due to "))


def test_a_cached_logger_writes_to_the_current_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stream swapped in after first use receives output; the closed one is not touched."""
    first, second = io.StringIO(), io.StringIO()
    monkeypatch.setattr("sys.stderr", first)
    configure_logging(json_output=True, level=logging.INFO)
    log = get_logger("curate_d.test.stream")
    log.info("before")
    first.close()
    monkeypatch.setattr("sys.stderr", second)
    log.info("after")
    assert '"event": "after"' in second.getvalue()


def test_a_background_thread_event_is_exported_with_its_fields(exporter: TestExporter) -> None:
    """An event logged from another thread arrives as one record carrying its fields."""
    with stderr_captured():
        configure_logging(to_logfire=True)
        log = get_logger("curate_d.test.thread")
        thread = threading.Thread(target=log.info, args=(EVENT,), kwargs=FIELDS)
        thread.start()
        thread.join()
    [record] = records_named(exporter, EVENT)
    attributes = dict(record.attributes or {})
    assert {key: attributes.get(key) for key in FIELDS} == FIELDS


def test_an_exported_event_stays_masked(exporter: TestExporter) -> None:
    """A credential field and a bearer token in free text are exported masked."""
    with stderr_captured() as stderr:
        configure_logging(to_logfire=True)
        get_logger("curate_d.test.masked").info(
            "provider.refresh_failed",
            refresh_token=SECRET,
            detail=f"upstream refused Bearer {SECRET}",
        )
    [record] = records_named(exporter, "provider.refresh_failed")
    attributes = dict(record.attributes or {})
    assert _is_masked(attributes["refresh_token"])
    # Logfire's scrubber has no bearer pattern: only our redaction can have masked this.
    assert attributes["detail"] == f"upstream refused Bearer {MASK}"
    assert SECRET not in exported_text(exporter)
    assert SECRET not in stderr.getvalue()


def test_a_debug_event_under_info_is_neither_exported_nor_written(
    exporter: TestExporter,
) -> None:
    """A debug event below the configured level produces no record and no stderr line."""
    with stderr_captured() as stderr:
        configure_logging(to_logfire=True, level=logging.INFO)
        get_logger("curate_d.test.debug").debug("staging.debug_detail", caller="ingest")
    assert records_named(exporter, "staging.debug_detail") == []
    assert stderr.getvalue() == ""


def test_export_leaves_the_stderr_line_unchanged(exporter: TestExporter) -> None:
    """The stderr JSON line is the same, timestamp aside, with export on and off."""
    with stderr_captured() as stderr:
        configure_logging()
        get_logger("curate_d.test.plain").info(EVENT, **FIELDS)
        configure_logging(to_logfire=True)
        get_logger("curate_d.test.exported").info(EVENT, **FIELDS)
    plain, exported = json_lines(stderr)
    assert records_named(exporter, EVENT)
    plain.pop("timestamp")
    exported.pop("timestamp")
    assert list(exported.items()) == list(plain.items())


def test_logging_without_the_flag_exports_nothing(exporter: TestExporter) -> None:
    """`configure_logging()` alone writes stderr and sends Logfire no record."""
    with stderr_captured() as stderr:
        configure_logging()
        get_logger("curate_d.test.unexported").info(EVENT, **FIELDS)
    assert records_named(exporter, EVENT) == []
    assert [line["event"] for line in json_lines(stderr)] == [EVENT]
