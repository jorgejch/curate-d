"""The gate between DBOS Conductor and DBOS's own command dispatcher.

DBOS's `ConductorWebsocket.run()` opens its socket through the module-level name
`dbos._conductor.conductor.connect`, then loops over `recv()` and dispatches every message
to a handler that may cancel, fork, rewind, delete or prune workflows. The protocol carries
no operator identity and DBOS audits none of it, so this module replaces that `connect`
with a wrapper whose connection proxy reads each message first and classifies its type:

- read-only messages reach DBOS's dispatcher unchanged;
- everything else is answered with an error saying Conductor is read-only in curate.d, and
  never dispatched.

Conductor is for observability only. It delivers a write command to every connected
executor in turn, each time under a new request ID, so a command would run once per
executor, and no request ID could tell a redelivery from a new command. A few refusals
carry a further reason: `RETENTION`, `IMPORT_WORKFLOW`, whose payload is never decoded,
`BACKFILL_SCHEDULE`, which belongs to the deployment-time schedule tooling (issue #6),
and `RECOVERY`: DBOS's handler re-enqueues every PENDING row of the executors it names,
live or not, while each curate.d worker keeps its identity across restarts and recovers its
own rows at startup.

DBOS's dispatcher therefore only ever sees read-only messages. Requests are parsed and
refusals built with DBOS's own `protocol` dataclasses, so the wire format stays DBOS's.

The same wrapper owns the transport: an explicit proxy setting instead of `HTTPS_PROXY`,
capped exponential backoff between failed attempts (DBOS itself retries every second), and
one structured event per failed attempt. DBOS embeds the key in the URL path and logs the
URL and every failed attempt's error text on its own logger, so `start_conductor` attaches
a `KeyScrubFilter` there.

Replacing a module global is process-wide, but each worker holds exactly one Conductor
connection and the original `connect` is captured once. The global, the `recv`/`send`
loop it feeds and the protocol dataclasses are private DBOS surface; tests pin them.

`start_conductor` opens that connection after a worker has launched DBOS. DBOS is never
given the key, because a keyed DBOS replaces the executor identity with a random one and
skips startup recovery; the thread is built here and handed to DBOS only for shutdown.
"""

import logging
import sys
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from types import MappingProxyType, TracebackType
from typing import Final, Protocol, Self, cast
from urllib.parse import quote, urlsplit

import dbos._conductor.conductor as dbos_conductor
from dbos._conductor import protocol as p
from dbos._conductor.conductor import ConductorWebsocket
from dbos._dbos import _get_dbos_instance
from websockets import ConnectionClosedOK, InvalidStatus
from websockets.sync.connection import Connection
from websockets.typing import Data

from curate_d.config import Settings
from curate_d.observability.logging import get_logger
from curate_d.observability.redaction import MASK
from curate_d.runtime.dbos_app import application_name
from curate_d.runtime.profiles import WorkerProfile

logger = get_logger(__name__)

INITIAL_BACKOFF_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 300.0


class CommandClass(StrEnum):
    """How the gate treats a Conductor message type."""

    READ_ONLY = "read_only"
    """Returned to DBOS's dispatcher unchanged."""

    REFUSED = "refused"
    """Answered by the gate with an error response, never dispatched."""


_MT = p.MessageType

READ_ONLY: Final = frozenset(
    {
        _MT.EXECUTOR_INFO,
        _MT.LIST_WORKFLOWS,
        _MT.LIST_QUEUED_WORKFLOWS,
        _MT.GET_WORKFLOW,
        _MT.LIST_STEPS,
        _MT.GET_WORKFLOW_EVENTS,
        _MT.GET_WORKFLOW_NOTIFICATIONS,
        _MT.GET_WORKFLOW_STREAMS,
        _MT.EXIST_PENDING_WORKFLOWS,
        _MT.GET_METRICS,
        _MT.ALERT,
        _MT.EXPORT_WORKFLOW,
        _MT.LIST_SCHEDULES,
        _MT.GET_SCHEDULE,
        _MT.LIST_APPLICATION_VERSIONS,
        _MT.GET_WORKFLOW_AGGREGATES,
        _MT.GET_STEP_AGGREGATES,
        _MT.LIST_QUEUES,
        _MT.GET_QUEUE,
    }
)
"""The message types DBOS's dispatcher is given: the ones that change nothing."""

