"""Native thread pools are pinned to one thread, before anything can load them."""

import ast
from pathlib import Path

from curate_d.runtime.threads import THREAD_VARIABLES, pin_library_threads

_WORKER_MAIN = Path(__file__).resolve().parents[3] / "src/curate_d/worker/__main__.py"


def test_every_thread_variable_is_set_to_one() -> None:
    """Each library's variable ends up at one."""
    environment: dict[str, str] = {}
    pin_library_threads(environment)
    assert environment == dict.fromkeys(THREAD_VARIABLES, "1")


def test_a_deployment_value_is_overridden() -> None:
    """A raised thread count is exactly the oversubscription being prevented."""
    environment = {"OMP_NUM_THREADS": "16", "UNRELATED": "x"}
    pin_library_threads(environment)
    assert environment["OMP_NUM_THREADS"] == "1"
    assert environment["UNRELATED"] == "x"


def test_the_worker_pins_threads_before_importing_the_application() -> None:
    """The pin call precedes the import that loads numpy and the decoders."""
    tree = ast.parse(_WORKER_MAIN.read_text(encoding="utf-8"))
    order = []
    for node in tree.body:
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
        ):
            order.append(node.value.func.id)
        elif isinstance(node, ast.ImportFrom) and node.module == "curate_d.worker.main":
            order.append("import worker.main")
    assert order == ["pin_library_threads", "import worker.main"]
