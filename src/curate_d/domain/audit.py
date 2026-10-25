"""The immutable audit trail.

Every decision the system acts on is recorded here with the inputs it was based on and
what produced it. "Why is this file in this folder" and "why was this original deleted"
must both be answerable from the ledger alone, months later, without re-running anything.
"""

from collections.abc import Mapping
from enum import StrEnum

from pydantic import AwareDatetime, Field

from curate_d.domain.base import DomainModel
from curate_d.domain.ids import ContentObjectId, PlacementId, SourceObjectId

# Details vary by entry type, so the column is a flat map rather than one model per type.
# Values are restricted to scalars: an audit entry is evidence, and nested structure
# invites storing a payload nobody can query.
type AuditValue = str | int | float | bool | None
type AuditDetails = Mapping[str, AuditValue]


class AuditEntryType(StrEnum):
    """The decisions and actions that must leave a trace."""

    CLASSIFICATION_DECIDED = "classification_decided"
    CLASSIFICATION_REJECTED = "classification_rejected"
    RENDER_FAILED = "render_failed"
    PLACEMENT_STARTED = "placement_started"
    PLACEMENT_REGISTERED = "placement_registered"
    PLACEMENT_FAILED = "placement_failed"
    PLACEMENT_ABSENT = "placement_absent"
    VERIFICATION_SUCCEEDED = "verification_succeeded"
    VERIFICATION_FAILED = "verification_failed"
    RETENTION_DECIDED = "retention_decided"
    SOURCE_READ_FAILED = "source_read_failed"
    SOURCE_DISCREPANCY_DETECTED = "source_discrepancy_detected"


class AuditEntry(DomainModel):
    """One append-only record of something the system decided or did.

    Carries no foreign keys: the most valuable entry is often the one describing work
    whose row was never created.
    """

    entry_type: AuditEntryType
    occurred_at: AwareDatetime
    decided_by: str = Field(
        min_length=1,
        description="Tier, rule, or operator that produced the entry.",
    )
    details: AuditDetails = Field(default_factory=dict)
    source_object_id: SourceObjectId | None = None
    content_object_id: ContentObjectId | None = None
    placement_id: PlacementId | None = None
