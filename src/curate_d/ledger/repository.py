"""Repositories over the three identity layers plus the audit trail.

Every repository takes a connection rather than owning one. That is deliberate: inside a
workflow the connection comes from a DBOS datasource transaction, and it is that
transaction committing its tracking record alongside the row that makes the write
exactly-once on replay. A repository that opened its own connection would silently
opt out of the guarantee.
"""

from collections.abc import Sequence

from pydantic import AwareDatetime
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from curate_d.domain import (
    AuditEntry,
    ContentObject,
    ContentObjectId,
    DestinationId,
    DestinationIdentity,
    Placement,
    PlacementId,
    Provider,
    SourceObject,
    SourceObjectId,
    SourceObjectState,
    StrongFingerprint,
    derive_content_object_id,
)
from curate_d.ledger.mapping import (
    audit_entry_values,
    content_object_values,
    destination_values,
    identifier,
    placement_values,
    source_object_values,
    text,
    to_audit_entry,
    to_content_object,
    to_destination,
    to_placement,
    to_source_object,
)
from curate_d.ledger.tables import (
    audit_entries,
    content_objects,
    destinations,
    placements,
    source_objects,
)


class SourceObjectRepository:
    """Reads and writes the provider-native identity layer."""

    def __init__(self, connection: AsyncConnection) -> None:
        """Wrap a caller-owned connection; the repository never opens its own."""
        self._connection = connection

    async def upsert(self, source: SourceObject) -> SourceObject:
        """Insert, or update in place when the provider already knows this file.

        Keyed on the provider's own identifier, so a rename updates the recorded path
        rather than creating a second row and orphaning the existing placements.
        """
        statement = insert(source_objects).values(**source_object_values(source))
        upsert = statement.on_conflict_do_update(
            index_elements=["provider", "account", "native_id"],
            set_={
                key: statement.excluded[key]
                for key in (
                    "path",
                    "size_bytes",
                    "modified_at",
                    "version_marker",
                    "state",
                    "change_signal",
                    "content_object_id",
                    "last_verified_at",
                    "failure_reason",
                    "reported_sha1",
                    "reported_sha256",
                    "reported_md5",
                )
            },
        ).returning(*source_objects.c)
        result = await self._connection.execute(upsert)
        return to_source_object(result.mappings().one())

    async def get(self, source_object_id: SourceObjectId) -> SourceObject | None:
        """Fetch a source object by its derived identifier, or None if absent."""
        result = await self._connection.execute(
            select(source_objects).where(source_objects.c.id == source_object_id)
        )
        row = result.mappings().one_or_none()
        return to_source_object(row) if row is not None else None

    async def find_by_native_identity(
        self, *, provider: Provider, account: str, native_id: str
    ) -> SourceObject | None:
        """Look up by the identity the provider itself uses. Anchors change detection."""
        result = await self._connection.execute(
            select(source_objects).where(
                source_objects.c.provider == provider.value,
                source_objects.c.account == account,
                source_objects.c.native_id == native_id,
            )
        )
        row = result.mappings().one_or_none()
        return to_source_object(row) if row is not None else None

    async def find_by_native_ids(
        self, *, provider: Provider, account: str, native_ids: Sequence[str]
    ) -> dict[str, SourceObject]:
        """Recorded rows for a batch of listed files, keyed by native identifier."""
        if not native_ids:
            return {}
        result = await self._connection.execute(
            select(source_objects).where(
                source_objects.c.provider == provider.value,
                source_objects.c.account == account,
                source_objects.c.native_id.in_(list(native_ids)),
            )
        )
        return {text(row, "native_id"): to_source_object(row) for row in result.mappings()}

    async def find_by_content_object(
        self, content_object_id: ContentObjectId
    ) -> Sequence[SourceObject]:
        """Every source location resolving to the same bytes."""
        result = await self._connection.execute(
            select(source_objects)
            .where(source_objects.c.content_object_id == content_object_id)
            .order_by(source_objects.c.provider, source_objects.c.account, source_objects.c.path)
        )
        return [to_source_object(row) for row in result.mappings()]

    async def attach_content_object(
        self, source_object_id: SourceObjectId, content_object_id: ContentObjectId
    ) -> None:
        """Link a source object to its fingerprinted content and mark it fingerprinted."""
        await self._connection.execute(
            source_objects.update()
            .where(source_objects.c.id == source_object_id)
            .values(
                content_object_id=content_object_id,
                state=SourceObjectState.FINGERPRINTED.value,
                failure_reason=None,
            )
        )

    async def mark_failed(self, source_object_id: SourceObjectId, reason: str) -> None:
        """Record why a file could not be ingested, without creating a content object."""
        await self._connection.execute(
            source_objects.update()
            .where(source_objects.c.id == source_object_id)
            .values(state=SourceObjectState.FAILED.value, failure_reason=reason)
        )

    async def record_verification(
        self, source_object_id: SourceObjectId, verified_at: AwareDatetime
    ) -> None:
        """Stamp a full-rescan confirmation, which is what makes a rescan resumable."""
        await self._connection.execute(
            source_objects.update()
            .where(source_objects.c.id == source_object_id)
            .values(last_verified_at=verified_at)
        )

    async def find_stale_for_full_rescan(
        self, *, provider: Provider, account: str, before: AwareDatetime, limit: int
    ) -> Sequence[SourceObject]:
        """Files a full rescan has not yet confirmed, never-verified ones first.

        Ordering nulls first is the resumption mechanism: an interrupted rescan picks up
        where it stopped instead of starting over.
        """
        result = await self._connection.execute(
            select(source_objects)
            .where(
                source_objects.c.provider == provider.value,
                source_objects.c.account == account,
                (source_objects.c.last_verified_at.is_(None))
                | (source_objects.c.last_verified_at < before),
            )
            .order_by(source_objects.c.last_verified_at.asc().nullsfirst(), source_objects.c.path)
            .limit(limit)
        )
        return [to_source_object(row) for row in result.mappings()]


