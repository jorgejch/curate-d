"""What reconciliation reads and records beside the ledger's repositories."""

from collections.abc import Mapping, Sequence
from typing import Final

from pydantic import AwareDatetime, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from curate_d.domain import (
    AuditEntryType,
    ContentObjectId,
    DestinationId,
    DomainModel,
    PlacementId,
    PlacementState,
)
from curate_d.ledger.mapping import identifier, moment, opt_text, text
from curate_d.ledger.tables import audit_entries, destination_checks, placements

DECIDED_BY: Final = "reconciliation"


class DestinationCheck(DomainModel):
    """The latest look at one destination identity for its registered placements."""

    destination_id: DestinationId
    destination_name: str = Field(description="The policy's name for it when checked: a label.")
    checked_at: AwareDatetime
    reachable: bool
    reason: str | None = Field(default=None, description="Why it could not be checked.")
    absent: int = Field(default=0, ge=0)


class RecordedRetention(DomainModel):
    """The latest retention decision recorded for a content object."""

    outcome: str
    reason: str
    policy_version: int | None = Field(
        default=None, description="Null when unknown, or written by a build that did not record it."
    )

    @classmethod
    def from_details(cls, details: Mapping[str, object]) -> "RecordedRetention":
        """Read the decision from a `retention_decided` entry's details."""
        version = details.get("policy_version")
        return cls(
            outcome=str(details.get("outcome")),
            reason=str(details.get("reason")),
            policy_version=version
            if isinstance(version, int) and not isinstance(version, bool)
            else None,
        )


class ReconciliationStore:
    """Retention decisions already recorded, present placements, destination checks."""

    def __init__(self, connection: AsyncConnection) -> None:
        """Wrap a caller-owned connection."""
        self._connection = connection

    async def last_retention(self, content_object_id: ContentObjectId) -> RecordedRetention | None:
        """Return the latest retention decision recorded, if any."""
        row = (
            (
                await self._connection.execute(
                    select(audit_entries.c.details)
                    .where(
                        audit_entries.c.content_object_id == content_object_id,
                        audit_entries.c.entry_type == AuditEntryType.RETENTION_DECIDED.value,
                    )
                    .order_by(audit_entries.c.id.desc())
                    .limit(1)
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None or not isinstance(row["details"], dict):
            return None
        return RecordedRetention.from_details(row["details"])

    async def present_at(
        self, destination_id: DestinationId, *, after: PlacementId | None, limit: int
    ) -> list[tuple[PlacementId, str]]:
        """Return (id, locator) of placements present at a destination identity, in id order.

        Only placements made at this identity: ones made under the same name at another
        account or root are not there to be found.
        """
        query = (
            select(placements.c.id, placements.c.locator)
            .where(
                placements.c.destination_id == destination_id,
                placements.c.state == PlacementState.PRESENT.value,
            )
            .order_by(placements.c.id)
            .limit(limit)
        )
        if after is not None:
            query = query.where(placements.c.id > after)
        rows = await self._connection.execute(query)
        return [
            (PlacementId(identifier(row, "id")), text(row, "locator")) for row in rows.mappings()
        ]

    async def record_check(self, check: DestinationCheck) -> None:
        """Replace a destination identity's latest check, relabelling it with the name."""
        values = check.model_dump()
        await self._connection.execute(
            insert(destination_checks)
            .values(**values)
            .on_conflict_do_update(index_elements=["destination_id"], set_=values)
        )

    async def checks(self, ids: Sequence[DestinationId] = ()) -> list[DestinationCheck]:
        """Return the latest check of each destination identity, or of those given."""
        query = select(destination_checks).order_by(
            destination_checks.c.destination_name, destination_checks.c.destination_id
        )
        if ids:
            query = query.where(destination_checks.c.destination_id.in_(ids))
        rows = await self._connection.execute(query)
        return [
            DestinationCheck(
                destination_id=DestinationId(identifier(row, "destination_id")),
                destination_name=text(row, "destination_name"),
                checked_at=moment(row, "checked_at"),
                reachable=bool(row["reachable"]),
                reason=opt_text(row, "reason"),
                absent=int(str(row["absent"])),
            )
            for row in rows.mappings()
        ]
