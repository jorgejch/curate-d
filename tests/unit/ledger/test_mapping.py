"""Row to domain translation.

Isolated: a dict stands in for the row, because what is under test is the translation,
not the query that produced it.
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from curate_d.domain import (
    AuditEntry,
    AuditEntryType,
    ChangeSignal,
    ContentObject,
    HashAlgorithm,
    PerceptualFingerprint,
    Placement,
    PlacementState,
    Provider,
    SourceObject,
    SourceObjectState,
    StrongFingerprint,
    VerificationLevel,
)
from curate_d.ledger.mapping import (
    ColumnValues,
    LedgerRowError,
    audit_entry_values,
    content_object_values,
    placement_values,
    source_object_values,
    to_audit_entry,
    to_content_object,
    to_placement,
    to_source_object,
)

_WHEN = datetime(2026, 8, 9, 10, 11, tzinfo=UTC)
_SOURCE_ID = uuid4()
_CONTENT_ID = uuid4()
_PLACEMENT_ID = uuid4()


def _source_row(**overrides: object) -> ColumnValues:
    """Build a plausible source_objects row, as a dict standing in for a DB row."""
    row: ColumnValues = {
        "id": _SOURCE_ID,
        "provider": "pcloud",
        "account": "acct-1",
        "native_id": "file-9",
        "path": "/a/b.jpg",
        "size_bytes": 4096,
        "modified_at": _WHEN,
        "version_marker": "rev-3",
        "state": "fingerprinted",
        "content_object_id": _CONTENT_ID,
        "change_signal": "version_marker",
        "last_verified_at": _WHEN,
        "failure_reason": None,
        "reported_sha1": "a" * 40,
        "reported_sha256": None,
        "reported_md5": "b" * 32,
    }
    row.update(overrides)
    return row


def test_a_source_row_becomes_a_source_object() -> None:
    """A source_objects row translates into a SourceObject with matching enums."""
    source = to_source_object(_source_row())
    assert source.provider is Provider.PCLOUD
    assert source.state is SourceObjectState.FINGERPRINTED
    assert source.change_signal is ChangeSignal.VERSION_MARKER
    assert source.content_object_id == _CONTENT_ID


def test_null_columns_become_absent_values_rather_than_empty_strings() -> None:
    """NULL columns map to None, not to sentinel or empty values."""
    source = to_source_object(
        _source_row(version_marker=None, content_object_id=None, last_verified_at=None)
    )
    assert source.version_marker is None
    assert source.content_object_id is None
    assert source.last_verified_at is None


def test_reported_digest_columns_become_digests_with_their_algorithms() -> None:
    """Each non-null digest column is read back under its own algorithm."""
    source = to_source_object(_source_row())
    assert source.reported(HashAlgorithm.SHA1) == StrongFingerprint(
        algorithm=HashAlgorithm.SHA1, digest="a" * 40
    )
    assert source.reported(HashAlgorithm.MD5) is not None
    assert source.reported(HashAlgorithm.SHA256) is None
    values = source_object_values(source)
    assert values["reported_sha1"] == "a" * 40
    assert values["reported_sha256"] is None


def test_a_source_object_round_trips_through_its_column_values() -> None:
    """A SourceObject survives flattening to column values and back."""
    original = to_source_object(_source_row())
    assert to_source_object(source_object_values(original)) == original


def test_a_column_of_the_wrong_type_names_the_column() -> None:
    """A schema drift must not surface three frames later as a field validation error."""
    with pytest.raises(LedgerRowError, match="'path'"):
        to_source_object(_source_row(path=42))


def test_a_boolean_is_not_accepted_where_a_number_belongs() -> None:
    """bool subclasses int, so the obvious isinstance check would let this through."""
    with pytest.raises(LedgerRowError, match="'size_bytes'"):
        to_source_object(_source_row(size_bytes=True))


def test_a_naive_timestamp_is_rejected() -> None:
    """A naive modification time is a silent correctness bug across two providers."""
    with pytest.raises(LedgerRowError, match="timezone"):
        to_source_object(_source_row(modified_at=datetime(2026, 8, 9, 10, 11)))  # noqa: DTZ001


def test_a_non_timestamp_where_a_timestamp_belongs_is_rejected() -> None:
    """A non-datetime value in a timestamp column is rejected, naming the column."""
    with pytest.raises(LedgerRowError, match="'modified_at'"):
        to_source_object(_source_row(modified_at="2026-08-09"))


def test_a_non_uuid_identifier_is_rejected() -> None:
    """A non-UUID value in an identifier column is rejected, naming the column."""
    with pytest.raises(LedgerRowError, match="'id'"):
        to_source_object(_source_row(id="not-a-uuid"))


def test_a_value_outside_the_enum_is_rejected() -> None:
    """A state value outside SourceObjectState's members is rejected."""
    with pytest.raises(ValueError, match="made_up"):
        to_source_object(_source_row(state="made_up"))