class ContentObjectRepository:
    """Reads and writes the content-addressed identity layer."""

    def __init__(self, connection: AsyncConnection) -> None:
        """Wrap a caller-owned connection; the repository never opens its own."""
        self._connection = connection

    async def upsert(self, content: ContentObject) -> ContentObject:
        """Insert, or return the existing row for these bytes.

        Two source objects with equal fingerprints must converge on one row; that
        convergence is what makes classification run once per distinct file.
        """
        statement = insert(content_objects).values(**content_object_values(content))
        upsert = statement.on_conflict_do_update(
            index_elements=["fingerprint_algorithm", "fingerprint_digest"],
            set_={
                # Fill in a perceptual fingerprint we did not have, but never erase one
                # because this particular caller did not compute it.
                "perceptual_fingerprint": func.coalesce(
                    statement.excluded.perceptual_fingerprint,
                    content_objects.c.perceptual_fingerprint,
                ),
            },
        ).returning(*content_objects.c)
        result = await self._connection.execute(upsert)
        return to_content_object(result.mappings().one())

    async def get(self, content_object_id: ContentObjectId) -> ContentObject | None:
        """Fetch a content object by its derived identifier, or None if absent."""
        result = await self._connection.execute(
            select(content_objects).where(content_objects.c.id == content_object_id)
        )
        row = result.mappings().one_or_none()
        return to_content_object(row) if row is not None else None

    async def find_by_fingerprint(self, fingerprint: StrongFingerprint) -> ContentObject | None:
        """Look up by content identity. This is the deduplication lookup."""
        result = await self._connection.execute(
            select(content_objects).where(
                content_objects.c.id == derive_content_object_id(str(fingerprint))
            )
        )
        row = result.mappings().one_or_none()
        return to_content_object(row) if row is not None else None


class DestinationRepository:
    """Records the destination identities placements belong to."""

    def __init__(self, connection: AsyncConnection) -> None:
        """Wrap a caller-owned connection; the repository never opens its own."""
        self._connection = connection

    async def ensure(self, destination: DestinationIdentity) -> DestinationId:
        """Record a destination identity unless it already is; return its identifier."""
        await self._connection.execute(
            insert(destinations).values(**destination_values(destination)).on_conflict_do_nothing()
        )
        return destination.id

    async def get(self, destination_id: DestinationId) -> DestinationIdentity | None:
        """Fetch a destination identity, or None if nothing was ever placed there."""
        result = await self._connection.execute(
            select(destinations).where(destinations.c.id == destination_id)
        )
        row = result.mappings().one_or_none()
        return to_destination(row) if row is not None else None

    async def get_many(
        self, destination_ids: Sequence[DestinationId]
    ) -> dict[DestinationId, DestinationIdentity]:
        """Fetch several destination identities by identifier; unknown ones are left out."""
        if not destination_ids:
            return {}
        result = await self._connection.execute(
            select(destinations).where(destinations.c.id.in_(set(destination_ids)))
        )
        return {
            DestinationId(identifier(row, "id")): to_destination(row) for row in result.mappings()
        }


