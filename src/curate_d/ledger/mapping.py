"""Translation between database rows and domain models.

Split out from the repositories deliberately. The SQL is I/O and needs a database to
exercise; the translation is a decision — which column feeds which field, how a NULL
becomes an absent value, which text becomes which enum — and decisions belong in isolated
tests. A missing enum coercion here reads as a working query right up until it raises in
production.

Every column is narrowed through a helper rather than cast, so a row whose shape does not
match the schema fails naming the column instead of surfacing three frames later as a
validation error about a field the caller never touched.
"""

from collections.abc import Mapping
from datetime import datetime
from typing import Protocol
from uuid import UUID

from curate_d.domain import (
    AuditEntry,
    AuditEntryType,
    ChangeSignal,
    ContentObject,
    ContentObjectId,
    DestinationId,
    DestinationIdentity,
    HashAlgorithm,
    PerceptualFingerprint,
    Placement,
    PlacementId,
    PlacementState,
    Provider,
    SourceObject,
    SourceObjectId,
    SourceObjectState,
    StrongFingerprint,
    VerificationLevel,
)

type ColumnValue = object
type ColumnValues = dict[str, ColumnValue]


class Row(Protocol):
    """A row indexed by column name.

    A protocol rather than a Mapping alias so that both SQLAlchemy's RowMapping, whose
    keys are also Column objects, and a plain dict in a unit test satisfy it.
    """

    def __getitem__(self, column: str, /) -> ColumnValue:
        """Return the value stored under `column`."""
        ...


class LedgerRowError(ValueError):
    """A row did not have the shape the schema promises."""


def _wrong_type(column: str, value: object, expected: str) -> LedgerRowError:
    """Build a `LedgerRowError` naming the column, expected shape, and actual value."""
    return LedgerRowError(
        f"column {column!r} should be {expected}, got {type(value).__name__}: {value!r}"
    )


def text(row: Row, column: str) -> str:
    """Read `column` as a non-null string.

    Raises:
        LedgerRowError: the value isn't a `str`.
    """
    value = row[column]
    if not isinstance(value, str):
        raise _wrong_type(column, value, "text")
    return value


def opt_text(row: Row, column: str) -> str | None:
    """Read `column` as a string, or None if it is NULL."""
    return None if row[column] is None else text(row, column)


def number(row: Row, column: str) -> int:
    """Read `column` as a non-null integer.

    Raises:
        LedgerRowError: the value isn't an `int`, or is a `bool` (an int subclass that
            would silently pass an integer check).
    """
    value = row[column]
    # bool is an int subclass; a boolean column read as a count is always a mistake.
    if isinstance(value, bool) or not isinstance(value, int):
        raise _wrong_type(column, value, "an integer")
    return value


def opt_number(row: Row, column: str) -> int | None:
    """Read `column` as an integer, or None if it is NULL."""
    return None if row[column] is None else number(row, column)


def binary(row: Row, column: str) -> bytes:
    """Read `column` as non-null bytes.

    Raises:
        LedgerRowError: the value isn't `bytes`.
    """
    value = row[column]
    if not isinstance(value, bytes):
        raise _wrong_type(column, value, "bytes")
    return value


def opt_binary(row: Row, column: str) -> bytes | None:
    """Read `column` as bytes, or None if it is NULL."""
    return None if row[column] is None else binary(row, column)


def identifier(row: Row, column: str) -> UUID:
    """Read `column` as a non-null UUID.

    Raises:
        LedgerRowError: the value isn't a `UUID`.
    """
    value = row[column]
    if not isinstance(value, UUID):
        raise _wrong_type(column, value, "a UUID")
    return value


def opt_identifier(row: Row, column: str) -> UUID | None:
    """Read `column` as a UUID, or None if it is NULL."""
    return None if row[column] is None else identifier(row, column)


def moment(row: Row, column: str) -> datetime:
    """Read `column` as a timezone-aware, non-null timestamp.

    Raises:
        LedgerRowError: the value isn't a `datetime`, or lacks timezone info.
    """
    value = row[column]
    if not isinstance(value, datetime):
        raise _wrong_type(column, value, "a timestamp")
    if value.tzinfo is None:
        raise LedgerRowError(f"column {column!r} came back without a timezone")
    return value


def opt_moment(row: Row, column: str) -> datetime | None:
    """Read `column` as a timestamp, or None if it is NULL."""
    return None if row[column] is None else moment(row, column)


# Reported digest columns, in the order they are read back.
_REPORTED_DIGEST_COLUMNS = (
    (HashAlgorithm.SHA1, "reported_sha1"),
    (HashAlgorithm.SHA256, "reported_sha256"),
    (HashAlgorithm.MD5, "reported_md5"),
)


def to_source_object(row: Row) -> SourceObject:
    """Translate a source_objects row into a `SourceObject`."""
    content_object_id = opt_identifier(row, "content_object_id")
    reported = tuple(
        StrongFingerprint(algorithm=algorithm, digest=digest)
        for algorithm, column in _REPORTED_DIGEST_COLUMNS
        if (digest := opt_text(row, column)) is not None
    )
    return SourceObject(
        id=SourceObjectId(identifier(row, "id")),
        provider=Provider(text(row, "provider")),
        account=text(row, "account"),
        native_id=text(row, "native_id"),
        path=text(row, "path"),
        size_bytes=number(row, "size_bytes"),
        modified_at=moment(row, "modified_at"),
        version_marker=opt_text(row, "version_marker"),
        state=SourceObjectState(text(row, "state")),
        content_object_id=(
            None if content_object_id is None else ContentObjectId(content_object_id)
        ),
        change_signal=ChangeSignal(text(row, "change_signal")),
        last_verified_at=opt_moment(row, "last_verified_at"),
        failure_reason=opt_text(row, "failure_reason"),
        reported_digests=reported,
    )


