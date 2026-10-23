"""Core domain types.

Three identity layers — source object, content object, placement — plus the facts that
drive rendering and the retention decision that governs source release. Everything here
is a frozen, strictly validated value object with no untyped containers.
"""

from curate_d.domain.audit import (
    AuditDetails,
    AuditEntry,
    AuditEntryType,
    AuditValue,
)
from curate_d.domain.base import DomainModel, StaleCheckpointError
from curate_d.domain.conditions import (
    CONDITION_FIELDS,
    check_conditions,
    matches,
    orphaned,
    taxonomy_in,
)
from curate_d.domain.configuration import ConfigurationKind
from curate_d.domain.connection import (
    ConnectionHealth,
    ConnectionState,
    OAuthTokens,
    ProviderConnection,
)
from curate_d.domain.content import ContentObject
from curate_d.domain.facts import (
    ClassificationResult,
    ClassificationTier,
    Facts,
    GeoPoint,
    MediaKind,
)
from curate_d.domain.fingerprint import (
    PERCEPTUAL_HASH_BITS,
    HashAlgorithm,
    PerceptualFingerprint,
    StrongFingerprint,
)
from curate_d.domain.ids import (
    CURATE_D_NAMESPACE,
    ContentObjectId,
    PlacementId,
    SourceObjectId,
    derive_content_object_id,
    derive_placement_id,
    derive_source_object_id,
)
from curate_d.domain.placement import (
    Destination,
    DryRunOutcome,
    Placement,
    PlacementState,
    PolicySource,
    VerificationLevel,
)
from curate_d.domain.policy import (
    SEED_POLICY,
    CapabilityProfileName,
    DestinationConfig,
    PlacementPolicy,
    PlacementRule,
    RetentionRule,
)
from curate_d.domain.retention import (
    RetentionDecision,
    RetentionOutcome,
    RetentionReason,
)
from curate_d.domain.review import (
    RejectedValue,
    ReviewItem,
    ReviewReason,
    ReviewStatus,
)
from curate_d.domain.scan import PendingFolder, Scan, ScanStatus
from curate_d.domain.source import (
    ChangeSignal,
    Provider,
    SourceObject,
    SourceObjectState,
)
from curate_d.domain.taxonomy import SEED_TAXONOMY, Taxonomy, TaxonomyField

__all__ = [
    "CONDITION_FIELDS",
    "CURATE_D_NAMESPACE",
    "PERCEPTUAL_HASH_BITS",
    "SEED_POLICY",
    "SEED_TAXONOMY",
    "AuditDetails",
    "AuditEntry",
    "AuditEntryType",
    "AuditValue",
    "CapabilityProfileName",
    "ChangeSignal",
    "ClassificationResult",
    "ClassificationTier",
    "ConfigurationKind",
    "ConnectionHealth",
    "ConnectionState",
    "ContentObject",
    "ContentObjectId",
    "Destination",
    "DestinationConfig",
    "DomainModel",
    "DryRunOutcome",
    "Facts",
    "GeoPoint",
    "HashAlgorithm",
    "MediaKind",
    "OAuthTokens",
    "PendingFolder",
    "PerceptualFingerprint",
    "Placement",
    "PlacementId",
    "PlacementPolicy",
    "PlacementRule",
    "PlacementState",
    "PolicySource",
    "Provider",
    "ProviderConnection",
    "RejectedValue",
    "RetentionDecision",
    "RetentionOutcome",
    "RetentionReason",
    "RetentionRule",
    "ReviewItem",
    "ReviewReason",
    "ReviewStatus",
    "Scan",
    "ScanStatus",
    "SourceObject",
    "SourceObjectId",
    "SourceObjectState",
    "StaleCheckpointError",
    "StrongFingerprint",
    "Taxonomy",
    "TaxonomyField",
    "VerificationLevel",
    "check_conditions",
    "derive_content_object_id",
    "derive_placement_id",
    "derive_source_object_id",
    "matches",
    "orphaned",
    "taxonomy_in",
]
