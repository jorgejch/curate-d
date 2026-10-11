"""The Conductor gate: classification, the proxy, its refusals, the transport, startup."""

import base64
import gzip
import inspect
import json
import logging
import pickle
import threading
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace, TracebackType
from typing import TYPE_CHECKING, ClassVar, Final, cast
from urllib.parse import quote

import dbos._conductor.conductor as dbos_conductor
import dbos._dbos as dbos_module
import pytest
import structlog
from dbos._conductor import protocol as p
from pydantic import SecretStr
from structlog.testing import LogCapture
from structlog.typing import EventDict
from websockets import ConnectionClosedOK, InvalidStatus
from websockets.datastructures import Headers
from websockets.http11 import Response
from websockets.proxy import Proxy, parse_proxy
from websockets.sync import client as websockets_client
from websockets.sync.connection import Connection
from websockets.typing import Data

from curate_d.config import Settings
from curate_d.runtime import conductor
from curate_d.runtime.conductor import (
    CLASSIFICATION,
    DBOS_CONNECT,
    HOSTED_CONDUCTOR_URL,
    MAX_BACKOFF_SECONDS,
    READ_ONLY,
    REFUSALS,
    WRITES,
    Backoff,
    CommandClass,
    GatedConnection,
    KeyScrubFilter,
    classify,
    install_gate,
    make_gated_connect,
    start_conductor,
    uninstall_gate,
)
from curate_d.runtime.profiles import WorkerProfile

if TYPE_CHECKING:
    from dbos import DBOS

KEY = "fake-conductor-key"
URL = f"wss://conductor.invalid/conductor/v1alpha1/websocket/curate-d/{KEY}"
PROXY = "http://egress-proxy:3128"


class FakeConnection:
    """A websocket connection that replays queued messages and records what is sent."""

    def __init__(
        self, messages: Iterable[Data] = (), on_empty: Callable[[], None] | None = None
    ) -> None:
        """Queue messages; `on_empty` runs before each timeout once they are exhausted."""
        self.inbox: deque[Data] = deque(messages)
        self.on_empty = on_empty
        self.sent: list[str] = []
        self.timeouts: list[float | None] = []
        self.close_code: int | None = None
        self.closed_with: tuple[int, str] | None = None
        self.entered = False
        self.exited = False
        self.remote_address = ("192.0.2.1", 443)

    def recv(self, timeout: float | None = None, decode: bool | None = None) -> Data:
        """Return the next queued message, or time out."""
        del decode
        self.timeouts.append(timeout)
        if not self.inbox:
            if self.on_empty is not None:
                self.on_empty()
            raise TimeoutError
        return self.inbox.popleft()

    def send(self, message: str, *, text: bool | None = None) -> None:
        """Record a sent message."""
        del text
        self.sent.append(message)

    def close(self, code: int = 1000, reason: str = "") -> None:
        """Record the close."""
        self.closed_with = (code, reason)
        self.close_code = code

    def ping(self, data: bytes | None = None, *, ack_on_close: bool = False) -> threading.Event:
        """Return an already-answered pong event."""
        del data, ack_on_close
        event = threading.Event()
        event.set()
        return event

    def __enter__(self) -> "FakeConnection":
        """Record entry."""
        self.entered = True
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Record exit."""
        self.exited = True


class FakeConnect:
    """Stands in for websockets' `connect`: fails as scripted, then connects."""

    def __init__(self, failures: Iterable[Exception] = ()) -> None:
        """Queue exceptions to raise before connecting."""
        self.failures = deque(failures)
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.connection = FakeConnection()

    def __call__(self, uri: str, /, **kwargs: object) -> Connection:
        """Record the call; raise the next queued failure or return the connection."""
        self.calls.append((uri, kwargs))
        if self.failures:
            raise self.failures.popleft()
        return cast(Connection, self.connection)


class RecordingWait:
    """A backoff wait that returns at once and records each delay."""

    def __init__(self) -> None:
        """Start with no waits."""
        self.delays: list[float] = []

    def __call__(self, seconds: float) -> bool:
        """Record the delay; report no interruption."""
        self.delays.append(seconds)
        return False


@pytest.fixture
def logs(monkeypatch: pytest.MonkeyPatch) -> list[EventDict]:
    """Capture the gate's log events, unredacted, so a leaked key is caught at the source."""
    capture = LogCapture()
    monkeypatch.setattr(
        conductor,
        "logger",
        structlog.wrap_logger(
            None,
            processors=[structlog.processors.format_exc_info, capture],
            wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG),
        ),
    )
    return capture.entries


@pytest.fixture
def restore_dbos_connect() -> Iterator[None]:
    """Put DBOS's own `connect` back after the test."""
    yield
    uninstall_gate()


def message(message_type: str, request_id: str = "req-1", **fields: object) -> str:
    """Serialize a Conductor request."""
    return json.dumps({"type": message_type, "request_id": request_id, **fields})


def gate(connection: FakeConnection) -> GatedConnection:
    """Wrap a fake connection in the gate."""
    return GatedConnection(cast(Connection, connection))


def types_of(command_class: CommandClass) -> list[str]:
    """Return the wire values of every message type in a class, sorted.

    Sorted because the classes are sets: pytest-xdist workers must collect the same
    parameters in the same order, and set order varies between processes.
    """
    return sorted(t.value for t, c in CLASSIFICATION.items() if c is command_class)