WRITES: Final = frozenset(
    {
        _MT.CANCEL,
        _MT.RESUME,
        _MT.PAUSE_SCHEDULE,
        _MT.RESUME_SCHEDULE,
        _MT.TRIGGER_SCHEDULE,
        _MT.DELETE,
        _MT.FORK_WORKFLOW,
        _MT.FORK_FROM_FAILURE,
        _MT.REWIND_WORKFLOW,
        _MT.SET_LATEST_APPLICATION_VERSION,
        _MT.BACKFILL_SCHEDULE,
        _MT.RETENTION,
        _MT.IMPORT_WORKFLOW,
        _MT.RECOVERY,
    }
)
"""The message types that change workflows, schedules or versions; all refused."""

READ_ONLY_REFUSAL: Final = "Conductor is read-only in curate.d"

REFUSALS: Mapping[p.MessageType, str] = MappingProxyType(
    {
        # A backfill fires every run a window held, which is deployment-time schedule work.
        _MT.BACKFILL_SCHEDULE: (
            "backfilling a schedule belongs to the deployment-time schedule tooling "
            "(issue #6), not to Conductor"
        ),
        # Conductor retention would be a second pruning policy.
        _MT.RETENTION: "retention.py is the only owner of workflow-history pruning",
        # The payload is a pickle DBOS would load: code from whoever holds the other end.
        _MT.IMPORT_WORKFLOW: "its payload is a pickle, which curate.d never decodes",
        # DBOS's handler re-enqueues every PENDING row the named executors own, live or not,
        # and curate.d's executors keep their identity across restarts.
        _MT.RECOVERY: (
            "curate.d recovers its executors itself: each restarted worker recovers its own "
            "rows at startup under its stable identity"
        ),
    }
)
"""The refused message types whose error response gives a reason beyond read-only."""

CLASSIFICATION: Mapping[p.MessageType, CommandClass] = MappingProxyType(
    {
        **dict.fromkeys(READ_ONLY, CommandClass.READ_ONLY),
        **dict.fromkeys(WRITES, CommandClass.REFUSED),
    }
)
"""Every DBOS message type the gate knows, by class. Unlisted types are refused."""


def classify(message_type: object) -> CommandClass:
    """Return the class of a message's `type` field, `REFUSED` for anything unlisted.

    `MessageType` is a `(str, Enum)` whose members hash by name, not value, so the raw
    string from the wire is converted rather than looked up directly.
    """
    if not isinstance(message_type, str):
        return CommandClass.REFUSED
    try:
        return CLASSIFICATION.get(p.MessageType(message_type), CommandClass.REFUSED)
    except ValueError:
        return CommandClass.REFUSED


def refusal(message_type: object) -> str:
    """Return the error a refused message type is answered with, with any further reason."""
    refused = f"curate.d refuses Conductor command type {message_type!r}: {READ_ONLY_REFUSAL}"
    try:
        reason = REFUSALS.get(p.MessageType(message_type))
    except ValueError:
        reason = None
    return refused if reason is None else f"{refused}; {reason}"


def log_refusal(raw: str, message: p.BaseMessage, recoveries_refused: set[str]) -> None:
    """Log a refused message, warning about each executor's refused `RECOVERY` only once.

    How Conductor treats a failed `RECOVERY` is undocumented. If it asks again for an
    executor that never comes back, the repeats are logged at debug rather than as one
    warning each. Every request is still answered with the refusal.

    Args:
        raw: The message as received.
        message: `raw`'s envelope.
        recoveries_refused: Executors already warned about on this connection; updated.
    """
    fields: dict[str, object] = {"message_type": message.type, "request_id": message.request_id}
    if message.type == _MT.RECOVERY:
        try:
            executor_ids = set(p.RecoveryRequest.from_json(raw).executor_ids)
        except (ValueError, TypeError, AttributeError):
            executor_ids = set()
        if executor_ids:
            fields["executor_ids"] = sorted(executor_ids)
            if executor_ids <= recoveries_refused:
                logger.debug("conductor.command_refused", **fields)
                return
            recoveries_refused |= executor_ids
    logger.warning("conductor.command_refused", **fields)


