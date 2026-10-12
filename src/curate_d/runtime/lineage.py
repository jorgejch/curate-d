"""Fork lineage checks shared by recovery and the operator's fork.

Both fork under a successor ID they derive, and let the DBOS primary key arbitrate a
repeat: a conflicting insert is accepted only when the row already there is a fork of the
same original. Anything else under that ID is unrelated work, and is never treated as the
successor.
"""

from sqlite3 import IntegrityError as SQLiteIntegrityError

from dbos import WorkflowStatus
from psycopg.errors import UniqueViolation
from sqlalchemy.exc import IntegrityError


class UnrelatedSuccessorError(ValueError):
    """The successor ID is taken by a workflow that is not a fork of the original."""


def is_workflow_id_conflict(error: IntegrityError) -> bool:
    """Return whether `error` is the workflow primary-key conflict, and no other constraint."""
    cause = error.orig
    if isinstance(cause, UniqueViolation):
        return cause.diag.constraint_name == "workflow_status_pkey"
    return isinstance(cause, SQLiteIntegrityError) and str(cause) == (
        "UNIQUE constraint failed: workflow_status.workflow_uuid"
    )


def confirmed_successor(status: WorkflowStatus | None, original: str, successor: str) -> bool:
    """Return whether `status`, read under `successor`, is a fork of `original`.

    Raises:
        UnrelatedSuccessorError: a workflow exists under `successor` but was not forked
            from `original`.
    """
    if status is None:
        return False
    if status.forked_from != original:
        raise UnrelatedSuccessorError(
            f"workflow ID {successor!r} belongs to an unrelated workflow, not a fork of "
            f"{original!r}"
        )
    return True