def test_every_dbos_message_type_is_classified() -> None:
    """A DBOS upgrade that adds a message type fails here before it can be dispatched."""
    assert set(CLASSIFICATION) == set(p.MessageType)


def test_retention_is_refused() -> None:
    """Retention stays with retention.py."""
    assert CLASSIFICATION[p.MessageType.RETENTION] is CommandClass.REFUSED


def test_import_is_refused() -> None:
    """An import's payload is a pickle, which the gate never lets DBOS decode."""
    assert CLASSIFICATION[p.MessageType.IMPORT_WORKFLOW] is CommandClass.REFUSED


@pytest.mark.parametrize("message_type", ["no_such_type", "", "CANCEL", 7, None])
def test_an_unknown_type_classifies_as_refused(message_type: object) -> None:
    """Anything outside the table, including a member's name or a non-string, is refused."""
    assert classify(message_type) is CommandClass.REFUSED


def test_a_wire_string_classifies_by_value() -> None:
    """The raw string from the wire finds its member, despite Enum hashing by name."""
    assert classify("cancel") is CommandClass.REFUSED
    assert classify("recovery") is CommandClass.REFUSED
    assert classify("list_workflows") is CommandClass.READ_ONLY


@pytest.mark.parametrize("message_type", types_of(CommandClass.READ_ONLY))
def test_a_read_only_message_reaches_the_reader_unchanged(message_type: str) -> None:
    """Read-only requests pass through to DBOS's dispatcher untouched."""
    raw = message(message_type, workflow_id="wf-1")
    connection = FakeConnection([raw])
    assert gate(connection).recv(timeout=5) == raw
    assert connection.sent == []


@pytest.mark.parametrize("message_type", [*types_of(CommandClass.REFUSED), "no_such_type"])
def test_a_refused_message_is_answered_and_withheld(
    message_type: str, logs: list[EventDict]
) -> None:
    """A refused or unknown request gets an error response and never reaches the reader."""
    read_only = message("list_workflows", "req-2")
    connection = FakeConnection([message(message_type, "req-1"), read_only])

    assert gate(connection).recv(timeout=5) == read_only

    [sent] = connection.sent
    response = p.BaseResponse.from_json(sent)
    assert response.type == message_type
    assert response.request_id == "req-1"
    assert response.error_message is not None
    assert message_type in response.error_message
    assert "refuses" in response.error_message
    assert [(e["event"], e["log_level"], e["message_type"], e["request_id"]) for e in logs] == [
        ("conductor.command_refused", "warning", message_type, "req-1")
    ]


def test_a_refused_message_alone_ends_in_a_timeout() -> None:
    """The gate keeps reading after answering, so the reader sees a timeout, not the message."""
    connection = FakeConnection([message("retention")])
    with pytest.raises(TimeoutError):
        gate(connection).recv(timeout=5)
    assert len(connection.sent) == 1


@pytest.mark.parametrize(
    "raw",
    [b"\x00binary", "not json", '["a", "list"]', json.dumps({"type": "list_workflows"})],
)
def test_a_malformed_message_is_dropped(raw: Data, logs: list[EventDict]) -> None:
    """Without a request ID there is nothing to answer, and nothing reaches the reader."""
    read_only = message("list_workflows")
    connection = FakeConnection([raw, read_only])
    assert gate(connection).recv(timeout=5) == read_only
    assert connection.sent == []
    assert [e["event"] for e in logs] == ["conductor.message_malformed"]


@pytest.mark.parametrize("timeout", [5.0, None])
def test_every_underlying_read_gets_the_caller_timeout(timeout: float | None) -> None:
    """A gated message answered in between does not shorten or lengthen the next read."""
    read_only = message("list_workflows")
    connection = FakeConnection([message("retention"), read_only])
    assert gate(connection).recv(timeout=timeout) == read_only
    assert connection.timeouts == [timeout, timeout]


def test_the_proxy_delegates_to_the_connection() -> None:
    """What DBOS's loop and keepalive call on the proxy reaches the wrapped connection."""
    connection = FakeConnection()
    proxy = cast(Connection, gate(connection))

    proxy.send("hello")
    assert proxy.ping().is_set()
    proxy.close(1001, "going away")

    assert connection.sent == ["hello"]
    assert connection.closed_with == (1001, "going away")
    assert proxy.close_code == 1001
    assert proxy.remote_address == ("192.0.2.1", 443)


def test_the_context_manager_yields_the_proxy() -> None:
    """`with connect(...) as websocket` must bind the gate, not the raw connection."""
    connection = FakeConnection()
    gated = gate(connection)
    with gated as entered:
        assert entered is gated
        assert connection.entered
    assert connection.exited


def gated_connect(
    original: FakeConnect,
    *,
    proxy_url: str | None = None,
    wait: Callable[[float], bool] | None = None,
    stop: threading.Event | None = None,
    backoff: Backoff | None = None,
) -> conductor.ConnectFunction[GatedConnection]:
    """Build a gated connect around a fake."""
    return make_gated_connect(
        original=original,
        key=KEY,
        proxy_url=proxy_url,
        stop=stop or threading.Event(),
        wait=wait or RecordingWait(),
        backoff=backoff,
    )


