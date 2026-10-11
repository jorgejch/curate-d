"""Worker entry point.

Deliberately re-exports nothing. A `main` attribute here would shadow the `main`
submodule, so `from curate_d.worker import main` would hand back the function instead of
the module — a trap that only shows up when something tries to patch it.

Run with `python -m curate_d.worker`.
"""
