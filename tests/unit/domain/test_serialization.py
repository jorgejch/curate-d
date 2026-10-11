"""Round-trip serialization across every domain model.

The registry below is checked against the package by reflection, so adding a model
without a sample fails this suite rather than quietly going untested.
"""

import importlib
import inspect
import pkgutil
from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import SecretStr

import curate_d.domain
from curate_d.domain import (
    SEED_TAXONOMY,
    AuditEntry,
    AuditEntryType,
    ChangeSignal,
    ClassificationResult,
    ClassificationTier,
    ConnectionHealth,
    ConnectionState,
    ContentObject,
    Destination,
    DestinationConfig,
    DomainModel,
    Facts,
    GeoPoint,
    HashAlgorithm,
    MediaKind,
    OAuthTokens,
    PendingFolder,
    PerceptualFingerprint,
    Placement,
    PlacementPolicy,
    PlacementRule,
    PlacementState,
    Provider,
    ProviderConnection,
    RejectedValue,
    RetentionDecision,
    RetentionOutcome,
    RetentionReason,
    RetentionRule,
    ReviewItem,
    ReviewReason,
    ReviewStatus,
    Scan,
    ScanStatus,
    SourceObject,
    SourceObjectState,
    StrongFingerprint,
    TaxonomyField,
    VerificationLevel,
    derive_content_object_id,
    derive_source_object_id,
)

_WHEN = datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC)
_SHA256 = "a" * 64
_FINGERPRINT = StrongFingerprint(algorithm=HashAlgorithm.SHA256, digest=_SHA256)
_CONTENT_ID = derive_content_object_id(str(_FINGERPRINT))
_SCAN_ID = UUID("0b6f2c2e-1c1a-4d3b-9a55-6f2f7c8a2b3d")

_SAMPLES: tuple[DomainModel, ...] = (
    _FINGERPRINT,
    PerceptualFingerprint(value=-42),
    SourceObject(
        id=derive_source_object_id("pcloud", "acct-1", "file-9"),
        provider=Provider.PCLOUD,
        account="acct-1",
        native_id="file-9",
        path="/Photos/2026/img.heic",
        size_bytes=1024,
        modified_at=_WHEN,
        version_marker="rev-3",
        state=SourceObjectState.FINGERPRINTED,
        content_object_id=_CONTENT_ID,
        change_signal=ChangeSignal.VERSION_MARKER,
        last_verified_at=_WHEN,
        failure_reason=None,
        reported_digests=(StrongFingerprint(algorithm=HashAlgorithm.SHA1, digest="c" * 40),),
    ),
    ContentObject(
        id=_CONTENT_ID,
        fingerprint=_FINGERPRINT,
        size_bytes=1024,
        media_type="image/heic",
        perceptual_fingerprint=PerceptualFingerprint(value=7),
    ),
    Destination(
        name="pcloud-organized",
        provider="pcloud",
        account="acct-1",
        root_path="/Organized",
    ),
    Placement.build(
        content_object_id=_CONTENT_ID,
        destination_name="pcloud-organized",
        locator="/Organized/2026/img.heic",
        template_version="v1",
    ).model_copy(
        update={
            "state": PlacementState.PRESENT,
            "verification_level": VerificationLevel.STRONG_COMMON_HASH,
            "verified_at": _WHEN,
            "observed_source_digest": _SHA256,
            "observed_destination_digest": _SHA256,
        }
    ),
    GeoPoint(latitude=48.8584, longitude=2.2945),
    Facts(
        media_kind=MediaKind.PHOTO,
        category="travel",
        subcategory="paris-2026",
        title="Eiffel Tower",
        captured_at=_WHEN,
        place=GeoPoint(latitude=48.8584, longitude=2.2945),
        place_label="paris",
        event_group="cluster-17",
        correspondent=None,
        language="en",
    ),
    ClassificationResult(
        content_object_id=_CONTENT_ID,
        facts=Facts(media_kind=MediaKind.DOCUMENT),
        tier=ClassificationTier.REMOTE_MODEL,
        confidence=0.91,
        produced_at=_WHEN,
        model_id="anthropic/claude-opus-5",
    ),
    AuditEntry(
        entry_type=AuditEntryType.VERIFICATION_FAILED,
        occurred_at=_WHEN,
        decided_by="verification-ladder",
        details={
            "expected": _SHA256,
            "observed": "b" * 64,
            "level": 3,
            "retried": False,
            "note": None,
        },
        source_object_id=None,
        content_object_id=_CONTENT_ID,
        placement_id=None,
    ),
    ProviderConnection(
        provider=Provider.PCLOUD,
        account="acct-1",
        state=ConnectionState.REQUIRES_REAUTHENTICATION,
        api_host="eapi.pcloud.com",
        expires_at=_WHEN,
        refreshable=True,
        last_refreshed_at=_WHEN,
        failure_reason="refresh rejected: invalid_grant",
    ),
    ConnectionHealth(
        provider=Provider.PCLOUD,
        account="acct-1",
        state=ConnectionState.EXPIRING,
        expires_at=_WHEN,
        requires_attention=False,
        detail="refresh due",
    ),
    Scan(
        id=_SCAN_ID,
        provider=Provider.PCLOUD,
        account="acct-1",
        root="/Photos",
        status=ScanStatus.INCOMPLETE,
        failure_reason="pcloud account acct-1: list folder failed: HTTP 503",
        started_at=_WHEN,
        finished_at=_WHEN,
    ),
    PendingFolder(folder_id="d42", path="/Photos/2026"),
    SEED_TAXONOMY,
    PlacementPolicy(
        destinations=(
            DestinationConfig(
                name="archive", provider=Provider.PCLOUD, account="acct-2", root_path="/Organized"
            ),
        ),
        rules=(
            PlacementRule(name="photos", when={"media_kind": "photo"}, destinations=("archive",)),
        ),
        retention=(
            RetentionRule(
                name="photos", when={"media_kind": "photo"}, release_source=True, required_copies=2
            ),
        ),
    ),
    DestinationConfig(name="spare", provider=Provider.PCLOUD, account="a", root_path="/"),
    PlacementRule(name="all", destinations=("spare",)),
    RetentionRule(name="keep"),
    RejectedValue(field=TaxonomyField.SUBCATEGORY, value="Receipts", category="Documents"),
    ReviewItem(
        id=_SCAN_ID,
        content_object_id=_CONTENT_ID,
        reason=ReviewReason.REJECTED_VALUE,
        detail="subcategory 'Receipts' is not in the taxonomy",
        rejected_values=(
            RejectedValue(field=TaxonomyField.SUBCATEGORY, value="Receipts", category="Documents"),
        ),
        status=ReviewStatus.RESOLVED,
        created_at=_WHEN,
        resolved_at=_WHEN,
        resolution="promoted Receipts",
    ),
    RetentionDecision(
        source_object_id=derive_source_object_id("pcloud", "acct-1", "file-9"),
        content_object_id=_CONTENT_ID,
        outcome=RetentionOutcome.RETAIN,
        reason=RetentionReason.INSUFFICIENT_VERIFICATION,
        decided_at=_WHEN,
        policy_name="default",
        required_copies=2,
        qualifying_copies=1,
        required_verification_level=VerificationLevel.STRONG_IN_TRANSIT,
    ),
)