@pytest.mark.parametrize("proxy_url", [PROXY, None])
@pytest.mark.parametrize("environment_proxy", ["http://elsewhere.invalid:8080", None])
def test_only_the_configured_proxy_is_used(
    proxy_url: str | None, environment_proxy: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The setting is passed explicitly, as `None` when unset, so HTTPS_PROXY never applies."""
    for name in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY"):
        if environment_proxy is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, environment_proxy)
    original = FakeConnect()

    connection = gated_connect(original, proxy_url=proxy_url)(URL, open_timeout=5, proxy=True)

    [(uri, kwargs)] = original.calls
    assert uri == URL
    assert kwargs == {"open_timeout": 5, "proxy": proxy_url}
    assert isinstance(connection, GatedConnection)


def test_backoff_doubles_to_a_cap() -> None:
    """Waits before attempts after failures go 1, 2, 4 ... and stop growing at 300 s."""
    original = FakeConnect([OSError("refused")] * 11)
    wait = RecordingWait()
    connect = gated_connect(original, wait=wait)

    for _ in range(11):
        with pytest.raises(OSError, match="refused"):
            connect(URL)
    connect(URL)

    assert wait.delays == [1, 2, 4, 8, 16, 32, 64, 128, 256, 300, 300]
    assert len(original.calls) == 12


@pytest.mark.parametrize("failures", [1025, 10**6])
def test_backoff_delay_stays_at_the_cap_for_any_failure_count(failures: int) -> None:
    """The exponent is bounded before the power, so no failure count overflows a float."""
    assert Backoff(failures=failures).delay == MAX_BACKOFF_SECONDS


def test_backoff_delay_doubles_from_the_initial_wait() -> None:
    """Small failure counts give the plain doubling sequence from one second."""
    assert [Backoff(failures=n).delay for n in range(5)] == [0.0, 1.0, 2.0, 4.0, 8.0]


def test_backoff_resets_after_a_connection() -> None:
    """A successful connection means the next failure starts from one second again."""
    original = FakeConnect([OSError("refused")] * 3)
    wait = RecordingWait()
    connect = gated_connect(original, wait=wait)

    for _ in range(3):
        with pytest.raises(OSError, match="refused"):
            connect(URL)
    connect(URL)
    connect(URL)
    original.failures.append(OSError("refused"))
    with pytest.raises(OSError, match="refused"):
        connect(URL)
    connect(URL)

    assert wait.delays == [1, 2, 4, 1]


def test_setting_the_stop_event_cuts_the_wait_short() -> None:
    """Shutdown during a five-minute backoff wait ends at once, without connecting."""
    stop = threading.Event()
    original = FakeConnect()
    connect = make_gated_connect(
        original=original, key=KEY, proxy_url=None, stop=stop, backoff=Backoff(failures=9)
    )
    outcome: list[BaseException] = []

    def attempt() -> None:
        try:
            connect(URL)
        except BaseException as exc:
            outcome.append(exc)

    thread = threading.Thread(target=attempt)
    thread.start()
    stop.set()
    thread.join(timeout=5)

    assert not thread.is_alive()
    [exc] = outcome
    assert isinstance(exc, ConnectionClosedOK)
    assert original.calls == []


def rejection(status: int = 401) -> InvalidStatus:
    """Build a rejected handshake whose body echoes the key."""
    body = json.dumps({"error": f"invalid key {KEY}"}).encode()
    return InvalidStatus(Response(status, "Unauthorized", Headers(), body))


def test_a_rejected_key_is_logged_once_per_attempt_without_the_key(
    logs: list[EventDict],
) -> None:
    """Each rejected handshake is one `conductor.rejected` error with the status only."""
    failures = [rejection(), rejection(403)]
    original = FakeConnect(failures)
    connect = gated_connect(original)

    for failure in failures:
        with pytest.raises(InvalidStatus) as caught:
            connect(URL)
        assert caught.value is failure

    assert [(e["event"], e["log_level"], e["status"]) for e in logs] == [
        ("conductor.rejected", "error", 401),
        ("conductor.rejected", "error", 403),
    ]
    assert [e["retry_in_seconds"] for e in logs] == [1, 2]
    assert KEY not in repr(logs)
    assert URL not in repr(logs)


def test_a_connection_error_is_reraised_and_logged_without_the_key(
    logs: list[EventDict],
) -> None:
    """The error goes back to DBOS as raised; the gate's own event loses the key from it."""
    failures = [OSError(f"cannot reach {URL}"), ValueError(f"bad uri {quote(URL, safe='')}")]
    original = FakeConnect(failures)
    connect = gated_connect(original)

    for failure in failures:
        with pytest.raises(type(failure)) as caught:
            connect(URL)
        assert caught.value is failure

    assert [e["event"] for e in logs] == ["conductor.connect_failed"] * 2
    assert logs[0]["reason"].startswith("OSError: cannot reach wss://conductor.invalid/")
    assert KEY not in repr(logs)
    assert quote(KEY, safe="") not in repr(logs)


def run_dbos_loop(
    original: FakeConnect,
    dbos_logger: logging.Logger,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> list[str]:
    """Run DBOS's own Conductor loop through the gate until it connects; return its log lines.

    `original` fails as scripted, then connects to a connection whose first empty read
    sets the stop event, which ends the loop.
    """
    stop = threading.Event()
    original.connection = FakeConnection(on_empty=stop.set)
    monkeypatch.setattr(
        dbos_conductor,
        "connect",
        make_gated_connect(
            original=original, key=KEY, proxy_url=None, stop=stop, wait=RecordingWait()
        ),
    )
    # DBOS sleeps a second after each failed attempt; the gate's backoff is the real wait.
    monkeypatch.setattr(dbos_conductor, "time", SimpleNamespace(sleep=lambda seconds: None))
    caplog.set_level(logging.DEBUG, logger=dbos_logger.name)
    fake_dbos = SimpleNamespace(
        logger=dbos_logger, _conductor_metadata_only_mode=False, _conductor_executor_metadata={}
    )

    dbos_conductor.ConductorWebsocket(
        cast("DBOS", fake_dbos), "curate-d", "wss://conductor.invalid/v1", KEY, stop
    ).run()

    return [record.getMessage() for record in caplog.records]


def test_dbos_logs_failed_attempts_without_the_key(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Through DBOS's own loop, the key filter scrubs the errors DBOS logs on reconnect."""
    original = FakeConnect([OSError(f"cannot reach {URL}"), rejection()])
    dbos_logger = logging.getLogger("test.conductor.scrub")
    monkeypatch.setattr(dbos_logger, "filters", [KeyScrubFilter(KEY)])

    messages = run_dbos_loop(original, dbos_logger, monkeypatch, caplog)

    assert any("Unexpected exception in connection to conductor" in m for m in messages)
    assert any("Failed to connect to conductor" in m for m in messages)
    assert not [m for m in messages if KEY in m]
    assert len(original.calls) == 3


def undecodable_rejection() -> InvalidStatus:
    """Build a rejected handshake whose body is a Latin-1 error page, which is not UTF-8."""
    body = "<p>Accès refusé</p>".encode("latin-1")
    return InvalidStatus(Response(403, "Forbidden", Headers(), body))


def test_a_rejection_whose_body_is_not_utf8_is_reraised_with_a_decodable_body() -> None:
    """The copy keeps the status and replaces the bytes DBOS's strict decode would fail on."""
    failure = undecodable_rejection()

    with pytest.raises(InvalidStatus) as caught:
        gated_connect(FakeConnect([failure]))(URL)

    assert caught.value is not failure
    assert caught.value.response.status_code == 403
    assert caught.value.response.body.decode("utf-8") == "<p>Acc�s refus�</p>"


def test_dbos_survives_a_rejection_whose_body_is_not_utf8(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """DBOS's loop logs the rejection and reconnects instead of dying on the decode."""
    original = FakeConnect([undecodable_rejection()])

    messages = run_dbos_loop(
        original, logging.getLogger("test.conductor.undecodable"), monkeypatch, caplog
    )

    assert any("Failed to connect to conductor" in m and "�" in m for m in messages)
    assert len(original.calls) == 2


def test_a_connection_logs_no_key(logs: list[EventDict]) -> None:
    """The success event carries no URL or key."""
    gated_connect(FakeConnect(), proxy_url=PROXY)(URL)
    assert logs == [{"event": "conductor.connected", "log_level": "info", "proxied": True}]


def test_an_empty_key_is_rejected() -> None:
    """An empty key cannot be scrubbed from anything."""
    with pytest.raises(ValueError, match="empty"):
        make_gated_connect(original=FakeConnect(), key="", proxy_url=None, stop=threading.Event())


@pytest.mark.usefixtures("restore_dbos_connect")
def test_install_replaces_dbos_connect_idempotently(monkeypatch: pytest.MonkeyPatch) -> None:
    """Installing twice never wraps a wrapper; uninstalling restores DBOS's own connect."""

    def installed() -> object:
        return vars(dbos_conductor)["connect"]

    assert installed() is DBOS_CONNECT
    assert cast(object, DBOS_CONNECT) is websockets_client.connect
    stop = threading.Event()

    first = install_gate(key=KEY, proxy_url=None, stop=stop)
    assert installed() is first
    original = FakeConnect()
    monkeypatch.setattr(conductor, "DBOS_CONNECT", original)
    second = install_gate(key=KEY, proxy_url=PROXY, stop=stop)
    assert installed() is second

    # A wrapper around `first` would have overridden the proxy with `None`.
    second(URL)
    [(_, kwargs)] = original.calls
    assert kwargs["proxy"] == PROXY

    monkeypatch.undo()
    uninstall_gate()
    assert installed() is websockets_client.connect


def test_dbos_run_loop_sees_only_read_only_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pins the private seam: DBOS's own run loop through the gate, end to end."""
    stop = threading.Event()
    original = FakeConnect()
    original.connection = FakeConnection(
        [
            message("executor_info", "req-1"),
            message("retention", "req-2", gc_cutoff_epoch_ms=0),
            message("cancel", "req-3", workflow_id="wf-1"),
        ],
        on_empty=stop.set,
    )
    monkeypatch.setattr(
        dbos_conductor,
        "connect",
        make_gated_connect(original=original, key=KEY, proxy_url=PROXY, stop=stop),
    )
    fake_dbos = SimpleNamespace(
        logger=logging.getLogger("test.conductor"),
        _conductor_metadata_only_mode=False,
        _conductor_executor_metadata={"profile": "io"},
    )
    websocket = dbos_conductor.ConductorWebsocket(
        cast("DBOS", fake_dbos), "curate-d", "wss://conductor.invalid/v1", KEY, stop
    )

    websocket.run()

    [(uri, kwargs)] = original.calls
    assert uri == f"wss://conductor.invalid/v1/websocket/curate-d/{KEY}"
    assert kwargs["proxy"] == PROXY
    assert isinstance(cast(object, websocket.websocket), GatedConnection)
    responses = [p.BaseResponse.from_json(sent) for sent in original.connection.sent]
    assert [(r.type, r.request_id) for r in responses] == [
        ("executor_info", "req-1"),
        ("retention", "req-2"),
        ("cancel", "req-3"),
    ]
    executor_info, retention, cancel = responses
    assert executor_info.error_message is None
    assert retention.error_message is not None
    assert "refuses" in retention.error_message
    assert cancel.error_message is not None
    assert "read-only" in cancel.error_message
    assert original.connection.exited


def test_an_import_is_refused_without_decoding_its_payload(
    monkeypatch: pytest.MonkeyPatch, logs: list[EventDict]
) -> None:
    """Through DBOS's own run loop, an import is answered with an error and never unpickled."""
    payload = base64.b64encode(gzip.compress(pickle.dumps({"workflow_id": "wf-1"}))).decode()
    decoded: list[str] = []

    def forbidden(name: str) -> Callable[[object], object]:
        def decode(data: object, /) -> object:
            decoded.append(name)
            raise AssertionError(f"{name} called on an import payload")

        return decode

    monkeypatch.setattr(pickle, "loads", forbidden("pickle.loads"))
    monkeypatch.setattr(gzip, "decompress", forbidden("gzip.decompress"))
    monkeypatch.setattr(base64, "b64decode", forbidden("base64.b64decode"))
    stop = threading.Event()
    original = FakeConnect()
    original.connection = FakeConnection(
        [message("import_workflow", "req-7", serialized_workflow=payload)], on_empty=stop.set
    )
    monkeypatch.setattr(
        dbos_conductor,
        "connect",
        make_gated_connect(original=original, key=KEY, proxy_url=None, stop=stop),
    )
    fake_dbos = SimpleNamespace(
        logger=logging.getLogger("test.conductor"),
        _conductor_metadata_only_mode=False,
        _conductor_executor_metadata={},
    )

    dbos_conductor.ConductorWebsocket(
        cast("DBOS", fake_dbos), "curate-d", "wss://conductor.invalid/v1", KEY, stop
    ).run()

    assert decoded == []
    [sent] = original.connection.sent
    response = p.BaseResponse.from_json(sent)
    assert (response.type, response.request_id) == ("import_workflow", "req-7")
    assert response.error_message is not None
    assert "refuses Conductor command type 'import_workflow'" in response.error_message
    refused = [e for e in logs if e["event"] == "conductor.command_refused"]
    assert [(e["log_level"], e["message_type"], e["request_id"]) for e in refused] == [
        ("warning", "import_workflow", "req-7")
    ]


# Refusals.


FORMER_AUDITED: Final = (
    "cancel",
    "resume",
    "pause_schedule",
    "resume_schedule",
    "trigger_schedule",
)
"""The commands curate.d audited and ran before Conductor became read-only."""

UNSAFE_COMMANDS: Final = (
    "delete",
    "fork_workflow",
    "fork_from_failure",
    "rewind_workflow",
    "set_latest_application_version",
)

READ_ONLY_REASON: Final = "Conductor is read-only in curate.d"


def test_only_message_types_that_change_nothing_reach_dbos() -> None:
    """Every write is refused, those audited before included; the rest are read-only."""
    writes = {*FORMER_AUDITED, *UNSAFE_COMMANDS, "backfill_schedule", "retention"}
    writes |= {"import_workflow", "recovery"}
    assert {t.value for t in WRITES} == writes == set(types_of(CommandClass.REFUSED))
    assert {t.value for t in READ_ONLY} == set(types_of(CommandClass.READ_ONLY))
    assert "executor_info" in types_of(CommandClass.READ_ONLY)
    assert not READ_ONLY & WRITES
    assert set(REFUSALS) <= WRITES


@pytest.mark.parametrize(
    ("message_type", "reason"),
    [
        *[(t, None) for t in (*FORMER_AUDITED, *UNSAFE_COMMANDS)],
        (
            "backfill_schedule",
            "backfilling a schedule belongs to the deployment-time schedule tooling "
            "(issue #6), not to Conductor",
        ),
        ("retention", "retention.py is the only owner of workflow-history pruning"),
        ("import_workflow", "its payload is a pickle, which curate.d never decodes"),
        (
            "recovery",
            "curate.d recovers its executors itself: each restarted worker recovers its own "
            "rows at startup under its stable identity",
        ),
    ],
)
def test_a_refused_command_is_answered_with_why_curate_d_refuses_it(
    message_type: str, reason: str | None, logs: list[EventDict]
) -> None:
    """Conductor is told curate.d is read-only, and any further reason for the type."""
    raw = message(message_type, workflow_id="wf-1", schedule_name="nightly")
    connection = FakeConnection([raw])

    with pytest.raises(TimeoutError):
        gate(connection).recv(timeout=5)

    refused = f"curate.d refuses Conductor command type {message_type!r}: {READ_ONLY_REASON}"
    [sent] = connection.sent
    assert json.loads(sent) == {
        "type": message_type,
        "request_id": "req-1",
        "error_message": refused if reason is None else f"{refused}; {reason}",
    }
    assert [e["event"] for e in logs] == ["conductor.command_refused"]


class RecordingDBOS:
    """Stands in for DBOS in its own run loop, recording every other member it reaches."""

    def __init__(self) -> None:
        """Give the loop the members it reads on every connection."""
        self.logger = logging.getLogger("test.conductor")
        self._conductor_metadata_only_mode = False
        self._conductor_executor_metadata: dict[str, str] = {}
        self.reached: list[str] = []

    def __getattr__(self, name: str) -> object:
        """Record a member DBOS's dispatcher would act through, and fail it."""
        self.reached.append(name)
        raise AttributeError(name)


@pytest.mark.parametrize("message_type", sorted(t.value for t in WRITES))
def test_a_write_command_never_reaches_dbos(
    message_type: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through DBOS's own run loop a write is refused before its dispatcher touches DBOS."""
    stop = threading.Event()
    original = FakeConnect()
    original.connection = FakeConnection(
        [
            message(
                message_type,
                workflow_id="wf-1",
                workflow_ids=["wf-1"],
                schedule_name="nightly",
                executor_ids=["worker-io-1"],
            )
        ],
        on_empty=stop.set,
    )
    monkeypatch.setattr(
        dbos_conductor,
        "connect",
        make_gated_connect(original=original, key=KEY, proxy_url=None, stop=stop),
    )
    fake_dbos = RecordingDBOS()

    dbos_conductor.ConductorWebsocket(
        cast("DBOS", fake_dbos), "curate-d", "wss://conductor.invalid/v1", KEY, stop
    ).run()

    assert fake_dbos.reached == []
    [sent] = original.connection.sent
    response = p.BaseResponse.from_json(sent)
    assert (response.type, response.request_id) == (message_type, "req-1")
    assert response.error_message is not None
    assert READ_ONLY_REASON in response.error_message


def test_a_repeated_recovery_is_refused_with_a_warning_only_for_new_executors(
    logs: list[EventDict],
) -> None:
    """Each RECOVERY is refused; only one naming an executor not yet warned about warns."""
    connection = FakeConnection(
        [
            message("recovery", "req-1", executor_ids=["worker-io-1"]),
            message("recovery", "req-2", executor_ids=["worker-io-1"]),
            message("recovery", "req-3", executor_ids=["worker-io-1", "worker-io-2"]),
        ]
    )

    with pytest.raises(TimeoutError):
        gate(connection).recv(timeout=5)

    assert [json.loads(sent)["request_id"] for sent in connection.sent] == [
        "req-1",
        "req-2",
        "req-3",
    ]
    assert all("refuses" in json.loads(sent)["error_message"] for sent in connection.sent)
    assert [(e["log_level"], e["request_id"], e["executor_ids"]) for e in logs] == [
        ("warning", "req-1", ["worker-io-1"]),
        ("debug", "req-2", ["worker-io-1"]),
        ("warning", "req-3", ["worker-io-1", "worker-io-2"]),
    ]


def test_an_unknown_type_is_refused_as_read_only() -> None:
    """A type the gate does not know is refused for the read-only rule alone."""
    assert conductor.refusal("no_such_type") == (
        f"curate.d refuses Conductor command type 'no_such_type': {READ_ONLY_REASON}"
    )
    assert conductor.refusal(7) == f"curate.d refuses Conductor command type 7: {READ_ONLY_REASON}"


def conductor_settings(**fields: object) -> Settings:
    """Build settings from raw values, validated as they would be from the environment.

    The deployment environment defaults to dev, which Conductor needs for an app name.
    """
    return Settings.model_validate({"environment": "dev", **fields})


class FakeWebsocket:
    """Stands in for `ConductorWebsocket`: records its construction and start."""

    instances: ClassVar[list["FakeWebsocket"]] = []

    def __init__(
        self,
        dbos: object,
        app_name: str,
        conductor_url: str,
        conductor_key: str,
        evt: threading.Event,
    ) -> None:
        """Record the arguments, under `ConductorWebsocket.__init__`'s parameter names."""
        self.dbos = dbos
        self.app_name = app_name
        self.conductor_url = conductor_url
        self.conductor_key = conductor_key
        self.evt = evt
        self.started = False
        FakeWebsocket.instances.append(self)

    def start(self) -> None:
        """Record the start; open nothing."""
        self.started = True


@dataclass
class Boundary:
    """What `start_conductor` touched: the DBOS instance, threads built, gates installed."""

    dbos: SimpleNamespace
    websockets: list[FakeWebsocket]
    gates: list[dict[str, object]]


@pytest.fixture
def boundary(monkeypatch: pytest.MonkeyPatch) -> Iterator[Boundary]:
    """Substitute the DBOS instance and the websocket class; spy on the real `install_gate`."""
    dbos_logger = logging.getLogger("test.conductor.dbos")
    fake_dbos = SimpleNamespace(
        logger=dbos_logger, conductor_websocket=None, _conductor_metadata_only_mode=False
    )
    gates: list[dict[str, object]] = []

    def spy_install_gate(**kwargs: object) -> object:
        gates.append(kwargs)
        return install_gate(**kwargs)  # type: ignore[arg-type]

    FakeWebsocket.instances = []
    monkeypatch.setattr(conductor, "_get_dbos_instance", lambda: fake_dbos)
    monkeypatch.setattr(conductor, "ConductorWebsocket", FakeWebsocket)
    monkeypatch.setattr(conductor, "install_gate", spy_install_gate)
    yield Boundary(fake_dbos, FakeWebsocket.instances, gates)
    uninstall_gate()
    for existing in [f for f in dbos_logger.filters if isinstance(f, KeyScrubFilter)]:
        dbos_logger.removeFilter(existing)


def conductor_events(entries: list[EventDict]) -> list[str]:
    """Return the `conductor.*` events logged."""
    return [e["event"] for e in entries if str(e["event"]).startswith("conductor.")]


@pytest.mark.parametrize("profile", list(WorkerProfile))
@pytest.mark.parametrize(
    ("enabled", "key", "expected"),
    [
        (False, None, "conductor.disabled"),
        (False, KEY, "conductor.disabled"),
        (True, None, "conductor.misconfigured"),
        (True, "", "conductor.misconfigured"),
        (True, "  ", "conductor.misconfigured"),
        (True, KEY, "conductor.connecting"),
    ],
)
def test_each_outcome_is_logged_once_and_only_connect_starts_a_thread(
    profile: WorkerProfile,
    enabled: bool,
    key: str | None,
    expected: str,
    boundary: Boundary,
    logs: list[EventDict],
) -> None:
    """Flag off is disabled whatever the key; flag on needs the key; only then connect."""
    settings = conductor_settings(conductor_enabled=enabled, conductor_key=key)

    result = start_conductor(settings, profile)

    assert conductor_events(logs) == [expected]
    [entry] = logs
    assert entry["profile"] == profile.value
    connects = expected == "conductor.connecting"
    assert len(boundary.websockets) == len(boundary.gates) == int(connects)
    assert (result is not None) is connects
    assert boundary.dbos.conductor_websocket is result
    assert KEY not in repr(logs)


@pytest.mark.parametrize(
    ("environment", "name"),
    [("local", "curate-d-local"), ("dev", "curate-d-dev"), ("prod", "curate-d-prod")],
)
def test_each_environment_registers_its_own_conductor_app(
    environment: str, name: str, boundary: Boundary, logs: list[EventDict]
) -> None:
    """Dev and prod connect under different application names."""
    settings = conductor_settings(
        conductor_enabled=True, conductor_key=KEY, environment=environment
    )

    start_conductor(settings, WorkerProfile.IO)

    [websocket] = boundary.websockets
    assert websocket.app_name == name
    [entry] = logs
    assert entry["app_name"] == name


def test_an_unset_environment_is_an_error_naming_the_setting(
    boundary: Boundary, logs: list[EventDict]
) -> None:
    """Without an environment there is no app name, so nothing connects or is installed."""
    settings = conductor_settings(conductor_enabled=True, conductor_key=KEY, environment=None)

    result = start_conductor(settings, WorkerProfile.IO)

    assert result is None
    [entry] = logs
    assert entry["event"] == "conductor.misconfigured"
    assert entry["log_level"] == "error"
    assert entry["setting"] == "CURATE_D_ENVIRONMENT"
    assert not boundary.websockets
    assert vars(dbos_conductor)["connect"] is DBOS_CONNECT


def test_a_missing_key_is_an_error_naming_the_setting(
    boundary: Boundary, logs: list[EventDict]
) -> None:
    """The operator sees which setting to provide; nothing is raised or installed."""
    start_conductor(conductor_settings(conductor_enabled=True), WorkerProfile.CPU)

    [entry] = logs
    assert entry["log_level"] == "error"
    assert entry["setting"] == "CURATE_D_CONDUCTOR_KEY"
    assert vars(dbos_conductor)["connect"] is DBOS_CONNECT


@pytest.mark.parametrize("blank", ["", "  "])
def test_a_blank_key_that_escaped_validation_is_still_misconfigured(
    blank: str, boundary: Boundary, logs: list[EventDict]
) -> None:
    """A blank key built past `Settings` validation takes the same path, not `ValueError`."""
    settings = Settings.model_construct(conductor_enabled=True, conductor_key=SecretStr(blank))

    result = start_conductor(settings, WorkerProfile.IO)

    assert result is None
    assert conductor_events(logs) == ["conductor.misconfigured"]
    assert not boundary.websockets
    assert not boundary.gates
    assert boundary.dbos.conductor_websocket is None
    assert vars(dbos_conductor)["connect"] is DBOS_CONNECT


@pytest.mark.parametrize("profile", list(WorkerProfile))
def test_connect_hands_dbos_a_started_thread_sharing_the_gate_stop_event(
    profile: WorkerProfile, boundary: Boundary, logs: list[EventDict]
) -> None:
    """DBOS's destroy sets the thread's event, which must also cut a gate backoff short."""
    settings = conductor_settings(conductor_enabled=True, conductor_key=KEY)

    result = start_conductor(settings, profile)

    [websocket] = boundary.websockets
    [gate_kwargs] = boundary.gates
    assert cast(object, result) is websocket
    assert websocket.started
    assert boundary.dbos.conductor_websocket is websocket
    assert websocket.dbos is boundary.dbos
    assert websocket.app_name == "curate-d-dev"
    assert websocket.conductor_url == HOSTED_CONDUCTOR_URL
    assert websocket.conductor_key == KEY
    assert gate_kwargs == {"key": KEY, "proxy_url": None, "stop": websocket.evt}
    assert vars(dbos_conductor)["connect"] is not DBOS_CONNECT
    [entry] = logs
    assert entry["host"] == "cloud.dbos.dev"
    assert entry["proxied"] is False


def test_a_configured_url_is_used_and_only_its_host_logged(
    boundary: Boundary, logs: list[EventDict]
) -> None:
    """The log names the host; the path, which DBOS extends with the key, stays out."""
    url = "wss://conductor.example/conductor/v1alpha1"
    settings = conductor_settings(conductor_enabled=True, conductor_key=KEY, conductor_url=url)

    start_conductor(settings, WorkerProfile.IO)

    [websocket] = boundary.websockets
    assert websocket.conductor_url == url
    [entry] = logs
    assert entry["host"] == "conductor.example"
    assert "v1alpha1" not in repr(entry)


def test_the_proxy_setting_reaches_the_gate_in_a_form_websockets_accepts(
    boundary: Boundary, logs: list[EventDict]
) -> None:
    """Pydantic renders a host-only URL with a trailing slash; websockets' parser takes it."""
    settings = conductor_settings(
        conductor_enabled=True,
        conductor_key=KEY,
        conductor_proxy_url="http://egress-proxy:3128",
    )

    start_conductor(settings, WorkerProfile.CPU)

    [gate_kwargs] = boundary.gates
    proxy_url = cast(str, gate_kwargs["proxy_url"])
    assert proxy_url == "http://egress-proxy:3128/"
    assert parse_proxy(proxy_url) == Proxy("http", "egress-proxy", 3128, None, None)
    [entry] = logs
    assert entry["proxied"] is True


def test_dbos_debug_records_never_carry_the_key(
    boundary: Boundary, caplog: pytest.LogCaptureFixture
) -> None:
    """DBOS logs the keyed URL at debug when the thread is built; the filter scrubs it."""
    start_conductor(conductor_settings(conductor_enabled=True, conductor_key=KEY), WorkerProfile.IO)
    caplog.set_level(logging.DEBUG, logger=boundary.dbos.logger.name)
    encoded = quote(KEY, safe="")

    dbos_conductor.ConductorWebsocket(
        cast("DBOS", boundary.dbos), "curate-d", HOSTED_CONDUCTOR_URL, KEY, threading.Event()
    )
    # websockets, handed DBOS's logger, logs the handshake's request line like this.
    boundary.dbos.logger.debug("> GET /websocket/curate-d/%s HTTP/1.1", encoded)

    messages = [record.getMessage() for record in caplog.records]
    assert any("Connecting to conductor at" in m for m in messages)
    assert any("> GET /websocket/curate-d/" in m for m in messages)
    assert not [m for m in messages if KEY in m or encoded in m]


def test_starting_twice_keeps_one_key_filter(boundary: Boundary) -> None:
    """A second start replaces the filter rather than stacking another."""
    settings = conductor_settings(conductor_enabled=True, conductor_key=KEY)
    start_conductor(settings, WorkerProfile.IO)
    start_conductor(settings, WorkerProfile.IO)

    filters = boundary.dbos.logger.filters
    assert len([f for f in filters if isinstance(f, KeyScrubFilter)]) == 1


def test_the_key_filter_leaves_a_malformed_record_to_the_handler() -> None:
    """A record whose arguments do not format is passed on, not raised from the filter."""
    record = logging.LogRecord("dbos", logging.INFO, __file__, 1, "%d", ("x",), None)
    assert KeyScrubFilter(KEY).filter(record)
    assert record.msg == "%d"


def test_conductor_websocket_constructor_is_pinned() -> None:
    """Pins the private seam: `start_conductor` calls this constructor by keyword."""
    parameters = list(inspect.signature(dbos_conductor.ConductorWebsocket.__init__).parameters)
    assert parameters == ["self", "dbos", "app_name", "conductor_url", "conductor_key", "evt"]


def test_dbos_run_loop_connects_through_the_module_global() -> None:
    """Pins the private seam: `run()` resolves `connect` from its module at call time."""
    assert "connect" in vars(dbos_conductor)
    assert "with connect(" in inspect.getsource(dbos_conductor.ConductorWebsocket.run)


def test_dbos_destroy_stops_the_conductor_thread() -> None:
    """Pins the private seam: destroy sets the thread's event, closes its socket, joins it."""
    source = inspect.getsource(dbos_module.DBOS._destroy)
    assert "self.conductor_websocket.evt.set()" in source
    assert "self.conductor_websocket.websocket.close()" in source
    assert "self.conductor_websocket.join(" in source


def test_the_hosted_url_is_dbos_own_default() -> None:
    """Pins DBOS's default endpoint, which `start_conductor` uses when none is configured."""
    source = inspect.getsource(dbos_module)
    assert '"DBOS_DOMAIN", "cloud.dbos.dev"' in source
    assert 'f"wss://{dbos_domain}/conductor/v1alpha1"' in source
    assert HOSTED_CONDUCTOR_URL == "wss://cloud.dbos.dev/conductor/v1alpha1"


API_SOURCES = sorted((Path(conductor.__file__).resolve().parents[1] / "api").rglob("*.py"))


def test_the_api_never_starts_conductor() -> None:
    """The API is not a DBOS executor; no API module may reach `start_conductor`."""
    assert any(path.name == "__main__.py" for path in API_SOURCES)
    offenders = [
        f"{path.name}: {needle}"
        for path in API_SOURCES
        for needle in ("start_conductor", "runtime.conductor", "runtime import conductor")
        if needle in path.read_text(encoding="utf-8")
    ]
    assert not offenders, offenders
