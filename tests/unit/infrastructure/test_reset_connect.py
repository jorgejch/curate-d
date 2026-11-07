"""The environment reset's ledger connection."""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest
from psycopg.conninfo import conninfo_to_dict

from curate_d.config import Settings

_RESET = Path(__file__).resolve().parents[3] / "ops" / "reset" / "reset.py"


def _load_reset() -> ModuleType:
    """Load ops/reset/reset.py, which is a script and not an importable package."""
    spec = importlib.util.spec_from_file_location("curate_d_ops_reset", _RESET)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_reset_connects_with_the_exact_url_parameters(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reset gives libpq each parameter unchanged, a space in `options` included."""
    monkeypatch.setenv(
        "CURATE_D_LEDGER_DATABASE_URL",
        "postgresql+psycopg://u:p@db.example:5432/ledger?options=-c%20statement_timeout%3D5000",
    )
    reset = _load_reset()
    received: list[str] = []
    monkeypatch.setattr(reset.psycopg, "connect", lambda conninfo, **_: received.append(conninfo))
    reset.connect(Settings(_env_file=None))
    assert conninfo_to_dict(received[0])["options"] == "-c statement_timeout=5000"
