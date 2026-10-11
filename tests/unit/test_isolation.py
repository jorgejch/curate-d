"""The unit suite's own guard rails."""

import socket
from pathlib import Path

import pytest

from tests.unit.conftest import NetworkAccessInUnitTestError


def test_outbound_tcp_connection_is_blocked() -> None:
    """An outbound AF_INET connect attempt raises NetworkAccessInUnitTestError."""
    with (
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock,
        pytest.raises(NetworkAccessInUnitTestError),
    ):
        sock.connect(("example.invalid", 80))


def test_local_socket_pair_still_works() -> None:
    """asyncio builds its event loop from a socket pair; blocking it breaks the suite."""
    left, right = socket.socketpair()
    with left, right:
        left.send(b"ping")
        assert right.recv(4) == b"ping"


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
# Sibling repositories this project deploys through. Spelled in pieces so this file does
# not trip its own check.
SIBLING_REPOSITORIES = ("flux" + "-ovh", "ovh-infra" + "-terraform")


def test_no_code_or_test_reaches_into_a_sibling_repository() -> None:
    """Reject sibling-repository references in Python files and Make targets."""
    paths = [REPOSITORY_ROOT / "Makefile"]
    paths.extend(
        path
        for top in ("src", "tests", "migrations")
        for path in sorted((REPOSITORY_ROOT / top).rglob("*.py"))
    )
    offenders = [
        f"{path.relative_to(REPOSITORY_ROOT)}: {name}"
        for path in paths
        for name in SIBLING_REPOSITORIES
        if name in path.read_text(encoding="utf-8")
    ]
    assert not offenders, offenders
