"""Retention decisions.

Retention is an outcome of policy evaluation, never a consequence of a transfer having
finished. Every decision records why, because "why was this original deleted" must be
answerable from the ledger alone.
"""

from enum import StrEnum

from pydantic import AwareDatetime, Field

from curate_d.domain.base import DomainModel
from curate_d.domain.ids import ContentObjectId, SourceObjectId
from curate_d.domain.placement import VerificationLevel


class RetentionOutcome(StrEnum):
    """What policy evaluation concluded about a source."""

    RETAIN = "retain"
    RELEASE = "release"


class RetentionReason(StrEnum):
    """Why the outcome was reached. Distinguishes "not yet" from "never"."""

    DEFAULT_RETAIN = "default_retain"
    """No policy permits release. This is the default and needs no configuration."""

    SOURCE_NEEDED_FOR_COPY_COUNT = "source_needed_for_copy_count"
    """The source is itself one of the copies the policy requires."""

    INSUFFICIENT_VERIFICATION = "insufficient_verification"
    """Enough copies exist, but not enough of them are strongly verified."""

    POLICY_SATISFIED = "policy_satisfied"
    """Release permitted: enough strongly verified copies exist elsewhere."""


class RetentionDecision(DomainModel):
    """The recorded result of evaluating retention policy for one source object."""

    source_object_id: SourceObjectId
    content_object_id: ContentObjectId
    outcome: RetentionOutcome
    reason: RetentionReason
    decided_at: AwareDatetime
    policy_name: str = Field(min_length=1)
    required_copies: int = Field(ge=1)
    qualifying_copies: int = Field(
        ge=0,
        description="Placements meeting the required verification level at decision time.",
    )
    required_verification_level: VerificationLevel = VerificationLevel.STRONG_IN_TRANSIT