def source_object_values(source: SourceObject) -> ColumnValues:
    """Flatten a `SourceObject` into column values for insert/update."""
    return {
        "id": source.id,
        "provider": source.provider.value,
        "account": source.account,
        "native_id": source.native_id,
        "path": source.path,
        "size_bytes": source.size_bytes,
        "modified_at": source.modified_at,
        "version_marker": source.version_marker,
        "state": source.state.value,
        "change_signal": source.change_signal.value,
        "content_object_id": source.content_object_id,
        "last_verified_at": source.last_verified_at,
        "failure_reason": source.failure_reason,
        **{
            column: (None if (digest := source.reported(algorithm)) is None else digest.digest)
            for algorithm, column in _REPORTED_DIGEST_COLUMNS
        },
    }


def to_content_object(row: Row) -> ContentObject:
    """Translate a content_objects row into a `ContentObject`."""
    perceptual = opt_number(row, "perceptual_fingerprint")
    return ContentObject(
        id=ContentObjectId(identifier(row, "id")),
        fingerprint=StrongFingerprint(
            algorithm=HashAlgorithm(text(row, "fingerprint_algorithm")),
            digest=text(row, "fingerprint_digest"),
        ),
        size_bytes=number(row, "size_bytes"),
        media_type=text(row, "media_type"),
        perceptual_fingerprint=(
            None if perceptual is None else PerceptualFingerprint(value=perceptual)
        ),
    )


def content_object_values(content: ContentObject) -> ColumnValues:
    """Flatten a `ContentObject` into column values for insert/update."""
    return {
        "id": content.id,
        "fingerprint_algorithm": content.fingerprint.algorithm.value,
        "fingerprint_digest": content.fingerprint.digest,
        "size_bytes": content.size_bytes,
        "media_type": content.media_type,
        "perceptual_fingerprint": (
            None if content.perceptual_fingerprint is None else content.perceptual_fingerprint.value
        ),
    }


def to_placement(row: Row) -> Placement:
    """Translate a placements row into a `Placement`."""
    return Placement(
        id=PlacementId(identifier(row, "id")),
        content_object_id=ContentObjectId(identifier(row, "content_object_id")),
        destination_id=DestinationId(identifier(row, "destination_id")),
        destination_name=text(row, "destination_name"),
        locator=text(row, "locator"),
        template_version=text(row, "template_version"),
        state=PlacementState(text(row, "state")),
        verification_level=VerificationLevel(number(row, "verification_level")),
        verified_at=opt_moment(row, "verified_at"),
        failure_reason=opt_text(row, "failure_reason"),
        observed_source_digest=opt_text(row, "observed_source_digest"),
        observed_destination_digest=opt_text(row, "observed_destination_digest"),
        source_object_id=(
            None
            if (source := opt_identifier(row, "source_object_id")) is None
            else SourceObjectId(source)
        ),
        attempts=number(row, "attempts"),
    )


def placement_values(placement: Placement) -> ColumnValues:
    """Flatten a `Placement` into column values for insert/update."""
    return {
        "id": placement.id,
        "content_object_id": placement.content_object_id,
        "destination_id": placement.destination_id,
        "destination_name": placement.destination_name,
        "locator": placement.locator,
        "template_version": placement.template_version,
        "state": placement.state.value,
        "verification_level": int(placement.verification_level),
        "verified_at": placement.verified_at,
        "failure_reason": placement.failure_reason,
        "observed_source_digest": placement.observed_source_digest,
        "observed_destination_digest": placement.observed_destination_digest,
        "source_object_id": placement.source_object_id,
        "attempts": placement.attempts,
    }


def to_destination(row: Row) -> DestinationIdentity:
    """Translate a destinations row into a `DestinationIdentity`."""
    return DestinationIdentity(
        provider=Provider(text(row, "provider")),
        account=text(row, "account"),
        root_path=text(row, "root_path"),
    )


def destination_values(destination: DestinationIdentity) -> ColumnValues:
    """Flatten a `DestinationIdentity` into column values for insert."""
    return {
        "id": destination.id,
        "provider": destination.provider.value,
        "account": destination.account,
        "root_path": destination.root_path,
    }


def to_audit_entry(row: Row) -> AuditEntry:
    """Translate an audit_entries row into an `AuditEntry`.

    Raises:
        LedgerRowError: the `details` column isn't a mapping.
    """
    details = row["details"]
    if not isinstance(details, Mapping):
        raise _wrong_type("details", details, "an object")
    source_object_id = opt_identifier(row, "source_object_id")
    content_object_id = opt_identifier(row, "content_object_id")
    placement_id = opt_identifier(row, "placement_id")
    return AuditEntry(
        entry_type=AuditEntryType(text(row, "entry_type")),
        occurred_at=moment(row, "occurred_at"),
        decided_by=text(row, "decided_by"),
        details=details,
        source_object_id=(None if source_object_id is None else SourceObjectId(source_object_id)),
        content_object_id=(
            None if content_object_id is None else ContentObjectId(content_object_id)
        ),
        placement_id=None if placement_id is None else PlacementId(placement_id),
    )


def audit_entry_values(entry: AuditEntry) -> ColumnValues:
    """Flatten an `AuditEntry` into column values for insert."""
    return {
        "occurred_at": entry.occurred_at,
        "entry_type": entry.entry_type.value,
        "source_object_id": entry.source_object_id,
        "content_object_id": entry.content_object_id,
        "placement_id": entry.placement_id,
        "decided_by": entry.decided_by,
        "details": dict(entry.details),
    }
