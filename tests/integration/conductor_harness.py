"""A local stand-in for DBOS Conductor and for the CPU worker's egress proxy.

`FakeConductor` speaks Conductor's side of the wire protocol on 127.0.0.1: it asks every
executor that connects for `EXECUTOR_INFO`, as Conductor does, records every message it
receives, and lets a test send a request and wait for the response by `request_id`. It can
reject the handshake with HTTP 401, as Conductor does for a bad key.

`ConnectProxy` is the smallest HTTP proxy the gate can use: it answers `CONNECT host:port`
by opening that connection, replying `200 Connection established` and splicing the two
sockets. It records each `CONNECT` target, so a test can tell the proxy was used.

`launch_worker` brings a worker up the way `worker/main.py` does, minus the lease and
staging: services, DBOS under a stable executor ID and profile, then `start_conductor`.
`capture_gate_logs` records the gate's structured events, unredacted.
"""

import contextlib
import json
import logging
import socket
import threading
import time
from collections.abc import Callable
from http import HTTPStatus
from typing import LiteralString
from uuid import uuid4

import psycopg
import pytest
import structlog
from dbos import DBOS
from dbos._conductor.conductor import ConductorWebsocket
from sqlalchemy import make_url
from structlog.testing import LogCapture
from structlog.typing import EventDict
from websockets import ConnectionClosed
from websockets.http11 import Request, Response
from websockets.sync.server import Server, ServerConnection, serve

from curate_d.config import Settings
from curate_d.runtime import conductor, services
from curate_d.runtime.conductor import start_conductor, uninstall_gate
from curate_d.runtime.dbos_app import configure
from curate_d.runtime.profiles import WorkerProfile
from curate_d.runtime.queues import register_all
from curate_d.worker.main import prepare_services

type Message = dict[str, object]

RESPONSE_SECONDS = 15.0


