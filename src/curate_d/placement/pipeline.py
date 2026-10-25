"""Placement state changes and their audit entries, apart from DBOS.

Copy, then verify, then register: a placement is PRESENT only once verification has
assessed recorded evidence, and the transaction that makes it PRESENT is the one that
audits why. A mismatch leaves the copy UNTRUSTED with both observed values; a source that
changed since fingerprinting leaves the placement FAILED and sends the source back to be
fingerprinted, rather than filing new bytes under the old classification.

The source is only ever read. Nothing here writes to it.

A copy that starts discards the previous copy's evidence, and a verification concludes
only the attempt it belongs to, and only while that attempt is still verifying. A
verification run again by a rewind or a fork after a newer attempt has started therefore
changes nothing, and cannot register the old copy's evidence at the new locator. An old
copy run again records neither its evidence nor its failure once its attempt is no
longer the one copying. Each check reads the placement row locked for the rest of its
transaction, so a start and a conclusion racing on one placement serialize and the
second checks against what the first wrote.

A copy reserves its locator in the transaction that records it starting. Reconciliation
resolves collisions against what the ledger held when it evaluated, which says nothing
about content evaluated beside it; so the start locks the path, looks again, and resolves
a collision with other content the same way rendering does before claiming the path.

A row lock holds nothing while the row does not exist, and two first starts rendered to
different locators lock different paths. So a start first locks the placement itself,
by content and destination, then its paths: two starts of one placement serialize
whether or not its row exists yet, and the second finds the first's. When both start
the same attempt at different locators, the second records nothing and leaves the
attempt to the first, whose workflow is the one reconciliation looks for.

"The destination" here is its identity (provider, account and root path), never the
policy's name for it: the placement, its row, its locks and the locator it reserves all
belong to the identity, and the start records the identity in its own transaction. The
name is the placement's label, the latest one it was placed under, so a start under a new
name for the same identity relabels the row and keeps its identifier. A row recorded
before destinations had identities keeps the identifier derived from the name then, so a
start finds the row by content and identity, never by a derived identifier.
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
    DestinationConfig,
    DestinationId,
    Placement,
    PlacementId,
    PlacementState,
    SourceObject,
    SourceObjectState,
    StrongFingerprint,
    derive_placement_id,
)
from curate_d.ledger import (
    AuditRepository,
    DestinationRepository,
    PlacementRepository,
    SourceObjectRepository,
)
from curate_d.ledger.tables import placements
from curate_d.placement.verification import Assessment, TransferEvidence, Verdict
from curate_d.rendering.collision import resolve

DECIDED_BY: Final = "placement"
# Candidate locators a start tries before giving up: the rendered one, then its
# fingerprint-suffixed forms.
_RESERVE_TRIES: Final = 4
# States whose recorded attempt already has, or is about to have, a verification: an
# attempt started over one of them records the next attempt instead.
_RESTARTED: Final = frozenset(
    {PlacementState.COPYING, PlacementState.VERIFYING, PlacementState.PRESENT}
)
# States in which the attempt recorded is still the workflow that started it, or its
# verification: a start of that same attempt elsewhere leaves it to them.
_UNDER_WAY: Final = frozenset({PlacementState.COPYING, PlacementState.VERIFYING})


class LocatorConflictError(RuntimeError):
    """No candidate locator at the destination was free of other content."""


class PlacementStore:
    """Placement evidence, beside the ledger's placement repository."""

    def __init__(self, connection: AsyncConnection) -> None:
        """Wrap a caller-owned connection."""
        self._connection = connection

    async def set_evidence(
        self, placement_id: PlacementId, evidence: TransferEvidence, *, attempt: int | None = None
    ) -> bool:
        """Record a copy's evidence and move the placement to verifying.

        Args:
            placement_id: The placement copied.
            evidence: Both sides of the copy.
            attempt: The attempt that copied. When given, the evidence is recorded only
                while that attempt is the placement's latest and still copying, so an old
                attempt run again cannot overwrite a newer one's.

        Returns:
            Whether the evidence was recorded.
        """
        statement = placements.update().where(placements.c.id == placement_id)
        if attempt is not None:
            statement = statement.where(
                placements.c.attempts == attempt,
                placements.c.state == PlacementState.COPYING.value,
            )
        result = await self._connection.execute(
            statement.values(
                evidence=evidence.model_dump(mode="json"), state=PlacementState.VERIFYING.value
            )
        )
        return result.rowcount > 0

    async def clear_evidence(self, placement_id: PlacementId) -> None:
        """Discard the recorded evidence, so nothing can assess it again."""
        await self._connection.execute(
            placements.update().where(placements.c.id == placement_id).values(evidence=None)
        )

    async def evidence(self, placement_id: PlacementId) -> TransferEvidence | None:
        """Return the latest copy's evidence, or None when none is recorded."""
        value = await self._connection.scalar(
            select(placements.c.evidence).where(placements.c.id == placement_id)
        )
        if value is None:
            return None
        if not isinstance(value, Mapping):
            raise ValueError(f"placement {placement_id} evidence is not an object")
        return TransferEvidence.model_validate(value, strict=False)


