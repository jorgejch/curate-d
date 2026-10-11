"""Every DBOS-registered function has a name no other registered function shares.

DBOS registers steps and workflows by name. Two modules each defining a `_now_step` both
register under one name, and a workflow recovered after a crash can then resolve the
other module's function. DBOS only warns about it, so the check is made here.
"""

import ast
from collections import Counter
from pathlib import Path

_WORKFLOWS = Path(__file__).resolve().parents[3] / "src/curate_d/workflows"


def _registered(tree: ast.Module) -> list[str]:
    """Names of functions decorated with a DBOS step or workflow decorator."""
    names = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for decorator in node.decorator_list:
            target = decorator.func if isinstance(decorator, ast.Call) else decorator
            if isinstance(target, ast.Attribute) and target.attr in {"step", "workflow"}:
                names.append(node.name)
    return names


def test_no_two_registered_functions_share_a_name() -> None:
    """Across every workflow module, each registered name appears once."""
    counts = Counter(
        name
        for path in sorted(_WORKFLOWS.glob("*.py"))
        for name in _registered(ast.parse(path.read_text(encoding="utf-8")))
    )
    assert counts, "no registered functions found; the scan is looking in the wrong place"
    assert [name for name, count in counts.items() if count > 1] == []