def error_response(message: p.BaseMessage, error_message: str) -> str:
    """Serialize an error response to a request, echoing its type and request ID."""
    return p.BaseResponse(
        type=message.type, request_id=message.request_id, error_message=error_message
    ).to_json()


class GatedConnection:
    """A websocket connection that passes only read-only Conductor messages to its reader.

    `recv()` answers every other message with a refusal and keeps reading, so DBOS's
    dispatcher never sees them. Everything else delegates to the wrapped connection,
    and the context manager yields this proxy rather than the connection it wraps.
    """

    def __init__(self, connection: Connection) -> None:
        """Wrap the open websocket connection to Conductor."""
        self._connection = connection
        self._recoveries_refused: set[str] = set()

    def recv(self, timeout: float | None = None, decode: bool | None = None) -> Data:
        """Return the next read-only message, answering every other message in between.

        Each underlying read gets the whole `timeout`: DBOS's loop uses it only to look at
        its stop event and the close code between reads.

        Raises:
            TimeoutError: No message arrived within `timeout` of the last one.
            websockets.ConnectionClosed: The connection closed.
        """
        while True:
            raw = self._connection.recv(timeout=timeout, decode=decode)
            if self._admit(raw):
                return raw

    def _admit(self, raw: Data) -> bool:
        """Return whether a message goes to the reader; refuse or drop it otherwise."""
        if not isinstance(raw, str):
            logger.warning("conductor.message_malformed", reason="binary frame")
            return False
        try:
            message = p.BaseMessage.from_json(raw)
        except (ValueError, TypeError, AttributeError) as exc:
            # No request ID to answer: DBOS's dispatcher would raise and reconnect on it.
            logger.warning("conductor.message_malformed", reason=type(exc).__name__)
            return False
        if classify(message.type) is CommandClass.READ_ONLY:
            return True
        log_refusal(raw, message, self._recoveries_refused)
        self._connection.send(error_response(message, refusal(message.type)))
        return False

    def __getattr__(self, name: str) -> object:
        """Delegate every other attribute to the wrapped connection.

        DBOS's loop sends through it and reads `close_code` after a read times out, and
        `DBOS.destroy` closes it (`conductor_websocket.websocket.close()`) at shutdown.
        DBOS's keepalive thread, which would ping through it too, runs only with websockets
        older than 15.
        """
        return getattr(self._connection, name)

    def __enter__(self) -> Self:
        """Enter the wrapped connection's context and yield this proxy."""
        self._connection.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Exit the wrapped connection's context, closing it."""
        self._connection.__exit__(exc_type, exc_value, traceback)


@dataclass
class Backoff:
    """Capped exponential delay between failed connection attempts."""

    initial: float = INITIAL_BACKOFF_SECONDS
    cap: float = MAX_BACKOFF_SECONDS
    failures: int = 0

    @property
    def delay(self) -> float:
        """Return the wait before the next attempt: zero until an attempt has failed."""
        if self.failures == 0:
            return 0.0
        # `2.0 ** n` raises OverflowError past the largest float exponent; the cap is
        # reached long before that, so bound the exponent rather than the result.
        doublings = min(self.failures - 1, sys.float_info.max_exp - 1)
        return min(self.cap, self.initial * 2.0**doublings)

    def failed(self) -> None:
        """Record a failed attempt."""
        self.failures += 1

    def reset(self) -> None:
        """Record a successful connection."""
        self.failures = 0


class ConnectFunction[T](Protocol):
    """The shape of `websockets.sync.client.connect` as the gate forwards it."""

    def __call__(self, uri: str, /, **kwargs: object) -> T:
        """Open a websocket connection to `uri`."""
        ...