class PlacementRepository:
    """Reads and writes the per-destination identity layer.

    A placement's destination row must exist before the placement is written: see
    `DestinationRepository.ensure`.
    """

    def __init__(self, connection: AsyncConnection) -> None:
        """Wrap a caller-owned connection; the repository never opens its own."""
        self._connection = connection

    async def upsert(self, placement: Placement) -> Placement:
        """Insert, or update in place when this content already has a row at the destination.

        The row keeps the identifier it was first given; the destination name follows the
        latest placement, so a rename that keeps the identity relabels the row.
        """
        statement = insert(placements).values(**placement_values(placement))
        upsert = statement.on_conflict_do_update(
            index_elements=["content_object_id", "destination_id"],
            set_={
                key: statement.excluded[key]
                for key in (
                    "destination_name",
                    "locator",
                    "template_version",
                    "state",
                    "verification_level",
                    "verified_at",
                    "failure_reason",
                    "observed_source_digest",
                    "observed_destination_digest",
                    "source_object_id",
                    "attempts",
                )
            },
        ).returning(*placements.c)
        result = await self._connection.execute(upsert)
        return to_placement(result.mappings().one())

    async def get(self, placement_id: PlacementId) -> Placement | None:
        """Fetch a placement by its derived identifier, or None if absent."""
        result = await self._connection.execute(
            select(placements).where(placements.c.id == placement_id)
        )
        row = result.mappings().one_or_none()
        return to_placement(row) if row is not None else None

    async def find(
        self, content_object_id: ContentObjectId, destination_id: DestinationId
    ) -> Placement | None:
        """Fetch this content's placement at a destination, or None if absent.

        Rows recorded before destinations had identities keep identifiers derived from the
        destination's name, so this, not `get` with a derived identifier, is how to find
        an existing placement.
        """
        result = await self._connection.execute(
            select(placements).where(
                placements.c.content_object_id == content_object_id,
                placements.c.destination_id == destination_id,
            )
        )
        row = result.mappings().one_or_none()
        return to_placement(row) if row is not None else None

    async def find_for_content_object(
        self, content_object_id: ContentObjectId
    ) -> Sequence[Placement]:
        """Every destination this content has been placed at. Drives reconciliation."""
        result = await self._connection.execute(
            select(placements)
            .where(placements.c.content_object_id == content_object_id)
            .order_by(placements.c.destination_name, placements.c.destination_id)
        )
        return [to_placement(row) for row in result.mappings()]

    async def find_at_locator(
        self, *, destination_id: DestinationId, locator: str
    ) -> Placement | None:
        """What already occupies a rendered path. Drives collision resolution."""
        result = await self._connection.execute(
            select(placements).where(
                placements.c.destination_id == destination_id,
                placements.c.locator == locator,
            )
        )
        row = result.mappings().one_or_none()
        return to_placement(row) if row is not None else None


class AuditRepository:
    """Appends to the audit trail. Deliberately offers no update or delete."""

    def __init__(self, connection: AsyncConnection) -> None:
        """Wrap a caller-owned connection; the repository never opens its own."""
        self._connection = connection

    async def append(self, entry: AuditEntry) -> int:
        """Insert an audit entry and return its assigned id.

        Returns:
            The auto-incremented id of the newly inserted row.
        """
        result = await self._connection.execute(
            audit_entries.insert().values(**audit_entry_values(entry)).returning(audit_entries.c.id)
        )
        return int(result.scalar_one())

    async def history_for_content_object(
        self, content_object_id: ContentObjectId
    ) -> Sequence[AuditEntry]:
        """Ordered decision history, which is what the control plane exposes."""
        result = await self._connection.execute(
            select(audit_entries)
            .where(audit_entries.c.content_object_id == content_object_id)
            .order_by(audit_entries.c.occurred_at, audit_entries.c.id)
        )
        return [to_audit_entry(row) for row in result.mappings()]
