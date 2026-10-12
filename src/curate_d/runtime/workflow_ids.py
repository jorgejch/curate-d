"""Deterministic workflow identifiers.

DBOS treats the workflow identifier as an idempotency key: two invocations with the same
identifier execute once. Deriving it from what the work *is* — the content fingerprint and
the destination — rather than from when it was scheduled turns that into the whole
deduplication mechanism.

This is what makes reconciliation cheap. The reconciler can enqueue every placement it
believes is missing on every pass, without first checking whether that work is already
running and without a deduplication table, because redundant enqueues collapse. Two
overlapping reconciliation passes therefore cannot produce two transfers of the same file.
"""

import hashlib

from curate_d.domain import ContentObjectId, StrongFingerprint

# Namespaced so a placement identifier can never collide with a scan or classification
# one, and readable in the workflow tables so an operator can tell what a row is.
PLACEMENT_PREFIX = "placement"
CLASSIFICATION_PREFIX = "classify"
SCAN_PREFIX = "scan"
INGEST_PREFIX = "ingest"
FULL_RESCAN_PREFIX = "rescan"
DRY_RUN_PREFIX = "dry-run"
RECONCILE_PREFIX = "reconcile"
DESTINATION_CHECK_PREFIX = "check"
RECOVER_EXECUTOR_PREFIX = "recover-executor"
RECOVER_ALL_PREFIX = "recover-all"
OPERATOR_FORK_PREFIX = "operator-fork"
OPERATOR_TRIGGER_PREFIX = "operator-trigger"

_SEPARATOR = ":"
_LOCATOR_TAG_DIGITS = 12


def _join(*parts: str) -> str:
    """Join key parts with the separator, rejecting any part that contains it.

    Raises:
        ValueError: a part contains the separator, which would let two distinct key
            tuples collide on the same joined identifier.
    """
    for part in parts:
        if _SEPARATOR in part:
            raise ValueError(
                f"{part!r} contains {_SEPARATOR!r}; two different work items could then "
                f"derive the same workflow identifier and one would be silently dropped"
            )
    return _SEPARATOR.join(parts)


def placement_attempt_workflow_id(
    fingerprint: StrongFingerprint, destination_name: str, locator: str, attempt: int
) -> str:
    """Return the identity of one attempt at putting these bytes at this path of this destination.

    The locator is part of it, hashed to keep the identifier short, so that content
    re-rendered to a new path is new work rather than a repeat of the old placement;
    the attempt is, so that a failed placement can be tried again.
    """
    base = placement_workflow_id(fingerprint, destination_name)
    tag = hashlib.sha256(locator.encode()).hexdigest()[:_LOCATOR_TAG_DIGITS]
    return f"{base}@{tag}" if attempt == 0 else f"{base}@{tag}#{attempt}"


def placement_workflow_id(fingerprint: StrongFingerprint, destination_name: str) -> str:
    """Return the identity of "put these bytes at this destination".

    Keyed on the fingerprint rather than the placement row so that the identifier exists
    before any row does, which is what lets the reconciler enqueue optimistically.
    """
    return _join(
        PLACEMENT_PREFIX, fingerprint.algorithm.value, fingerprint.digest, destination_name
    )


def classification_workflow_id(fingerprint: StrongFingerprint) -> str:
    """Return the identity of "work out what these bytes are". Runs once per distinct file."""
    return _join(CLASSIFICATION_PREFIX, fingerprint.algorithm.value, fingerprint.digest)


def scan_workflow_id(provider: str, account: str, subtree: str, cycle: str) -> str:
    """Return the identity of one scan of one subtree.

    Carries a cycle marker because, unlike a placement, a scan is meant to happen again:
    without it the second scan of a subtree would collapse into the first and never run.
    """
    return _join(SCAN_PREFIX, provider, account, subtree, cycle)


def ingest_workflow_id(source_object_id: str, observed: str) -> str:
    """Return the identity of "fetch and fingerprint this file as it now is".

    `observed` is what the scan saw — the version marker, or size and time where there is
    none — so a file enqueued twice for one version ingests once, and a later version of
    the same file ingests again.
    """
    return _join(INGEST_PREFIX, source_object_id, observed)


def full_rescan_workflow_id(provider: str, account: str, cycle: str) -> str:
    """Return the identity of one full rescan pass."""
    return _join(FULL_RESCAN_PREFIX, provider, account, cycle)


def content_placement_workflow_id(content_object_id: ContentObjectId, destination_name: str) -> str:
    """Return a placement identity for callers holding a content object, not a fingerprint."""
    return _join(PLACEMENT_PREFIX, "id", str(content_object_id), destination_name)


def dry_run_workflow_id(run_id: str) -> str:
    """Return one dry run's identity; the caller chooses the run id once, so a retry collapses."""
    return _join(DRY_RUN_PREFIX, run_id)


def reconcile_workflow_id(run: str, part: int) -> str:
    """Return the identity of one part of one reconciliation run; a run hands over part by part."""
    return _join(RECONCILE_PREFIX, run, str(part))


def destination_check_workflow_id(destination_name: str, cycle: str) -> str:
    """Return the identity of one destination's check in one checking cycle."""
    return _join(DESTINATION_CHECK_PREFIX, destination_name, cycle)


def recover_executor_workflow_id(executor_id: str, request_id: str) -> str:
    """Return the identity of one operator request to recover one executor's work."""
    return _join(RECOVER_EXECUTOR_PREFIX, executor_id, request_id)


def recover_all_workflow_id(request_id: str) -> str:
    """Return the identity of one operator request to recover every executor's work."""
    return _join(RECOVER_ALL_PREFIX, request_id)


def operator_fork_workflow_id(request_id: str) -> str:
    """Return the successor identity of one operator fork; a repeated request collapses."""
    return _join(OPERATOR_FORK_PREFIX, request_id)


def operator_trigger_workflow_id(schedule_name: str, request_id: str) -> str:
    """Return the identity of one operator trigger of a schedule; a repeat collapses."""
    return _join(OPERATOR_TRIGGER_PREFIX, schedule_name, request_id)
