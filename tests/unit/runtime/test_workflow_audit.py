"""The guard against untracked in-workflow database access.

Both halves matter: it must catch the real mistake, and it must not fire on the correct
form, or it gets suppressed and stops protecting anything.
"""

from pathlib import Path
from textwrap import dedent

import pytest

import curate_d
from curate_d.runtime.workflow_audit import audit_source, audit_tree, main

_PATH = Path("example.py")


def _audit(source: str) -> list[str]:
    """Dedent `source` and return the flagged call sites' text."""
    return [violation.call for violation in audit_source(dedent(source), _PATH)]


def test_a_raw_connection_call_inside_a_workflow_is_caught() -> None:
    """Flag a direct connection.execute() call inside a workflow."""
    assert _audit(
        """
        @DBOS.workflow()
        async def place(self) -> None:
            await self._connection.execute(statement)
        """
    ) == ["_connection.execute()"]


def test_an_engine_call_inside_a_workflow_is_caught() -> None:
    """Flag both engine.begin() and the connection.execute() call it opens."""
    assert _audit(
        """
        @DBOS.workflow()
        async def place() -> None:
            async with engine.begin() as conn:
                await conn.execute(statement)
        """
    ) == ["engine.begin()", "conn.execute()"]


def test_a_datasource_transaction_is_not_flagged() -> None:
    """The correct form must stay quiet, or the check gets turned off."""
    assert (
        _audit(
            """
            @datasource.transaction()
            async def record(placement):
                await datasource.sql_session().execute(statement)

            @DBOS.workflow()
            async def place(placement) -> None:
                await record(placement)
            """
        )
        == []
    )


def test_a_raw_call_outside_any_workflow_is_not_flagged() -> None:
    """Repositories are called from datasource transactions; that is the whole design."""
    assert (
        _audit(
            """
            async def upsert(self, source):
                return await self._connection.execute(statement)
            """
        )
        == []
    )


def test_a_synchronous_workflow_is_checked_too() -> None:
    """Flag a raw connection call inside a non-async workflow."""
    assert _audit(
        """
        @DBOS.workflow()
        def sweep() -> None:
            connection.execute(statement)
        """
    ) == ["connection.execute()"]


def test_a_bare_decorator_without_a_call_is_recognised() -> None:
    """Recognise `@workflow` without parentheses as marking a workflow."""
    assert _audit(
        """
        @workflow
        def sweep() -> None:
            engine.connect()
        """
    ) == ["engine.connect()"]


def test_the_violation_message_explains_the_consequence() -> None:
    """A message that only says "forbidden" gets worked around."""
    violation = audit_source(
        dedent(
            """
            @DBOS.workflow()
            async def place() -> None:
                await engine.execute(statement)
            """
        ),
        _PATH,
    )[0]
    assert "re-execute on replay" in str(violation)
    assert "datasource transaction" in str(violation)


def test_the_application_source_tree_is_clean() -> None:
    """The check that actually protects the codebase."""
    root = Path(curate_d.__file__).parent
    violations = audit_tree(root)
    assert violations == [], "\n".join(str(violation) for violation in violations)


def test_the_cli_exits_nonzero_and_reports_a_violation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Exit 1 and name the offending call when a tree holds a raw in-workflow call."""
    (tmp_path / "flows.py").write_text(
        dedent(
            """
            @DBOS.workflow()
            async def place() -> None:
                await engine.execute(statement)
            """
        ),
        encoding="utf-8",
    )
    assert main(["workflow_audit", str(tmp_path)]) == 1
    assert "engine.execute()" in capsys.readouterr().err


def test_the_cli_exits_zero_on_a_clean_tree(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Exit 0 and print nothing when no workflow touches the database directly."""
    (tmp_path / "flows.py").write_text(
        dedent(
            """
            @DBOS.workflow()
            async def place(placement) -> None:
                await record(placement)
            """
        ),
        encoding="utf-8",
    )
    assert main(["workflow_audit", str(tmp_path)]) == 0
    assert capsys.readouterr().err == ""
