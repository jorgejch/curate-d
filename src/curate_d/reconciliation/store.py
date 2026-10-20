"""What reconciliation reads and records beside the ledger's repositories."""

from collections.abc import Sequence
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
    """The latest look at one destination for its registered placements.

    Kept per destination identity; the name is the policy's label when it was checked.
    """

    destination_id: DestinationId
    destination_name: str
    checked_at: AwareDatetime
    reachable: bool
    reason: str | None = Field(default=None, description="Why it could not be checked.")
    absent: int = Field(default=0, ge=0)


class ReconciliationStore:
    """Retention decisions already recorded, present placements, destination checks."""

    def __init__(self, connection: AsyncConnection) -> None:
        """Wrap a caller-owned connection."""
        self._connection = connection

    async def last_retention(self, content_object_id: ContentObjectId) -> tuple[str, str] | None:
        """The outcome and reason of the latest retention decision recorded, if any."""
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
        return str(row["details"].get("outcome")), str(row["details"].get("reason"))

    async def present_at(
        self, destination_id: DestinationId, *, after: PlacementId | None, limit: int
    ) -> list[tuple[PlacementId, str]]:
        """(id, locator) of placements registered as present at a destination, in id order.

        Only placements at that identity: those another account or root holds under the
        same name are not there to be found.
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
        """Replace a destination's latest check."""
        values = check.model_dump()
        await self._connection.execute(
            insert(destination_checks)
            .values(**values)
            .on_conflict_do_update(index_elements=["destination_id"], set_=values)
        )

    async def checks(
        self, names: Sequence[str] = (), *, ids: Sequence[DestinationId] = ()
    ) -> list[DestinationCheck]:
        """The latest check of each destination, or of those named or identified."""
        query = select(destination_checks).order_by(
            destination_checks.c.destination_name, destination_checks.c.destination_id
        )
        if names:
            query = query.where(destination_checks.c.destination_name.in_(names))
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
