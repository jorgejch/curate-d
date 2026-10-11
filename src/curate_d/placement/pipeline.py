"""Placement state changes and their audit entries, apart from DBOS.

Copy, then verify, then register: a placement is PRESENT only once verification has
assessed recorded evidence, and the transaction that makes it PRESENT is the one that
audits why. A mismatch leaves the copy UNTRUSTED with both observed values; a source that
changed since fingerprinting leaves the placement FAILED and sends the source back to be
fingerprinted, rather than filing new bytes under the old classification.

The source is only ever read. Nothing here writes to it.

A copy reserves its locator in the transaction that records it starting. Reconciliation
resolves collisions against what the ledger held when it evaluated, which says nothing
about content evaluated beside it; so the start locks the path, looks again, and resolves
a collision with other content the same way rendering does before claiming the path.
"""

from collections.abc import Awaitable, Callable, Mapping
from typing import Final

from pydantic import AwareDatetime
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from curate_d.domain import (
    AuditEntry,
    AuditEntryType,
    AuditValue,
    ContentObjectId,
    Placement,
    PlacementId,
    PlacementState,
    SourceObject,
    SourceObjectState,
    StrongFingerprint,
)
from curate_d.ledger import AuditRepository, PlacementRepository, SourceObjectRepository
from curate_d.ledger.tables import placements
from curate_d.placement.verification import Assessment, TransferEvidence, Verdict
from curate_d.rendering.collision import resolve

DECIDED_BY: Final = "placement"
# Candidate locators a start tries before giving up: the rendered one, then its
# fingerprint-suffixed forms.
_RESERVE_TRIES: Final = 4


class LocatorConflictError(RuntimeError):
    """No candidate locator at the destination was free of other content."""


class PlacementStore:
    """Placement evidence, beside the ledger's placement repository."""

    def __init__(self, connection: AsyncConnection) -> None:
        """Wrap a caller-owned connection."""
        self._connection = connection

    async def set_evidence(self, placement_id: PlacementId, evidence: TransferEvidence) -> None:
        """Record a copy's evidence and move the placement to verifying."""
        await self._connection.execute(
            placements.update()
            .where(placements.c.id == placement_id)
            .values(evidence=evidence.model_dump(mode="json"), state=PlacementState.VERIFYING.value)
        )

    async def evidence(self, placement_id: PlacementId) -> TransferEvidence | None:
        """The latest copy's evidence."""
        value = await self._connection.scalar(
            select(placements.c.evidence).where(placements.c.id == placement_id)
        )
        if value is None:
            return None
        if not isinstance(value, Mapping):
            raise ValueError(f"placement {placement_id} evidence is not an object")
        return TransferEvidence.model_validate(value, strict=False)


def _audit(
    entry_type: AuditEntryType,
    placement: Placement,
    details: dict[str, AuditValue],
    now: AwareDatetime,
) -> AuditEntry:
    """An audit entry about one placement."""
    return AuditEntry(
        entry_type=entry_type,
        occurred_at=now,
        decided_by=DECIDED_BY,
        details={
            "destination": placement.destination_name,
            "locator": placement.locator,
            **details,
        },
        content_object_id=placement.content_object_id,
        placement_id=placement.id,
        source_object_id=placement.source_object_id,
    )


async def claim(
    locator: str,
    *,
    content_object_id: ContentObjectId,
    fingerprint: StrongFingerprint,
    holder: Callable[[str], Awaitable[Placement | None]],
) -> tuple[str, ContentObjectId | None]:
    """The first candidate from `locator` that is free or already this content's.

    `holder` answers what occupies a candidate. A candidate other content holds is
    resolved as rendering resolves a collision, by this content's fingerprint suffix.

    Returns:
        The locator, and the content holding `locator` when that pushed this content to
        a suffixed one.

    Raises:
        LocatorConflictError: every candidate is held by other content.
    """
    candidate = locator
    displaced_by: ContentObjectId | None = None
    for _ in range(_RESERVE_TRIES):
        occupant = await holder(candidate)
        resolved = resolve(
            candidate, occupant, content_object_id=content_object_id, fingerprint=fingerprint
        ).locator
        if resolved == candidate:
            return candidate, displaced_by
        if displaced_by is None and occupant is not None:
            displaced_by = occupant.content_object_id
        candidate = resolved
    raise LocatorConflictError(f"no free locator for content {content_object_id} from {locator}")


