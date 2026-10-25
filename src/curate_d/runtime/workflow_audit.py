"""Static checks: no raw database access from inside a workflow, no untraced enqueue.

A workflow that touches the database outside a datasource transaction is not tracked, so
the write re-executes on replay. That failure is invisible in review — the code looks
identical to the correct version — and only shows up as a duplicate row after a crash.
This walks the AST instead of trusting anyone to notice.

Deliberately syntactic. It cannot follow a connection through three helpers, and does not
try to: the rule it enforces is that the *call site* inside a workflow body is a
datasource transaction, which is exactly the property that can be checked here.

A second rule covers every module, inside a workflow or not: a `DBOS.enqueue_workflow_async`
or `DBOS.enqueue_workflow` call must sit in a `with` whose items include
`PropagateOtelContext()`. Without it the dequeued workflow starts a trace of its own, and
one file's ingest, classification and placement export as unrelated traces.

    uv run python -m curate_d.runtime.workflow_audit src/curate_d

Exits non-zero listing every raw in-workflow database call and every untraced enqueue.
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

# DBOS calls that put a workflow on a queue, and the context manager that carries the
# caller's trace onto it.
ENQUEUE_CALLS = frozenset({"enqueue_workflow_async", "enqueue_workflow"})
PROPAGATE_CONTEXT = "PropagateOtelContext"

RAW_CALL_REASON = (
    "directly. In-workflow ledger writes must go through a datasource transaction, or they "
    "re-execute on replay."
)
BARE_ENQUEUE_REASON = (
    f"outside {PROPAGATE_CONTEXT}. The dequeued workflow would start a trace of its own; "
    f"add {PROPAGATE_CONTEXT}() as the first item of the enclosing `with`."
)


@dataclass(frozen=True)
class Violation:
    """One call site breaking a rule: a raw in-workflow database call or a bare enqueue."""

    path: Path
    line: int
    function: str
    call: str
    reason: str = RAW_CALL_REASON

    def __str__(self) -> str:
        """Render a one-line, file:line-prefixed diagnostic for CLI output."""
        return f"{self.path}:{self.line}: {self.function} calls {self.call} {self.reason}"


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


def _name(node: ast.expr) -> str | None:
    """Return the trailing identifier of a name or attribute expression, else None."""
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


def _is_enqueue(node: ast.Call) -> bool:
    """Return whether `node` calls an enqueue method on `DBOS` or `<module>.DBOS`."""
    func = node.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr in ENQUEUE_CALLS
        and _name(func.value) == "DBOS"
    )


def _propagates(node: ast.With | ast.AsyncWith) -> bool:
    """Return whether one of the `with` items is a `PropagateOtelContext(...)` call."""
    return any(
        isinstance(item.context_expr, ast.Call)
        and _name(item.context_expr.func) == PROPAGATE_CONTEXT
        for item in node.items
    )


def _bare_enqueues(
    node: ast.AST, function: str = "<module>", propagated: bool = False
) -> Iterator[tuple[int, str, str]]:
    """Yield (line, enclosing function, call) for each enqueue outside `PropagateOtelContext`.

    A function boundary resets the context: a `with` around a `def` does not cover the body
    when it later runs.
    """
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        function, propagated = node.name, False
    elif isinstance(node, ast.Lambda):
        propagated = False
    if isinstance(node, ast.Call) and _is_enqueue(node) and not propagated:
        yield node.lineno, function, f"DBOS.{_name(node.func)}()"
    if isinstance(node, ast.With | ast.AsyncWith):
        for item in node.items:
            yield from _bare_enqueues(item, function, propagated)
        inner = propagated or _propagates(node)
        for statement in node.body:
            yield from _bare_enqueues(statement, function, inner)
        return
    for child in ast.iter_child_nodes(node):
        yield from _bare_enqueues(child, function, propagated)


def audit_source(source: str, path: Path) -> list[Violation]:
    """Return every raw in-workflow database call and every bare enqueue in one module."""
    tree = ast.parse(source, filename=str(path))
    violations: list[Violation] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) or not _is_workflow(node):
            continue
        violations.extend(
            Violation(path=path, line=line, function=node.name, call=call)
            for line, call in _raw_calls(node)
        )
    violations.extend(
        Violation(path=path, line=line, function=function, call=call, reason=BARE_ENQUEUE_REASON)
        for line, function, call in _bare_enqueues(tree)
    )
    return violations


def audit_tree(root: Path) -> list[Violation]:
    """Every violation under a source tree, ordered so output is stable."""
    violations: list[Violation] = []
    for path in sorted(root.rglob("*.py")):
        violations.extend(audit_source(path.read_text(encoding="utf-8"), path))
    return violations


def main(argv: list[str]) -> int:
    """Audit the tree named in `argv[1]` (default `src/curate_d`) against both rules.

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
