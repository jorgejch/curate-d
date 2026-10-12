"""The Alembic revision graph starts at the squashed baseline and stays one chain."""

import ast
import re
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import Script, ScriptDirectory
from alembic.util import to_tuple

from curate_d.domain.audit import AuditEntryType

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BASELINE = "0001_baseline"


@pytest.fixture(scope="module")
def scripts() -> ScriptDirectory:
    """Load the repository's migration scripts without touching a database."""
    config = Config(str(REPOSITORY_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPOSITORY_ROOT / "migrations"))
    return ScriptDirectory.from_config(config)


def _baseline(scripts: ScriptDirectory) -> Script:
    """Return the baseline revision's script."""
    script = scripts.get_revision(BASELINE)
    assert script is not None
    return script


def test_the_graph_has_one_base_and_one_head(scripts: ScriptDirectory) -> None:
    """The baseline is the only base, and the chain above it has not branched."""
    assert scripts.get_bases() == [BASELINE]
    assert len(scripts.get_heads()) == 1
    assert _baseline(scripts).down_revision is None


def test_every_down_revision_resolves(scripts: ScriptDirectory) -> None:
    """No revision points below the baseline at a retired one."""
    revisions = {script.revision for script in scripts.walk_revisions()}
    dangling = {
        script.revision: down
        for script in scripts.walk_revisions()
        for down in to_tuple(script.down_revision, default=())
        if down not in revisions
    }
    assert dangling == {}


def test_the_baseline_imports_nothing_from_the_application(scripts: ScriptDirectory) -> None:
    """The baseline is frozen: an edit to curate_d cannot change what it builds."""
    tree = ast.parse(Path(_baseline(scripts).path).read_text(encoding="utf-8"))
    imported = [
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    ]
    imported += [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
    assert not [name for name in imported if name.split(".")[0] == "curate_d"]


def test_the_baseline_allows_every_audit_entry_type(scripts: ScriptDirectory) -> None:
    """The baseline's entry_type CHECK lists exactly the AuditEntryType values."""
    allowed = set(re.findall(r"'([a-z_]+)'", _baseline(scripts).module._AUDIT_ENTRY_TYPES))

    assert allowed == {member.value for member in AuditEntryType}