async def reserve(
    connection: AsyncConnection,
    *,
    content_object_id: ContentObjectId,
    fingerprint: StrongFingerprint,
    destination_name: str,
    locator: str,
) -> tuple[str, ContentObjectId | None]:
    """Claim a locator at a destination for the rest of the transaction; see `claim`.

    Each candidate is locked before it is looked at, so two starts racing for one path
    serialize and the second sees the first's claim.
    """
    placements_repo = PlacementRepository(connection)

    async def holder(candidate: str) -> Placement | None:
        """Lock `candidate`, then read what holds it."""
        key = func.hashtextextended(f"{destination_name}\n{candidate}", 0)
        await connection.execute(select(func.pg_advisory_xact_lock(key)))
        return await placements_repo.find_at_locator(
            destination_name=destination_name, locator=candidate
        )

    return await claim(
        locator, content_object_id=content_object_id, fingerprint=fingerprint, holder=holder
    )


async def start(
    connection: AsyncConnection,
    *,
    content_object_id: ContentObjectId,
    fingerprint: StrongFingerprint,
    destination_name: str,
    locator: str,
    template_version: str,
    source: SourceObject,
    transformations: Mapping[str, str],
    attempt: int = 0,
    now: AwareDatetime,
) -> tuple[Placement, bool]:
    """Reserve a locator and record that a copy is starting as attempt `attempt`.

    The attempt count is never lowered. The locator recorded is `locator` unless other
    content already holds it; see `reserve`.

    Returns:
        The placement, and whether a copy should start: False when it is already
        registered as present at the reserved locator, which is left exactly as it was.
    """
    placements_repo = PlacementRepository(connection)
    reserved, displaced_by = await reserve(
        connection,
        content_object_id=content_object_id,
        fingerprint=fingerprint,
        destination_name=destination_name,
        locator=locator,
    )
    built = Placement.build(
        content_object_id=content_object_id,
        destination_name=destination_name,
        locator=reserved,
        template_version=template_version,
    )
    existing = await placements_repo.get(built.id)
    if (
        existing is not None
        and existing.state is PlacementState.PRESENT
        and existing.locator == reserved
    ):
        return existing, False
    placement = built.model_copy(
        update={
            "state": PlacementState.COPYING,
            "source_object_id": source.id,
            "attempts": attempt if existing is None else max(existing.attempts, attempt),
        }
    )
    stored = await placements_repo.upsert(placement)
    collision: dict[str, AuditValue] = (
        {} if displaced_by is None else {"rendered_locator": locator, "held_by": str(displaced_by)}
    )
    await AuditRepository(connection).append(
        _audit(
            AuditEntryType.PLACEMENT_STARTED,
            stored,
            {
                "template_version": template_version,
                "source_path": source.path,
                "attempt": stored.attempts,
                **collision,
                **{f"original_{key}": value for key, value in transformations.items()},
            },
            now,
        )
    )
    return stored, True


async def defer(connection: AsyncConnection, placement_id: PlacementId, reason: str) -> None:
    """Record why a copy is waiting for resources; it stays copying and is retried."""
    await connection.execute(
        placements.update().where(placements.c.id == placement_id).values(failure_reason=reason)
    )


async def fail(
    connection: AsyncConnection,
    placement_id: PlacementId,
    reason: str,
    *,
    now: AwareDatetime,
) -> Placement | None:
    """Record a copy that did not complete; the next attempt is a new workflow."""
    placements_repo = PlacementRepository(connection)
    placement = await placements_repo.get(placement_id)
    if placement is None:
        return None
    failed = await placements_repo.upsert(
        placement.model_copy(
            update={
                "state": PlacementState.FAILED,
                "failure_reason": reason,
                "attempts": placement.attempts + 1,
            }
        )
    )
    await AuditRepository(connection).append(
        _audit(AuditEntryType.PLACEMENT_FAILED, failed, {"reason": reason}, now)
    )
    return failed