def _content_row(**overrides: object) -> ColumnValues:
    """Build a plausible content_objects row, as a dict standing in for a DB row."""
    row: ColumnValues = {
        "id": _CONTENT_ID,
        "fingerprint_algorithm": "sha256",
        "fingerprint_digest": "a" * 64,
        "size_bytes": 10,
        "media_type": "image/png",
        "perceptual_fingerprint": -42,
    }
    row.update(overrides)
    return row


def test_a_content_row_becomes_a_content_object() -> None:
    """A content_objects row translates into a ContentObject with matching fingerprint."""
    content = to_content_object(_content_row())
    assert content.fingerprint == StrongFingerprint(algorithm=HashAlgorithm.SHA256, digest="a" * 64)
    assert content.perceptual_fingerprint == PerceptualFingerprint(value=-42)


def test_an_absent_perceptual_hash_maps_to_none_not_zero() -> None:
    """Zero is a legitimate hash. Confusing it with absence would break dedup."""
    assert to_content_object(_content_row(perceptual_fingerprint=None)).perceptual_fingerprint is (
        None
    )
    zero = to_content_object(_content_row(perceptual_fingerprint=0)).perceptual_fingerprint
    assert zero == PerceptualFingerprint(value=0)


def test_a_content_object_round_trips_through_its_column_values() -> None:
    """A ContentObject survives flattening to column values and back."""
    original: ContentObject = to_content_object(_content_row())
    assert to_content_object(content_object_values(original)) == original


def _placement_row(**overrides: object) -> ColumnValues:
    """Build a plausible placements row, as a dict standing in for a DB row."""
    row: ColumnValues = {
        "id": _PLACEMENT_ID,
        "content_object_id": _CONTENT_ID,
        "destination_name": "archive",
        "locator": "/Archive/x.png",
        "template_version": "v1",
        "state": "present",
        "verification_level": 3,
        "verified_at": _WHEN,
        "failure_reason": None,
        "observed_source_digest": "a" * 64,
        "observed_destination_digest": "a" * 64,
        "source_object_id": _SOURCE_ID,
        "attempts": 2,
    }
    row.update(overrides)
    return row


def test_a_placement_row_becomes_a_placement() -> None:
    """A placements row translates into a Placement with matching enums."""
    placement = to_placement(_placement_row())
    assert placement.state is PlacementState.PRESENT
    assert placement.verification_level is VerificationLevel.STRONG_COMMON_HASH


def test_the_verification_level_ordinal_survives_the_round_trip() -> None:
    """The retention gate compares levels; an off-by-one here would release originals."""
    for level in VerificationLevel:
        original: Placement = to_placement(_placement_row(verification_level=int(level)))
        assert to_placement(placement_values(original)).verification_level is level


def test_an_out_of_range_verification_level_is_rejected() -> None:
    """A verification_level outside VerificationLevel's range is rejected."""
    with pytest.raises(ValueError, match="9"):
        to_placement(_placement_row(verification_level=9))


def _audit_row(**overrides: object) -> ColumnValues:
    """Build a plausible audit_entries row, as a dict standing in for a DB row."""
    row: ColumnValues = {
        "entry_type": "verification_failed",
        "occurred_at": _WHEN,
        "decided_by": "verification-ladder",
        "details": {"expected": "a" * 64, "observed": "b" * 64},
        "source_object_id": None,
        "content_object_id": _CONTENT_ID,
        "placement_id": _PLACEMENT_ID,
    }
    row.update(overrides)
    return row


def test_an_audit_row_becomes_an_audit_entry() -> None:
    """An audit_entries row translates into an AuditEntry with matching fields."""
    entry = to_audit_entry(_audit_row())
    assert entry.entry_type is AuditEntryType.VERIFICATION_FAILED
    assert entry.source_object_id is None
    assert entry.placement_id == _PLACEMENT_ID


def test_an_audit_entry_round_trips_through_its_column_values() -> None:
    """An AuditEntry survives flattening to column values and back."""
    original: AuditEntry = to_audit_entry(_audit_row())
    assert to_audit_entry(audit_entry_values(original)) == original


def test_audit_details_that_are_not_an_object_are_rejected() -> None:
    """A non-mapping `details` column is rejected."""
    with pytest.raises(LedgerRowError, match="details"):
        to_audit_entry(_audit_row(details="not an object"))


def test_the_mappers_agree_on_the_domain_builders() -> None:
    """The builders derive identifiers; the mappers must not invent different ones."""
    built: SourceObject = SourceObject.build(
        provider=Provider.PCLOUD,
        account="acct-1",
        native_id="file-9",
        path="/a/b.jpg",
        size_bytes=4096,
        modified_at=_WHEN,
        version_marker="rev-3",
    )
    assert to_source_object(source_object_values(built)) == built
