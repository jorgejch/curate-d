"""Typed staging object contracts."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from curate_d.staging.errors import (
    StagingAccessDeniedError,
    StagingError,
    StagingIntegrityError,
    StagingObjectStoreError,
)
from curate_d.staging.keys import StagingKey
from curate_d.staging.objects import StagedObject

_WHEN = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


def _staged(attempt: UUID | None = None) -> StagedObject:
    """A StagedObject with a fixed key and digest, and an optional attempt identifier."""
    return StagedObject(
        key=StagingKey("0" * 32),
        attempt_id=attempt if attempt is not None else uuid4(),
        size_bytes=1024,
        sha256="a" * 64,
        created_at=_WHEN,
    )


def test_object_key_combines_the_staging_key_and_attempt() -> None:
    """The remote name carries the attempt, so two writers never target one object."""
    attempt = uuid4()
    assert _staged(attempt).object_key == f"{'0' * 32}/{attempt.hex}"


def test_competing_attempts_produce_distinct_object_keys() -> None:
    """Two attempts on one staging key write separately; neither overwrites the other."""
    assert _staged().object_key != _staged().object_key


def test_staged_objects_are_frozen() -> None:
    """A registered object is immutable, so nothing can edit a digest after verification."""
    obj = _staged()
    try:
        obj.size_bytes = 2048  # type: ignore[misc]
    except AttributeError:
        return
    raise AssertionError("StagedObject should be frozen")


def test_access_denial_is_an_object_store_failure() -> None:
    """A denial is catchable as a store failure but distinguishable from one."""
    assert issubclass(StagingAccessDeniedError, StagingObjectStoreError)


def test_integrity_failure_is_not_an_object_store_failure() -> None:
    """A digest mismatch is a staging failure, not a transport one: retrying cannot fix it."""
    assert issubclass(StagingIntegrityError, StagingError)
    assert not issubclass(StagingIntegrityError, StagingObjectStoreError)
