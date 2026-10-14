"""Isolation guards for the unit suite.

Two ways a unit test can quietly stop being one, both closed here.

A test that opens a socket becomes a flaky test, so outbound connections fail loudly
now rather than at 3am in CI. Local socket pairs stay open because asyncio builds its
event loop out of them. libpq opens its sockets in C, past that guard, so a workflow
system database client built by `dbos_app.client` refuses every query on its own: a
test that needs one substitutes it.

A test that reads the developer's `.env` becomes a test whose result depends on whose
machine it runs on. `Settings` loads that file by design, so a developer who exports a
credential key locally silently flips the meaning of every assertion about unset
configuration. The suite is pinned to the environment alone.
"""

import socket
from collections.abc import Iterator
from typing import NoReturn

import pytest
from dbos import DBOSClient

from curate_d.config import Settings
from curate_d.runtime import dbos_app

_NETWORK_FAMILIES = frozenset({socket.AF_INET, socket.AF_INET6})

type SocketAddress = str | bytes | tuple[str, int] | tuple[str, int, int, int]


class NetworkAccessInUnitTestError(RuntimeError):
    """Raised when a unit test attempts an outbound network connection."""


def _refuse(call: str, address: SocketAddress) -> NetworkAccessInUnitTestError:
    """Build the error raised when a unit test attempts outbound socket access."""
    return NetworkAccessInUnitTestError(
        f"socket.{call}({address!r}) blocked: unit tests must substitute collaborators "
        f"at the boundary. Move this test to tests/integration."
    )


@pytest.fixture(autouse=True)
def _forbid_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Patch socket connect methods to reject outbound AF_INET/AF_INET6 connections.

    Raises:
        NetworkAccessInUnitTestError: a test attempts an outbound network connection.
    """
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def connect(self: socket.socket, address: SocketAddress) -> None:
        """Raise for a network-family connect; delegate everything else (e.g. Unix sockets)."""
        if self.family in _NETWORK_FAMILIES:
            raise _refuse("connect", address)
        real_connect(self, address)

    def connect_ex(self: socket.socket, address: SocketAddress) -> int:
        """Raise for a network-family connect_ex; delegate everything else."""
        if self.family in _NETWORK_FAMILIES:
            raise _refuse("connect_ex", address)
        return real_connect_ex(self, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    yield


class _UnsubstitutedWorkflowClient:
    """A real, lazy `dbos_app.client` that refuses every use but its teardown.

    Building the client needs no database, and production code relies on that, so a test
    may build it; anything it then asks of the database is refused here, before libpq
    connects from C.
    """

    def __init__(self, real: DBOSClient) -> None:
        self._real = real

    def destroy(self) -> None:
        """Dispose of the real client's unconnected pool."""
        self._real.destroy()

    def __getattr__(self, name: str) -> NoReturn:
        """Raise for any use of the client other than `destroy`.

        Raises:
            NetworkAccessInUnitTestError: always.
        """
        raise NetworkAccessInUnitTestError(
            f"dbos_app.client(...).{name} blocked: libpq connects in C, past the socket "
            f"guard. Patch dbos_app.client with a fake in the test, or move it to "
            f"tests/integration."
        )


@pytest.fixture(autouse=True)
def _forbid_workflow_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every client `dbos_app.client` builds refuse to reach the database.

    A test or fixture that patches `dbos_app.client` itself overrides this one: requested
    fixtures and the test body run after autouse fixtures.
    """
    real_client = dbos_app.client

    def unsubstituted(settings: Settings, **kwargs: bool) -> _UnsubstitutedWorkflowClient:
        """Build the real lazy client and wrap it so that any query raises."""
        return _UnsubstitutedWorkflowClient(real_client(settings, **kwargs))

    monkeypatch.setattr(dbos_app, "client", unsubstituted)


@pytest.fixture(autouse=True)
def _ignore_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    """Detach Settings from the local .env for the duration of the unit suite.

    Without this, `monkeypatch.delenv` cannot express "this value is unset": the file
    supplies it again behind the test's back, and an assertion that unset configuration
    is refused passes or fails according to the developer's own machine.
    """
    monkeypatch.setitem(Settings.model_config, "env_file", None)