def make_gated_connect(
    *,
    original: ConnectFunction[Connection],
    key: str,
    proxy_url: str | None,
    stop: threading.Event,
    wait: Callable[[float], bool] | None = None,
    backoff: Backoff | None = None,
) -> ConnectFunction[GatedConnection]:
    """Build the replacement for DBOS's module-level `connect`.

    The replacement takes `connect`'s arguments, overrides `proxy` with the setting (so
    `HTTPS_PROXY` in the environment is never used), waits out the backoff, connects and
    returns the connection wrapped in a `GatedConnection`.

    Args:
        original: DBOS's own `connect`.
        key: The Conductor key, scrubbed from the `conductor.connect_failed` reason.
        proxy_url: The HTTP proxy for this connection only, or `None` to connect directly.
        stop: The Conductor thread's stop event; setting it cuts a backoff wait short.
        wait: Waits up to the given seconds and returns whether it was interrupted.
            Defaults to `stop.wait`.
        backoff: Backoff state; a fresh one by default.

    Returns:
        The replacement. When the stop event cuts a wait short it raises
        `ConnectionClosedOK`, which DBOS's loop treats as a clean exit once the event is
        set. A failed attempt re-raises its error for DBOS's loop to log, on the logger
        `start_conductor` filters the key from; a rejected handshake's body is first made
        valid UTF-8 (`_decodable`).

    Raises:
        ValueError: The key is empty, so it could not be scrubbed.
    """
    if not key:
        raise ValueError("the Conductor key is empty")
    wait = wait or stop.wait
    backoff = backoff or Backoff()

    def gated_connect(uri: str, /, **kwargs: object) -> GatedConnection:
        """Connect to Conductor after any backoff and return the gated connection."""
        delay = backoff.delay
        if delay and (wait(delay) or stop.is_set()):
            raise ConnectionClosedOK(None, None)
        try:
            connection = original(uri, **{**kwargs, "proxy": proxy_url})
        except InvalidStatus as exc:
            backoff.failed()
            logger.error(
                "conductor.rejected",
                status=exc.response.status_code,
                retry_in_seconds=backoff.delay,
            )
            decodable = _decodable(exc)
            if decodable is exc:
                raise
            raise decodable from None
        except Exception as exc:
            backoff.failed()
            # This logger is curate.d's, which `KeyScrubFilter` does not cover.
            reason = _scrub(f"{type(exc).__name__}: {exc}", key)
            logger.error("conductor.connect_failed", reason=reason, retry_in_seconds=backoff.delay)
            raise
        backoff.reset()
        logger.info("conductor.connected", proxied=proxy_url is not None)
        return GatedConnection(connection)

    return gated_connect


def _decodable(exc: InvalidStatus) -> InvalidStatus:
    """Return a rejected handshake whose body is valid UTF-8: `exc` itself, or a copy.

    DBOS's loop decodes the body as strict UTF-8 inside its `except InvalidStatus`, so a
    body that is not (a proxy's or CDN's error page, say) would raise there and end the
    Conductor thread for good. The copy replaces the undecodable bytes.
    """
    body = bytes(exc.response.body)
    try:
        body.decode("utf-8")
    except UnicodeDecodeError:
        text = body.decode("utf-8", "replace").encode()
        return InvalidStatus(replace(exc.response, body=text))
    return exc


def _scrub(text: str, key: str) -> str:
    """Remove a key, raw or percent-encoded, from text."""
    for form in {key, quote(key, safe="")}:
        text = text.replace(form, MASK)
    return text


# Read as `**kwargs: object` so the gate forwards whatever DBOS passes, unchanged.
DBOS_CONNECT: Final = cast(
    "ConnectFunction[Connection]",
    dbos_conductor.connect,  # type: ignore[attr-defined]
)
"""DBOS's own `connect`, captured once at import, before anything can replace it."""


def install_gate(
    *,
    key: str,
    proxy_url: str | None,
    stop: threading.Event,
) -> ConnectFunction[GatedConnection]:
    """Replace DBOS's module-level `connect` with a gated one and return it.

    Idempotent: a second call replaces the first wrapper, and every wrapper is built on
    DBOS's captured original, never on a previous wrapper.
    """
    gated = make_gated_connect(original=DBOS_CONNECT, key=key, proxy_url=proxy_url, stop=stop)
    dbos_conductor.connect = gated  # type: ignore[attr-defined, assignment]
    return gated


def uninstall_gate() -> None:
    """Restore DBOS's own `connect`."""
    dbos_conductor.connect = DBOS_CONNECT  # type: ignore[attr-defined, assignment]