_SECRET_BEARING: tuple[DomainModel, ...] = (
    OAuthTokens(
        access_token=SecretStr("access-token-value"),
        refresh_token=SecretStr("refresh-token-value"),
        expires_at=_WHEN,
        scope="read write",
        token_type="bearer",
    ),
)


def _declared_models() -> set[type[DomainModel]]:
    """Return every concrete DomainModel subclass defined anywhere in curate_d.domain."""
    found: set[type[DomainModel]] = set()
    for module_info in pkgutil.walk_packages(
        curate_d.domain.__path__, prefix=f"{curate_d.domain.__name__}."
    ):
        module = importlib.import_module(module_info.name)
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if issubclass(obj, DomainModel) and obj is not DomainModel:
                found.add(obj)
    return found


def test_every_domain_model_has_a_round_trip_sample() -> None:
    """Every declared DomainModel subclass is covered by a sample in this module."""
    covered = {type(sample) for sample in (*_SAMPLES, *_SECRET_BEARING)}
    missing = _declared_models() - covered
    assert not missing, f"add a round-trip sample for: {sorted(m.__name__ for m in missing)}"


@pytest.mark.parametrize("sample", _SAMPLES, ids=lambda s: type(s).__name__)
def test_model_survives_a_json_round_trip(sample: DomainModel) -> None:
    """A sample survives serialization to JSON and back unchanged."""
    restored = type(sample).model_validate_json(sample.model_dump_json())
    assert restored == sample


@pytest.mark.parametrize("sample", _SECRET_BEARING, ids=lambda s: type(s).__name__)
def test_a_credential_bearing_model_does_not_serialize_its_secrets(sample: DomainModel) -> None:
    """The opposite of a round trip, and deliberately so.

    These models must NOT survive `model_dump_json`: the whole point of SecretStr is that
    the accidental log line, the exception repr, and the API response all emit a
    placeholder. A sample here that round-tripped cleanly would be a credential leak.
    """
    serialized = sample.model_dump_json()
    for _field, value in sample:
        if isinstance(value, SecretStr):
            assert value.get_secret_value() not in serialized
    assert "access-token-value" not in serialized
    assert "access-token-value" not in repr(sample)
    assert "access-token-value" not in str(sample)
