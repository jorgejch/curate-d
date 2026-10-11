"""Static check: no raw database access from inside a workflow.

A workflow that touches the database outside a datasource transaction is not tracked, so
the write re-executes on replay. That failure is invisible in review — the code looks
identical to the correct version — and only shows up as a duplicate row after a crash.
This walks the AST instead of trusting anyone to notice.

Deliberately syntactic. It cannot follow a connection through three helpers, and does not
try to: the rule it enforces is that the *call site* inside a workflow body is a
datasource transaction, which is exactly the property that can be checked here.

    uv run python -m curate_d.runtime.workflow_audit src/curate_d

Exits non-zero listing every raw in-workflow database call.
"""

import ast
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

# Attribute or function names that mean "talk to the database now".
FORBIDDEN_CALLS = frozenset(
    {
        "execute",
        "exec_driver_sql",
        "scalar",
        "scalars",
        "stream",
        "connect",
        "begin",
        "run_sync",
    }
)

# Receivers that make such a call a raw one. A session obtained from a datasource inside
# a transaction is fine; an engine or a connection the workflow opened itself is not.
RAW_RECEIVERS = frozenset({"engine", "_engine", "connection", "_connection", "conn"})

WORKFLOW_DECORATORS = frozenset({"workflow"})


@dataclass(frozen=True)
class Violation:
    """One raw database call reached from a workflow body."""

    path: Path
    line: int
    function: str
    call: str

    def __str__(self) -> str:
        """Render a one-line, file:line-prefixed diagnostic for CLI output."""
        return (
            f"{self.path}:{self.line}: {self.function} calls {self.call} directly. "
            f"In-workflow ledger writes must go through a datasource transaction, or they "
            f"re-execute on replay."
        )


def _is_workflow(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Whether a function carries @DBOS.workflow() or an alias of it."""
    for decorator in node.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if isinstance(target, ast.Attribute) and target.attr in WORKFLOW_DECORATORS:
            return True
        if isinstance(target, ast.Name) and target.id in WORKFLOW_DECORATORS:
            return True
    return False


def _raw_calls(body: ast.AST) -> Iterator[tuple[int, str]]:
    """Yield (line, call description) for each forbidden call on a raw receiver in `body`."""
    for node in ast.walk(body):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr not in FORBIDDEN_CALLS:
            continue
        receiver = func.value
        name = (
            receiver.attr
            if isinstance(receiver, ast.Attribute)
            else receiver.id
            if isinstance(receiver, ast.Name)
            else None
        )
        if name in RAW_RECEIVERS:
            yield node.lineno, f"{name}.{func.attr}()"


def audit_source(source: str, path: Path) -> list[Violation]:
    """Every raw database call appearing in a workflow body in one module."""
    tree = ast.parse(source, filename=str(path))
    violations: list[Violation] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) or not _is_workflow(node):
            continue
        violations.extend(
            Violation(path=path, line=line, function=node.name, call=call)
            for line, call in _raw_calls(node)
        )
    return violations


def audit_tree(root: Path) -> list[Violation]:
    """Every violation under a source tree, ordered so output is stable."""
    violations: list[Violation] = []
    for path in sorted(root.rglob("*.py")):
        violations.extend(audit_source(path.read_text(encoding="utf-8"), path))
    return violations


def main(argv: list[str]) -> int:
    """Audit the tree named in `argv[1]` (default `src/curate_d`) for raw in-workflow calls.

    Args:
        argv: Command-line arguments; `argv[1]`, if present, is the root to scan.

    Returns:
        1 if any violation was found, 0 otherwise.
    """
    root = Path(argv[1]) if len(argv) > 1 else Path("src/curate_d")
    violations = audit_tree(root)
    for violation in violations:
        sys.stderr.write(f"{violation}\n")
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