class FakeConductor:
    """A Conductor endpoint on a random local port."""

    def __init__(self) -> None:
        """Start serving on 127.0.0.1 in a background thread."""
        self.reject = False
        """Answer every handshake with 401 while set."""
        self.handshakes: list[str] = []
        """The path of every handshake attempted, rejected ones included."""
        self.connections = 0
        """How many handshakes were accepted."""
        self.received: list[Message] = []
        self._responses: dict[str, Message] = {}
        self._connection: ServerConnection | None = None
        self._condition = threading.Condition()
        self._server: Server = serve(
            self._handle, "127.0.0.1", 0, process_request=self._process_request
        )
        self.port: int = self._server.socket.getsockname()[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        """Return the base URL an executor is configured with."""
        return f"ws://127.0.0.1:{self.port}/"

    def _process_request(self, connection: ServerConnection, request: Request) -> Response | None:
        """Record the handshake; reject it while `reject` is set."""
        with self._condition:
            self.handshakes.append(request.path)
            self._condition.notify_all()
        if self.reject:
            return connection.respond(HTTPStatus.UNAUTHORIZED, "invalid conductor key\n")
        return None

    def _handle(self, connection: ServerConnection) -> None:
        """Ask the executor who it is, then record everything it sends."""
        with self._condition:
            self._connection = connection
            self.connections += 1
            self._condition.notify_all()
        try:
            connection.send(json.dumps({"type": "executor_info", "request_id": f"info-{uuid4()}"}))
            for raw in connection:
                message: Message = json.loads(raw)
                with self._condition:
                    self.received.append(message)
                    self._responses[str(message["request_id"])] = message
                    self._condition.notify_all()
        except ConnectionClosed:
            pass
        finally:
            with self._condition:
                if self._connection is connection:
                    self._connection = None
                self._condition.notify_all()

    def wait_until[T](
        self, probe: Callable[[], T | None], *, timeout: float = RESPONSE_SECONDS
    ) -> T:
        """Return `probe()` once it is not None, re-checking on every server event.

        Raises:
            TimeoutError: `probe()` was still None after `timeout` seconds.
        """
        with self._condition:
            result = self._condition.wait_for(probe, timeout=timeout)
        if result is None:
            raise TimeoutError(f"fake Conductor: condition not met within {timeout:g} s")
        return result

    def executor_info(self, *, timeout: float = RESPONSE_SECONDS) -> Message:
        """Return the executor's answer to the `EXECUTOR_INFO` sent when it connected."""
        return self.wait_until(
            lambda: next((m for m in self.received if m["type"] == "executor_info"), None),
            timeout=timeout,
        )

    def send(
        self,
        message_type: str,
        *,
        request_id: str | None = None,
        timeout: float = RESPONSE_SECONDS,
        **fields: object,
    ) -> str:
        """Send a request to the connected executor and return its request ID.

        Args:
            message_type: The request's type.
            request_id: The ID to send it under, as Conductor reuses one when it redelivers;
                a fresh one when omitted.
            timeout: How long to wait for a connected executor.
            **fields: The request's body.

        Raises:
            TimeoutError: No executor connected within `timeout`.
        """
        request_id = request_id or f"req-{uuid4()}"
        with self._condition:
            self._responses.pop(request_id, None)
        connection = self.wait_until(lambda: self._connection, timeout=timeout)
        connection.send(json.dumps({"type": message_type, "request_id": request_id, **fields}))
        return request_id

    def response(self, request_id: str, *, timeout: float = RESPONSE_SECONDS) -> Message:
        """Wait for and return the response to a request.

        Raises:
            TimeoutError: No response arrived within `timeout`.
        """
        return self.wait_until(lambda: self._responses.get(request_id), timeout=timeout)

    def request(
        self,
        message_type: str,
        *,
        request_id: str | None = None,
        timeout: float = RESPONSE_SECONDS,
        **fields: object,
    ) -> Message:
        """Send a request to the connected executor and return its response.

        `request_id` redelivers a request sent before under it (see `send`).

        Raises:
            TimeoutError: No executor connected, or none answered, within `timeout`.
        """
        deadline = time.monotonic() + timeout
        request_id = self.send(message_type, request_id=request_id, timeout=timeout, **fields)
        return self.response(request_id, timeout=max(0.0, deadline - time.monotonic()))

    def close(self) -> None:
        """Stop serving and close any open connection."""
        self._server.shutdown()
        self._thread.join(timeout=10)


class ConnectProxy:
    """An HTTP `CONNECT` proxy on a random local port."""

    def __init__(self) -> None:
        """Start accepting on 127.0.0.1 in a background thread."""
        self.targets: list[str] = []
        """The `host:port` of every `CONNECT` received."""
        self._listener = socket.create_server(("127.0.0.1", 0))
        # Closing a socket does not wake a blocked accept() on every platform.
        self._listener.settimeout(0.1)
        self._stopped = threading.Event()
        self.port: int = self._listener.getsockname()[1]
        self._sockets: list[socket.socket] = []
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._accept, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        """Return the proxy setting an executor is configured with."""
        return f"http://127.0.0.1:{self.port}"

    def _track(self, sock: socket.socket) -> None:
        """Remember a socket so `stop()` can close it."""
        with self._lock:
            self._sockets.append(sock)

    def _accept(self) -> None:
        """Serve each client on its own thread until the listener is closed."""
        while not self._stopped.is_set():
            try:
                client, _ = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            client.settimeout(None)
            self._track(client)
            threading.Thread(target=self._tunnel, args=(client,), daemon=True).start()

    def _tunnel(self, client: socket.socket) -> None:
        """Read one `CONNECT`, open the target and splice both directions."""
        try:
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = client.recv(4096)
                if not chunk:
                    return
                head += chunk
            header, _, early = head.partition(b"\r\n\r\n")
            method, target, _ = header.split(b"\r\n", 1)[0].decode("latin-1").split(" ", 2)
            if method != "CONNECT":
                client.sendall(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
                return
            with self._lock:
                self.targets.append(target)
            host, _, port = target.rpartition(":")
            upstream = socket.create_connection((host, int(port)), timeout=5)
            upstream.settimeout(None)
            self._track(upstream)
            client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            if early:
                upstream.sendall(early)
            threading.Thread(target=self._pipe, args=(upstream, client), daemon=True).start()
            self._pipe(client, upstream)
        except OSError:
            client.close()

    @staticmethod
    def _pipe(source: socket.socket, sink: socket.socket) -> None:
        """Copy bytes from `source` to `sink` until either side closes."""
        try:
            while chunk := source.recv(65536):
                sink.sendall(chunk)
        except OSError:
            pass
        finally:
            for sock in (source, sink):
                with contextlib.suppress(OSError):
                    sock.shutdown(socket.SHUT_RDWR)

    def stop(self) -> None:
        """Stop accepting and close every tunnel; the port then refuses connections."""
        self._stopped.set()
        self._thread.join(timeout=5)
        self._listener.close()
        with self._lock:
            sockets, self._sockets = self._sockets, []
        for sock in sockets:
            sock.close()


def launch_worker(
    settings: Settings, profile: WorkerProfile, executor_id: str
) -> ConductorWebsocket | None:
    """Install services, launch DBOS as `executor_id` and start the Conductor connection.

    Registers every queue, as `worker/main.py` does.
    """
    DBOS.destroy()
    # Configured first: a destroyed instance still in DBOS's registry counts as launched,
    # so a datasource created before the new one exists is refused.
    configure(settings, executor_id=executor_id, profile=profile)
    prepare_services(settings, profile)
    DBOS.launch()
    register_all()
    return start_conductor(settings, profile)


def stop_worker() -> None:
    """Shut DBOS down, which stops the Conductor thread, and restore DBOS's `connect`."""
    DBOS.destroy()
    uninstall_gate()
    services.uninstall()


def capture_gate_logs(monkeypatch: pytest.MonkeyPatch) -> list[EventDict]:
    """Record the gate's log events for the rest of the test; return the live list."""
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


def wait_for_event(
    entries: list[EventDict], event: str, *, timeout: float = RESPONSE_SECONDS
) -> EventDict:
    """Return the first captured log entry named `event`, polling until it appears.

    Raises:
        TimeoutError: No such entry was logged within `timeout` seconds.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = next((entry for entry in list(entries) if entry["event"] == event), None)
        if found is not None:
            return found
        time.sleep(0.05)
    raise TimeoutError(f"{event!r} was not logged within {timeout:g} s")


def sql(
    settings: Settings, query: LiteralString, params: dict[str, object] | None = None
) -> list[tuple[object, ...]]:
    """Run one statement against the ledger database and return its rows."""
    conninfo = make_url(str(settings.ledger_database_url)).set(drivername="postgresql")
    with psycopg.connect(conninfo.render_as_string(hide_password=False), autocommit=True) as conn:
        cursor = conn.execute(query, params)
        return list(cursor.fetchall()) if cursor.description is not None else []


def workflow_status(settings: Settings, workflow_id: str) -> str | None:
    """Return a workflow's status row value, None when there is no row."""
    rows = sql(
        settings,
        "SELECT status FROM dbos.workflow_status WHERE workflow_uuid = %(id)s",
        {"id": workflow_id},
    )
    return str(rows[0][0]) if rows else None


def command_entry_types(settings: Settings, request_id: str) -> list[str]:
    """Return the type of every Conductor entry for a request, oldest first."""
    rows = sql(
        settings,
        "SELECT entry_type FROM audit_entries WHERE decided_by = 'conductor'"
        " AND details->>'request_id' = %(request_id)s ORDER BY id",
        {"request_id": request_id},
    )
    return [str(row[0]) for row in rows]
