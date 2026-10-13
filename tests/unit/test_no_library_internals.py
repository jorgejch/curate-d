"""The guard against DBOS's private internals in shipped code.

A private name has no compatibility promise: a DBOS upgrade can move or remove it without
notice, and the first sign is a worker crashing at import. Shipped code and migrations use
DBOS's public API only. Ruff's banned-api list names the private modules at import time;
this also catches an attribute reached through a public object.
"""

import re
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PRIVATE_REFERENCE = re.compile(r"dbos\._|_get_dbos_instance")


@pytest.mark.xfail(strict=True, reason="sections 2 and 3 of remove-dbos-internals still pending")
def test_no_shipped_code_reaches_into_dbos_internals() -> None:
    """Reject a `dbos._` module or `_get_dbos_instance` reference in src/ and migrations/."""
    offenders = [
        f"{path.relative_to(REPOSITORY_ROOT)}:{number}: {line.strip()}"
        for top in ("src", "migrations")
        for path in sorted((REPOSITORY_ROOT / top).rglob("*.py"))
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if PRIVATE_REFERENCE.search(line)
    ]
    assert not offenders, offenders
