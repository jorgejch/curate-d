"""Structured logging output stream."""

import io
import logging

import pytest

from curate_d.observability import configure_logging, get_logger


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