HOSTED_CONDUCTOR_URL: Final = "wss://cloud.dbos.dev/conductor/v1alpha1"
"""DBOS's own default endpoint, used when no URL is configured. `DBOS_DOMAIN` is not read."""

KEY_SETTING: Final = "CURATE_D_CONDUCTOR_KEY"
ENVIRONMENT_SETTING: Final = "CURATE_D_ENVIRONMENT"


class KeyScrubFilter(logging.Filter):
    """Remove the Conductor key from every record logged on the logger it is attached to.

    DBOS logs the websocket URL, key included, at debug level when it builds the thread,
    and passes the same logger to websockets, which logs the handshake's request line. Its
    loop logs every failed connection attempt there too: the error text, and a rejected
    handshake's body. None of these carries a traceback, so the message is all there is.
    """

    def __init__(self, key: str) -> None:
        """Scrub `key`, raw or percent-encoded."""
        super().__init__()
        self._key = key

    def filter(self, record: logging.LogRecord) -> bool:
        """Rewrite the record's message without the key; never drop the record."""
        try:
            message = record.getMessage()
        except (TypeError, ValueError):
            # Left to the handler, which reports a malformed record without raising here.
            return True
        scrubbed = _scrub(message, self._key)
        if scrubbed != message:
            record.msg, record.args = scrubbed, None
        return True


def _install_key_filter(target: logging.Logger, key: str) -> None:
    """Attach a `KeyScrubFilter` for `key`, replacing any earlier one."""
    for existing in [f for f in target.filters if isinstance(f, KeyScrubFilter)]:
        target.removeFilter(existing)
    target.addFilter(KeyScrubFilter(key))


def start_conductor(settings: Settings, profile: WorkerProfile) -> ConductorWebsocket | None:
    """Open this worker's Conductor connection if the settings call for one.

    Call after `DBOS.launch()` and queue registration. Logs exactly one of
    `conductor.disabled`, `conductor.misconfigured` or `conductor.connecting`. Never
    raises over configuration: workflow execution does not depend on Conductor.

    Args:
        settings: Application settings.
        profile: The worker's profile, for the log event.

    Returns:
        The started Conductor thread, also assigned to the DBOS instance's
        `conductor_websocket` so `DBOS.destroy()` stops it; `None` when disabled or
        misconfigured.
    """
    if not settings.conductor_enabled:
        logger.info("conductor.disabled", profile=profile.value)
        return None
    # Settings already folds a blank key into None; the strip guards a caller that built
    # the model without validation, so a blank never reaches `make_gated_connect`.
    key = None if settings.conductor_key is None else settings.conductor_key.get_secret_value()
    if key is None or not key.strip():
        logger.error(
            "conductor.misconfigured",
            profile=profile.value,
            reason=f"conductor_enabled is set but {KEY_SETTING} is not",
            setting=KEY_SETTING,
        )
        return None
    if settings.environment is None:
        logger.error(
            "conductor.misconfigured",
            profile=profile.value,
            reason=f"conductor_enabled is set but {ENVIRONMENT_SETTING} is not, so the "
            f"Conductor application to register under is unknown",
            setting=ENVIRONMENT_SETTING,
        )
        return None
    app_name = application_name(settings.environment)

    url = HOSTED_CONDUCTOR_URL if settings.conductor_url is None else str(settings.conductor_url)
    proxy_url = None if settings.conductor_proxy_url is None else str(settings.conductor_proxy_url)
    dbos_instance = _get_dbos_instance()
    _install_key_filter(dbos_instance.logger, key)
    # One event for both: DBOS's destroy sets it, which also cuts a gate backoff short.
    stop = threading.Event()
    install_gate(key=key, proxy_url=proxy_url, stop=stop)
    logger.info(
        "conductor.connecting",
        profile=profile.value,
        app_name=app_name,
        host=urlsplit(url).hostname,
        proxied=proxy_url is not None,
    )
    websocket = ConductorWebsocket(
        dbos_instance, app_name=app_name, conductor_url=url, conductor_key=key, evt=stop
    )
    dbos_instance.conductor_websocket = websocket
    websocket.start()
    return websocket
