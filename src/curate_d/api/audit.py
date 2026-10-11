"""A content object's recorded history: how it was classified, placed, verified, retained.

The audit trail is append-only and read in the order it was recorded, so a decision that
was later revised appears with the revision after it. The placements as they stand now
are reported beside it, with the verification each reached.
"""

from collections.abc import Callable, Sequence
from typing import Annotated, Protocol

from fastapi import APIRouter, Depends, Request
from pydantic import AwareDatetime, BaseModel
from sqlalchemy.ext.asyncio import AsyncEngine

from curate_d.api.errors import NOT_FOUND, NotFoundError, problems
from curate_d.domain import (
    AuditEntry,
    AuditEntryType,
    AuditValue,
    ContentObject,
    ContentObjectId,
    Placement,
    PlacementState,
)
from curate_d.ledger import AuditRepository, ContentObjectRepository, PlacementRepository


class Event(BaseModel):
    """One recorded decision or action."""

    entry_type: AuditEntryType
    occurred_at: AwareDatetime
    decided_by: str
    details: dict[str, AuditValue]
    placement_id: str | None = None
    source_object_id: str | None = None


class PlacementView(BaseModel):
    """A placement as it stands, and how strongly its copy was verified."""

    id: str
    destination: str
    locator: str
    state: PlacementState
    verification_level: str
    verified_at: AwareDatetime | None = None
    failure_reason: str | None = None


class History(BaseModel):
    """A content object, its placements now, and everything recorded about it in order."""

    content_object_id: ContentObjectId
    fingerprint: str
    size_bytes: int
    media_type: str
    placements: list[PlacementView]
    events: list[Event]


def event_of(entry: AuditEntry) -> Event:
    """The response for an audit entry."""
    return Event(
        entry_type=entry.entry_type,
        occurred_at=entry.occurred_at,
        decided_by=entry.decided_by,
        details=dict(entry.details),
        placement_id=None if entry.placement_id is None else str(entry.placement_id),
        source_object_id=None if entry.source_object_id is None else str(entry.source_object_id),
    )


def placement_of(placement: Placement) -> PlacementView:
    """The response for a placement."""
    return PlacementView(
        id=str(placement.id),
        destination=placement.destination_name,
        locator=placement.locator,
        state=placement.state,
        verification_level=placement.verification_level.name.lower(),
        verified_at=placement.verified_at,
        failure_reason=placement.failure_reason,
    )


def history_of(
    content: ContentObject, placements: Sequence[Placement], entries: Sequence[AuditEntry]
) -> History:
    """A content object's history, its placements ordered by destination."""
    return History(
        content_object_id=content.id,
        fingerprint=f"{content.fingerprint.algorithm.value}:{content.fingerprint.digest}",
        size_bytes=content.size_bytes,
        media_type=content.media_type,
        placements=[placement_of(p) for p in sorted(placements, key=lambda p: p.destination_name)],
        events=[event_of(entry) for entry in entries],
    )


class AuditBackend(Protocol):
    """Recorded history by content object."""

    async def history(self, content_object_id: ContentObjectId) -> History:
        """The content object's history.

        Raises:
            NotFoundError: the ledger has no such content object.
        """
        ...


def router(backend: Callable[[Request], AuditBackend]) -> APIRouter:
    """The audit endpoints over `backend`."""
    routes = APIRouter(prefix="/content", tags=["audit"])
    uses = Depends(backend)

    @routes.get(
        "/{content_object_id}/history", response_model=History, responses=problems(NOT_FOUND)
    )
    async def get_history(
        content_object_id: ContentObjectId, audit: Annotated[AuditBackend, uses]
    ) -> History:
        """How the content was classified, where it was placed and verified, and retention."""
        return await audit.history(content_object_id)

    return routes


class LedgerAudit:
    """History from the ledger."""

    def __init__(self, engine: AsyncEngine) -> None:
        """Bind the ledger."""
        self._engine = engine

    async def history(self, content_object_id: ContentObjectId) -> History:
        """The content object's history, read in one transaction."""
        async with self._engine.connect() as connection:
            content = await ContentObjectRepository(connection).get(content_object_id)
            if content is None:
                raise NotFoundError("content object", content_object_id)
            placements = await PlacementRepository(connection).find_for_content_object(
                content_object_id
            )
            entries = await AuditRepository(connection).history_for_content_object(
                content_object_id
            )
        return history_of(content, placements, entries)