async def mark_absent(
    connection: AsyncConnection,
    placement_id: PlacementId,
    *,
    checked: str,
    now: AwareDatetime,
) -> Placement | None:
    """Record that a registered copy is missing from its destination.

    Only a placement still registered as present is changed; the next attempt to
    re-establish it is a new workflow identity.

    Returns:
        The placement as recorded, or None when it was not present any more.
    """
    placements_repo = PlacementRepository(connection)
    placement = await placements_repo.get(placement_id)
    if placement is None or placement.state is not PlacementState.PRESENT:
        return None
    absent = await placements_repo.upsert(
        placement.model_copy(
            update={
                "state": PlacementState.ABSENT,
                "failure_reason": f"not found at the destination by {checked}",
                "attempts": placement.attempts + 1,
            }
        )
    )
    await AuditRepository(connection).append(
        _audit(
            AuditEntryType.PLACEMENT_ABSENT,
            absent,
            {"checked": checked, "level": placement.verification_level.name.lower()},
            now,
        )
    )
    return absent


async def conclude(
    connection: AsyncConnection,
    placement_id: PlacementId,
    assessment: Assessment,
    *,
    now: AwareDatetime,
) -> tuple[Placement | None, SourceObject | None]:
    """Register, distrust or fail a placement from its assessment.

    Returns:
        The placement as recorded, and the source when it was sent back to be
        fingerprinted.
    """
    placements_repo = PlacementRepository(connection)
    placement = await placements_repo.get(placement_id)
    if placement is None:
        return None, None
    details: dict[str, AuditValue] = {
        "compared": assessment.compared,
        "source_value": assessment.source_value,
        "destination_value": assessment.destination_value,
        "reason": assessment.reason,
    }
    audit = AuditRepository(connection)
    if assessment.verdict is Verdict.VERIFIED:
        registered = await placements_repo.upsert(
            placement.model_copy(
                update={
                    "state": PlacementState.PRESENT,
                    "verification_level": assessment.level,
                    "verified_at": now,
                    "failure_reason": None,
                    "observed_source_digest": assessment.source_value,
                    "observed_destination_digest": assessment.destination_value,
                }
            )
        )
        await audit.append(
            _audit(
                AuditEntryType.VERIFICATION_SUCCEEDED,
                registered,
                {**details, "level": assessment.level.name.lower()},
                now,
            )
        )
        await audit.append(
            _audit(
                AuditEntryType.PLACEMENT_REGISTERED,
                registered,
                {"level": assessment.level.name.lower()},
                now,
            )
        )
        return registered, None

    state = (
        PlacementState.UNTRUSTED
        if assessment.verdict is Verdict.MISMATCH
        else PlacementState.FAILED
    )
    recorded = await placements_repo.upsert(
        placement.model_copy(
            update={
                "state": state,
                "failure_reason": assessment.reason or assessment.verdict.value,
                "observed_source_digest": assessment.source_value,
                "observed_destination_digest": assessment.destination_value,
                "attempts": placement.attempts + 1,
            }
        )
    )
    await audit.append(
        _audit(
            AuditEntryType.VERIFICATION_FAILED,
            recorded,
            {**details, "verdict": assessment.verdict.value},
            now,
        )
    )
    if assessment.verdict is not Verdict.SOURCE_CHANGED or placement.source_object_id is None:
        return recorded, None
    sources = SourceObjectRepository(connection)
    source = await sources.get(placement.source_object_id)
    if source is None:
        return recorded, None
    reset = await sources.upsert(
        source.model_copy(
            update={
                "state": SourceObjectState.DISCOVERED,
                "content_object_id": None,
                "failure_reason": None,
                "reported_digests": (),
            }
        )
    )
    await audit.append(
        AuditEntry(
            entry_type=AuditEntryType.SOURCE_DISCREPANCY_DETECTED,
            occurred_at=now,
            decided_by=DECIDED_BY,
            details=details,
            source_object_id=reset.id,
            content_object_id=placement.content_object_id,
            placement_id=placement.id,
        )
    )
    return recorded, reset
