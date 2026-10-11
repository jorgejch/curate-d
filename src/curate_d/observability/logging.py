"""Structured logging setup.

JSON in deployment so records are queryable; human-readable locally. The redaction
processor sits last in the chain, after everything that might add a field, so nothing
can be bound to an event after its last chance to be masked.
"""

import logging
import sys
import threading
from collections.abc import MutableMapping

import structlog
from structlog.typing import EventDict, WrappedLogger

from curate_d.observability.redaction import Redactable, redact

_WRITE_LOCK = threading.Lock()


class _StderrLogger:
    """A structlog logger writing each rendered event to the `sys.stderr` current at write time.

    `structlog.PrintLogger` keeps the stream it was built with, and a cached logger is built
    once per process, so a stream swapped in later (pytest's per-test capture, a redirect)
    would be written to after it closed. Stateless, so caching it is safe.
    """

    def msg(self, message: str) -> None:
        """Write one rendered event and a newline, then flush."""
        with _WRITE_LOCK:
            stream = sys.stderr
            stream.write(message + "\n")
            stream.flush()

    log = debug = info = warn = warning = msg
    fatal = failure = err = error = critical = exception = msg


_STDERR_LOGGER = _StderrLogger()


def _stderr_logger_factory(*_args: object) -> _StderrLogger:
    """Return the shared stderr logger. A structlog logger factory; arguments are ignored."""
    return _STDERR_LOGGER


def redaction_processor(
    _logger: WrappedLogger, _method_name: str, event_dict: EventDict
) -> EventDict:
    """Mask credentials in every field of an event. A structlog processor."""
    redacted = redact(event_dict)
    assert isinstance(redacted, MutableMapping)
    return redacted


def configure_logging(*, json_output: bool = True, level: int = logging.INFO) -> None:
    """Install the shared processor chain. Safe to call more than once."""
    renderer: structlog.typing.Processor = (
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            # Last before rendering: every field that will be emitted now exists.
            redaction_processor,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=_stderr_logger_factory,
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a bound logger for a module."""
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger


__all__ = ["Redactable", "configure_logging", "get_logger", "redaction_processor"]
