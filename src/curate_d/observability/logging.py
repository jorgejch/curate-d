"""Structured logging setup.

JSON in deployment so records are queryable; human-readable locally. The redaction
processor sits after everything that might add a field, so nothing can be bound to an
event after its last chance to be masked.

A process that exports telemetry also sends each event to Logfire as a log record. The
export reads the same masked dict stderr gets, from between redaction and rendering, and
leaves it unchanged, so the stderr line is the same with export on or off. Logfire must be
configured (`tracing.configure_tracing`) for the record to go anywhere.

Logfire and OpenTelemetry report their own failures through stdlib `logging`, with a
traceback. A failure inside a caller's `except` block chains the caller's exception into
that traceback, so those loggers are rendered through the same redaction as every event.
"""

import contextlib
import logging
import sys
import threading
from collections.abc import MutableMapping

import logfire
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


# Logfire's internal errors and OpenTelemetry's exporter and processor failures.
_TELEMETRY_LOGGERS = ("logfire", "opentelemetry")


class _RedactedStderrHandler(logging.Handler):
    """A stdlib handler writing each formatted record through the shared stderr logger.

    Its formatter is a structlog `ProcessorFormatter` ending in redaction and rendering.
    """

    def emit(self, record: logging.LogRecord) -> None:
        """Format the record and write it to the current stderr."""
        try:
            _STDERR_LOGGER.msg(self.format(record))
        except Exception:
            self.handleError(record)

    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802 - stdlib name
        """Write a fixed line naming the logger, never the record or the active traceback.

        The default prints both, unredacted, and the active traceback may be the caller's.
        """
        # Nowhere left to report a failure of this write.
        with contextlib.suppress(Exception):
            _STDERR_LOGGER.msg(
                f'{{"event": "logging.diagnostic_unrenderable", "logger": "{record.name}"}}'
            )


_TELEMETRY_HANDLER = _RedactedStderrHandler(logging.WARNING)


def _route_telemetry_diagnostics(renderer: structlog.typing.Processor) -> None:
    """Send the telemetry libraries' stdlib records through redaction to stderr, only.

    Without a handler of their own they reach `logging.lastResort`, which prints the
    traceback unredacted. Not propagated, so no other handler gets the raw record.
    """
    _TELEMETRY_HANDLER.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=[
                structlog.stdlib.add_logger_name,
                structlog.processors.add_log_level,
                structlog.processors.TimeStamper(fmt="iso", utc=True),
            ],
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.StackInfoRenderer(),
                structlog.processors.format_exc_info,
                redaction_processor,
                renderer,
            ],
        )
    )
    for name in _TELEMETRY_LOGGERS:
        logger = logging.getLogger(name)
        if _TELEMETRY_HANDLER not in logger.handlers:
            logger.addHandler(_TELEMETRY_HANDLER)
        logger.propagate = False


def configure_logging(
    *, json_output: bool = True, level: int = logging.INFO, to_logfire: bool = False
) -> None:
    """Install the shared processor chain and route telemetry diagnostics through it.

    Safe to call more than once.

    Args:
        level: Events below it are dropped before any processor, export included.
        to_logfire: Also send every event to Logfire as a log record. Only entry points
            that configure Logfire pass it; elsewhere each record would be dropped with a
            not-configured warning.
    """
    exporters: list[structlog.typing.Processor] = (
        [logfire.StructlogProcessor(console_log=False)] if to_logfire else []
    )
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
            # Every field that will be emitted now exists.
            redaction_processor,
            # After redaction, so the export never sees an unmasked field.
            *exporters,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=_stderr_logger_factory,
        cache_logger_on_first_use=True,
    )
    _route_telemetry_diagnostics(renderer)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a bound logger for a module."""
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger


__all__ = ["Redactable", "configure_logging", "get_logger", "redaction_processor"]