def location_details(destination: DestinationConfig) -> dict[str, AuditValue]:
    """Return where a destination puts its copies, as audit details.

    Recorded when a copy starts, so the entry still names the location in force then
    after the destination is redefined.
    """
    return {
        "destination_provider": destination.provider.value,
        "destination_account": destination.account,
        "destination_root": destination.root_path,
    }


def started_details(
    *,
    template_version: str,
    source: SourceObject,
    attempt: int,
    transformations: Mapping[str, str],
    rendered_locator: str,
    held_by: ContentObjectId | None = None,
    context: Mapping[str, AuditValue] | None = None,
) -> dict[str, AuditValue]:
    """Return the details of a placement_started entry.

    Args:
        template_version: The template version the locator was rendered with.
        source: The source the copy reads.
        attempt: The attempt recorded on the placement.
        transformations: Components normalization changed, by rendered form.
        rendered_locator: The locator rendered; recorded only when `held_by` holds it.
        held_by: Other content holding `rendered_locator`, which pushed this content to
            a suffixed locator.
        context: Further evidence the caller holds, such as the destination's location
            and the policy version. The keys above take precedence over it.
    """
    collision: dict[str, AuditValue] = (
        {} if held_by is None else {"rendered_locator": rendered_locator, "held_by": str(held_by)}
    )
    return {
        **(context or {}),
        "template_version": template_version,
        "source_path": source.path,
        "source_version_marker": source.version_marker,
        "attempt": attempt,
        **collision,
        **{f"original_{key}": value for key, value in transformations.items()},
    }


