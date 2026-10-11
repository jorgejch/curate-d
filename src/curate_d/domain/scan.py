"""Scans: enumerations of a provider subtree, and how far each got."""

from enum import StrEnum
from uuid import UUID

from pydantic import AwareDatetime, Field

from curate_d.domain.base import DomainModel
from curate_d.domain.source import Provider


class ScanStatus(StrEnum):
    """Where one enumeration of a subtree stands."""

    RUNNING = "running"
    INCOMPLETE = "incomplete"
    """Stopped by a provider failure; the next scan of the root resumes it."""

    COMPLETE = "complete"


class Scan(DomainModel):
    """One enumeration of one configured subtree."""

    id: UUID
    provider: Provider
    account: str = Field(min_length=1)
    root: str = Field(min_length=1, description="Configured subtree, as a provider path.")
    status: ScanStatus
    failure_reason: str | None = None
    started_at: AwareDatetime
    finished_at: AwareDatetime | None = None


class PendingFolder(DomainModel):
    """A folder a scan has discovered but not yet listed."""

    folder_id: str = Field(min_length=1)
    path: str = Field(min_length=1)
