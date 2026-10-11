"""Executor identity.

Not a concurrency concern. Per-worker queue limits are enforced from each process's own
in-memory count of running workflows, so they need nothing from the deployment. Identity
matters for *recovery*, where three properties are required:

**Distinct**, because DBOS defaults the identity to the literal string ``local``. On
startup a process recovers PENDING workflows whose executor identity matches its own,
flipping them back to ENQUEUED. If every pod answers to ``local``, one pod restarting
matches rows owned by live peers and re-dispatches work that is currently running
elsewhere — losing exactly-once to a deployment detail rather than to anything in the
workflow code.

**Stable**, because startup recovery scans only the process's own identity and nothing
sweeps for identities that no longer exist. An identity that dies with its pod strands
that pod's in-flight workflows in PENDING with no owner that will ever claim them. This
is why there is no hostname fallback: under a Deployment the hostname is a random pod
name, which is distinct but never comes back.

**Exclusive**: at most one live process may use an identity at a time. Two live
processes sharing one is unsupported by DBOS and can strand queued workflows in PENDING
(dbos-transact-py #804). A StatefulSet gives each ordinal a stable name through the
downward API (``POD_NAME``) and replaces a pod only after the old one is gone; the
executor lease (lease.py) enforces the same at runtime for the cases the controller
cannot see, such as a forced pod deletion.

Only workers construct and launch DBOS. The API process is not an executor and needs no
identity; if it ever enqueues work it should do so through ``DBOSClient``, which runs
no recovery, rather than by launching DBOS.
"""

import os

EXECUTOR_ID_ENV = "CURATE_D_EXECUTOR_ID"
# Kubernetes convention: the downward API writes the pod name here.
POD_NAME_ENV = "POD_NAME"


class ExecutorIdentityError(RuntimeError):
    """No stable, distinct executor identity is configured."""


def resolve_executor_id() -> str:
    """Return the identity this process reports to DBOS, used to recover its own workflows.

    Explicit configuration wins, then the pod name. Nothing else is accepted: a hostname
    or DBOS's own default would be either shared or not stable (see the module docstring).

    Raises:
        ExecutorIdentityError: Neither environment variable holds a non-blank value.
    """
    for variable in (EXECUTOR_ID_ENV, POD_NAME_ENV):
        value = os.environ.get(variable, "").strip()
        if value:
            return value
    raise ExecutorIdentityError(
        f"set {EXECUTOR_ID_ENV} or {POD_NAME_ENV} (a StatefulSet pod name through the "
        f"downward API): without a stable identity, a restarted worker never recovers its "
        f"own in-flight workflows, and without a distinct one DBOS falls back to the shared "
        f"'local', so a restarting process recovers workflows that live peers are running"
    )