def _audit(
    entry_type: AuditEntryType,
    placement: Placement,
    details: dict[str, AuditValue],
    now: AwareDatetime,
) -> AuditEntry:
    """Return an audit entry about one placement."""
    return AuditEntry(
        entry_type=entry_type,
        occurred_at=now,
        decided_by=DECIDED_BY,
        details={
            "destination": placement.destination_name,
            "destination_id": str(placement.destination_id),
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
    """Return the first candidate from `locator` that is free or already this content's.

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


def path_lock_name(destination_id: DestinationId, locator: str) -> str:
    """Return the advisory-lock name of one path at a destination identity."""
    return f"{destination_id}\n{locator}"


def placement_lock_name(placement_id: PlacementId) -> str:
    """Return the advisory-lock name of one placement.

    No path's name equals it: a destination identifier holds only hexadecimal digits and
    hyphens, so a path's name has a newline before any colon.
    """
    return f"placement:{placement_id}"


async def hold(connection: AsyncConnection, name: str) -> None:
    """Take the advisory lock on `name` until the transaction ends, waiting for it."""
    await connection.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(name, 0))))


async def reserve(
    connection: AsyncConnection,
    *,
    content_object_id: ContentObjectId,
    fingerprint: StrongFingerprint,
    destination_id: DestinationId,
    locator: str,
) -> tuple[str, ContentObjectId | None]:
    """Claim a locator at a destination identity for the rest of the transaction; see `claim`.

    Each candidate is locked before it is looked at, so two starts racing for one path
    serialize and the second sees the first's claim.
    """
    placements_repo = PlacementRepository(connection)

    async def holder(candidate: str) -> Placement | None:
        """Lock `candidate`, then read what holds it."""
        await hold(connection, path_lock_name(destination_id, candidate))
        return await placements_repo.find_at_locator(
            destination_id=destination_id, locator=candidate
        )

    return await claim(
        locator, content_object_id=content_object_id, fingerprint=fingerprint, holder=holder
    )


def taken_elsewhere(existing: Placement, *, attempt: int, locator: str) -> bool:
    """Return whether another start of `attempt` is copying or verifying at another locator.

    An attempt's workflow is identified by its rendered locator and its attempt, so two
    starts of one attempt at one locator are one workflow run again, as by a rewind. At
    different locators they are separate workflows, from reconciliations that overlapped
    and rendered differently: the one that started first owns the attempt, and its
    workflow is the one reconciliation looks for.
    """
    return (
        existing.state in _UNDER_WAY
        and existing.attempts == attempt
        and existing.locator != locator
    )


def recorded_attempt(existing: Placement | None, attempt: int) -> int:
    """Return the attempt a copy starting as `attempt` records; see `start`."""
    if existing is None:
        return attempt
    if existing.state in _RESTARTED and attempt <= existing.attempts:
        return existing.attempts + 1
    return max(existing.attempts, attempt)


async def start(
    connection: AsyncConnection,
    *,
    content_object_id: ContentObjectId,
    fingerprint: StrongFingerprint,
    destination: DestinationConfig,
    locator: str,
    template_version: str,
    source: SourceObject,
    transformations: Mapping[str, str],
    attempt: int = 0,
    context: Mapping[str, AuditValue] | None = None,
    now: AwareDatetime,
) -> tuple[Placement, bool]:
    """Reserve a locator and record that a copy is starting as attempt `attempt`.

    The attempt count is never lowered. A placement still copying, verifying or
    registered elsewhere already has a verification under its attempt; an old attempt
    started over it, as by a rewind, records the next attempt, so its copy is verified
    under an identity of its own. The locator recorded is `locator` unless other
    content already holds it; see `reserve`. The previous copy's evidence is discarded,
    so a verification of that copy run again finds nothing to assess. The placement is
    locked for the transaction before its paths, and its row is read locked, so a start
    racing another of the same placement, with or without a row yet, waits and then
    sees the other's. A start that then finds the same attempt copying or verifying at
    another locator records nothing; see `taken_elsewhere`. `context` joins the audit
    entry's details; see `started_details`. The destination's identity is recorded first,
    in this transaction, unless it already is; an existing row keeps its identifier and
    takes `destination`'s name.

    Returns:
        The placement, and whether a copy should start. False, with the placement left
        exactly as it was, when it is already registered as present at the reserved
        locator, or when another start of this attempt is copying or verifying it at
        another locator; the placement's state tells the two apart.
    """
    placements_repo = PlacementRepository(connection)
    destination_id = await DestinationRepository(connection).ensure(destination.identity)
    await hold(
        connection, placement_lock_name(derive_placement_id(content_object_id, destination_id))
    )
    reserved, displaced_by = await reserve(
        connection,
        content_object_id=content_object_id,
        fingerprint=fingerprint,
        destination_id=destination_id,
        locator=locator,
    )
    built = Placement.build(
        content_object_id=content_object_id,
        destination_id=destination_id,
        destination_name=destination.name,
        locator=reserved,
        template_version=template_version,
    )
    existing = await placements_repo.find(content_object_id, destination_id, for_update=True)
    if existing is not None and (
        (existing.state is PlacementState.PRESENT and existing.locator == reserved)
        or taken_elsewhere(existing, attempt=attempt, locator=reserved)
    ):
        return existing, False
    placement = built.model_copy(
        update={
            "id": built.id if existing is None else existing.id,
            "state": PlacementState.COPYING,
            "source_object_id": source.id,
            "attempts": recorded_attempt(existing, attempt),
        }
    )
    stored = await placements_repo.upsert(placement)
    await PlacementStore(connection).clear_evidence(stored.id)
    await AuditRepository(connection).append(
        _audit(
            AuditEntryType.PLACEMENT_STARTED,
            stored,
            started_details(
                template_version=template_version,
                source=source,
                attempt=stored.attempts,
                transformations=transformations,
                rendered_locator=locator,
                held_by=displaced_by,
                context=context,
            ),
            now,
        )
    )
    return stored, True


async def defer(
    connection: AsyncConnection,
    placement_id: PlacementId,
    reason: str,
    *,
    attempt: int | None = None,
) -> bool:
    """Record why a copy is waiting for resources; it stays copying and is retried.

    Args:
        connection: The connection of the writing transaction.
        placement_id: The placement waiting.
        reason: Why it waits.
        attempt: The attempt waiting. When given, the reason is recorded only while that
            attempt is the placement's latest and still copying.

    Returns:
        Whether the reason was recorded.
    """
    statement = placements.update().where(placements.c.id == placement_id)
    if attempt is not None:
        statement = statement.where(
            placements.c.attempts == attempt,
            placements.c.state == PlacementState.COPYING.value,
        )
    result = await connection.execute(statement.values(failure_reason=reason))
    return result.rowcount > 0


async def fail(
    connection: AsyncConnection,
    placement_id: PlacementId,
    reason: str,
    *,
    attempt: int | None = None,
    now: AwareDatetime,
) -> Placement | None:
    """Record a copy that did not complete; the next attempt is a new workflow.

    When `attempt` is given, only a placement still copying that attempt is failed, so
    an old attempt run again by a rewind or a fork cannot fail a newer attempt or a
    registered copy. The row is locked for the transaction before it is checked.

    Returns:
        The placement as recorded, or None when there was nothing to fail.
    """
    placements_repo = PlacementRepository(connection)
    placement = await placements_repo.get(placement_id, for_update=True)
    if placement is None or (
        attempt is not None
        and (placement.state is not PlacementState.COPYING or placement.attempts != attempt)
    ):
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

    Only a placement still registered as present is changed, checked on the row locked
    for the transaction; the next attempt to re-establish it is a new workflow identity.

    Returns:
        The placement as recorded, or None when it was not present any more.
    """
    placements_repo = PlacementRepository(connection)
    placement = await placements_repo.get(placement_id, for_update=True)
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
    attempt: int,
    source_version_marker: str | None = None,
    now: AwareDatetime,
) -> tuple[Placement, SourceObject | None] | None:
    """Register, distrust or fail a placement from its assessment.

    Only a placement still verifying `attempt` is concluded. The check runs in the
    writing transaction on the row locked for it, so a verification run again after a
    newer attempt started changes nothing and writes no audit entry.

    Args:
        connection: The connection of the writing transaction.
        placement_id: The placement assessed.
        assessment: What the copy's evidence showed.
        attempt: The attempt whose evidence was assessed.
        source_version_marker: The source's version when it was copied, for the audit.
        now: When the conclusion is recorded.

    Returns:
        None when there was nothing to conclude. Otherwise the placement as recorded,
        and the source when it was sent back to be fingerprinted.
    """
    placements_repo = PlacementRepository(connection)
    placement = await placements_repo.get(placement_id, for_update=True)
    if (
        placement is None
        or placement.state is not PlacementState.VERIFYING
        or placement.attempts != attempt
    ):
        return None
    details: dict[str, AuditValue] = {
        "compared": assessment.compared,
        "source_value": assessment.source_value,
        "destination_value": assessment.destination_value,
        "reason": assessment.reason,
        "attempt": placement.attempts,
        "source_version_marker": source_version_marker,
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
