"""Every stack address in the integration suite can be pointed at another stack.

`make test-integration-parallel` runs each shard against its own stack, found through the
`CURATE_D_TEST_*` and `CURATE_D_LEDGER_DATABASE_URL` variables. A hardcoded loopback
address reaches the default stack instead, and that shard fails or, worse, writes into a
stack a developer is using. A literal address may therefore appear only as the default of
an `os.environ.get(...)`.
"""

import re
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
INTEGRATION_TESTS = REPOSITORY_ROOT / "tests" / "integration"

_ADDRESS = re.compile(r"\b(?:127\.0\.0\.1|localhost):\d+")
# `os.environ.get("NAME", "<default holding the address>")` on one line.
_ENVIRONMENT_DEFAULT = re.compile(
    r"""os\.environ\.get\(\s*(["'])[A-Z0-9_]+\1\s*,\s*(["'])[^"']*\2\s*\)"""
)


def hardcoded_addresses(line: str) -> list[str]:
    """Return the stack addresses on a line that are not an environment-variable default."""
    return _ADDRESS.findall(_ENVIRONMENT_DEFAULT.sub("", line))


def test_a_literal_address_is_hardcoded() -> None:
    """An address assigned directly is reported."""
    assert hardcoded_addresses('_ENDPOINT = "http://127.0.0.1:9100"') == ["127.0.0.1:9100"]
    assert hardcoded_addresses('url = "postgresql://u:p@localhost:5433/d"') == ["localhost:5433"]


def test_an_environment_default_is_not_hardcoded() -> None:
    """An address that only defaults an environment variable is allowed."""
    line = 'URL: Final = os.environ.get("CURATE_D_TEST_RCLONE_URL", "http://127.0.0.1:5572")'
    assert hardcoded_addresses(line) == []


def test_an_address_beside_an_environment_default_is_still_hardcoded() -> None:
    """An allowed default does not excuse a second, literal address on the same line."""
    line = 'a, b = os.environ.get("X", "http://127.0.0.1:1"), "http://localhost:2"'
    assert hardcoded_addresses(line) == ["localhost:2"]


@pytest.mark.parametrize("line", ['port = "9100"', "# the default stack publishes on loopback"])
def test_a_line_without_host_and_port_is_not_an_address(line: str) -> None:
    """A bare port or a mention of loopback is not a stack address."""
    assert hardcoded_addresses(line) == []


def test_the_integration_suite_reads_every_stack_address_from_the_environment() -> None:
    """No line under tests/integration/ hardcodes a loopback stack address."""
    offenders = [
        f"{path.relative_to(REPOSITORY_ROOT)}:{number}: {address}"
        for path in sorted(INTEGRATION_TESTS.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts
        for number, line in enumerate(
            path.read_bytes().decode("utf-8", errors="replace").splitlines(), start=1
        )
        for address in hardcoded_addresses(line)
    ]
    assert not offenders, offenders
