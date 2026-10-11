"""Isolation guards for the unit suite.

Two ways a unit test can quietly stop being one, both closed here.

A test that opens a socket becomes a flaky test, so outbound connections fail loudly
now rather than at 3am in CI. Local socket pairs stay open because asyncio builds its
event loop out of them.

A test that reads the developer's `.env` becomes a test whose result depends on whose
machine it runs on. `Settings` loads that file by design, so a developer who exports a
credential key locally silently flips the meaning of every assertion about unset
configuration. The suite is pinned to the environment alone.
"""

import socket
from collections.abc import Iterator

import pytest

from curate_d.config import Settings

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


@pytest.fixture(autouse=True)
def _ignore_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    """Detach Settings from the local .env for the duration of the unit suite.

    Without this, `monkeypatch.delenv` cannot express "this value is unset": the file
    supplies it again behind the test's back, and an assertion that unset configuration
    is refused passes or fails according to the developer's own machine.
    """
    monkeypatch.setitem(Settings.model_config, "env_file", None)
