"""The applied schema enforces the staging cache index's invariants.

Integration tests because the invariants are CHECK constraints, a primary key and a
unique constraint: a rule the application alone respects is not an invariant. The table
is a cache, but the one-published-object-per-key property is what stops two writers
publishing one staging key, so the database enforces it rather than the coordination
code.
"""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.integration

_WHEN = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
_DIGEST = "a" * 64


async def _insert_staged(
    engine: AsyncEngine, *, staging_key: str | None = None, **overrides: object
) -> None:
    """Insert a staged_objects row with valid defaults, applying any overrides."""
    params: dict[str, object] = {
        "staging_key": staging_key if staging_key is not None else uuid.uuid4().hex,
        "attempt_id": uuid.uuid4(),
        "size_bytes": 1024,
        "sha256": _DIGEST,
        "created_at": _WHEN,
    }
    params.update(overrides)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO staged_objects"
                " (staging_key, attempt_id, size_bytes, sha256, created_at)"
                " VALUES (:staging_key, :attempt_id, :size_bytes, :sha256, :created_at)"
            ),
            params,
        )


async def test_a_staged_object_round_trips(ledger_engine: AsyncEngine) -> None:
    """A valid registration inserts and reads back."""
    key = uuid.uuid4().hex
    await _insert_staged(ledger_engine, staging_key=key)
    async with ledger_engine.connect() as connection:
        result = await connection.execute(
            text("SELECT size_bytes, sha256 FROM staged_objects WHERE staging_key = :key"),
            {"key": key},
        )
        assert result.one() == (1024, _DIGEST)


async def test_one_registration_per_staging_key(ledger_engine: AsyncEngine) -> None:
    """A second registration for a published key is rejected, not silently accepted.

    This is the property that makes a late writer unable to displace the winner.
    """
    key = uuid.uuid4().hex
    await _insert_staged(ledger_engine, staging_key=key)
    with pytest.raises(IntegrityError):
        await _insert_staged(ledger_engine, staging_key=key)


async def test_an_attempt_can_win_only_one_key(ledger_engine: AsyncEngine) -> None:
    """One attempt identifier cannot be registered against two staging keys."""
    attempt = uuid.uuid4()
    await _insert_staged(ledger_engine, attempt_id=attempt)
    with pytest.raises(IntegrityError):
        await _insert_staged(ledger_engine, attempt_id=attempt)


async def test_zero_length_content_is_allowed(ledger_engine: AsyncEngine) -> None:
    """Empty files stage like any other content; the spec requires they work."""
    await _insert_staged(ledger_engine, size_bytes=0)


async def test_negative_size_is_rejected(ledger_engine: AsyncEngine) -> None:
    """A negative byte count would corrupt occupancy accounting."""
    with pytest.raises(IntegrityError):
        await _insert_staged(ledger_engine, size_bytes=-1)


@pytest.mark.parametrize(
    "digest",
    [
        "A" * 64,  # uppercase: would compare unequal to a computed lowercase digest
        "a" * 63,  # truncated
        "a" * 65,  # overlong
        "g" * 64,  # non-hex
        "",
    ],
)
async def test_malformed_digest_is_rejected(ledger_engine: AsyncEngine, digest: str) -> None:
    """Only lowercase 64-character hex is a SHA-256 this code will compare against."""
    with pytest.raises(IntegrityError):
        await _insert_staged(ledger_engine, sha256=digest)


@pytest.mark.parametrize(
    "key",
    [
        str(uuid.uuid4()),  # dashed: would name a second entry for one source object
        uuid.uuid4().hex.upper(),
        "../escape",
        "",
    ],
)
async def test_non_derived_staging_key_is_rejected(ledger_engine: AsyncEngine, key: str) -> None:
    """Keys are derived source object identifiers; anything else is provider-influenced."""
    with pytest.raises(IntegrityError):
        await _insert_staged(ledger_engine, staging_key=key)
