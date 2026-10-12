"""The applied schema enforces the ledger's invariants.

These are integration tests because the invariants live in the database: a CHECK
constraint or a trigger that only the application respects is not an invariant.
"""

import re
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.integration

_WHEN = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


async def _insert_source(
    engine: AsyncEngine, *, native_id: str, account: str = "acct-1", **overrides: object
) -> uuid.UUID:
    """Insert a source_objects row with sensible defaults, returning its id."""
    row_id = uuid.uuid4()
    params: dict[str, object] = {
        "id": row_id,
        "provider": "pcloud",
        "account": account,
        "native_id": native_id,
        "path": f"/{native_id}.jpg",
        "size_bytes": 1024,
        "modified_at": _WHEN,
        "version_marker": "rev-1",
        "state": "discovered",
        "change_signal": "version_marker",
    }
    params.update(overrides)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO source_objects (id, provider, account, native_id, path, size_bytes,"
                " modified_at, version_marker, state, change_signal)"
                " VALUES (:id, :provider, :account, :native_id, :path, :size_bytes,"
                " :modified_at, :version_marker, :state, :change_signal)"
            ),
            params,
        )
    return row_id


async def _insert_content(engine: AsyncEngine, *, digest: str) -> uuid.UUID:
    """Insert a content_objects row for `digest`, returning its id."""
    row_id = uuid.uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO content_objects (id, fingerprint_algorithm, fingerprint_digest,"
                " size_bytes, media_type) VALUES (:id, 'sha256', :digest, 10, 'image/png')"
            ),
            {"id": row_id, "digest": digest},
        )
    return row_id


@pytest.fixture(autouse=True)
async def _clean(ledger_engine: AsyncEngine) -> None:
    """Delete rows left by prior tests so each test starts against empty tables."""
    async with ledger_engine.begin() as connection:
        await connection.execute(text("DELETE FROM placements"))
        await connection.execute(text("DELETE FROM classifications"))
        await connection.execute(text("DELETE FROM review_items"))
        await connection.execute(text("DELETE FROM classification_holds"))
        await connection.execute(text("DELETE FROM source_objects"))
        await connection.execute(text("DELETE FROM content_objects"))


async def test_migrations_leave_no_difference_against_the_model_metadata(
    ledger_engine: AsyncEngine,
) -> None:
    """Hand-written migrations drift from the metadata silently; this catches that."""
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    from curate_d.ledger.tables import metadata

    def _diff(connection: object) -> list[object]:
        """Compare the applied schema against the declared metadata."""
        context = MigrationContext.configure(connection, opts={"compare_type": True})  # type: ignore[arg-type]
        return list(compare_metadata(context, metadata))

    async with ledger_engine.connect() as connection:
        differences = await connection.run_sync(_diff)

    # spatial_ref_sys belongs to PostGIS, not to us.
    unexpected = [d for d in differences if "spatial_ref_sys" not in str(d)]
    assert unexpected == [], f"schema drift: {unexpected}"


async def test_the_migrated_command_request_index_matches_the_model_metadata(
    ledger_engine: AsyncEngine,
) -> None:
    """The applied one-entry-per-request index has the metadata's expression and predicate.

    `compare_metadata` does not compare a partial index's predicate, so the declared index is
    built on a copy of audit_entries in a scratch schema, rolled back, and the two definitions
    PostgreSQL reports are compared.
    """
    from sqlalchemy import MetaData

    from curate_d.ledger.tables import audit_entries

    name = "uq_audit_entries_command_request_id"
    probe_schema = f"probe_{uuid.uuid4().hex}"
    probe = audit_entries.to_metadata(MetaData(), schema=probe_schema)
    async with ledger_engine.connect() as connection:
        await connection.execute(text(f"CREATE SCHEMA {probe_schema}"))
        await connection.run_sync(probe.create)
        rows = (
            await connection.execute(
                text("SELECT schemaname, indexdef FROM pg_indexes WHERE indexname = :name"),
                {"name": name},
            )
        ).all()
        await connection.rollback()

    # pg_get_indexdef names the schema and table before USING; compare what follows.
    definitions = {schema: indexdef.split(" USING ", 1)[1] for schema, indexdef in rows}
    declared = definitions.pop(probe_schema)
    assert list(definitions.values()) == [declared]


async def test_duplicate_provider_account_native_id_is_rejected(
    ledger_engine: AsyncEngine,
) -> None:
    """The provider's own identity for a file must appear exactly once."""
    await _insert_source(ledger_engine, native_id="dup-1")
    with pytest.raises(IntegrityError):
        await _insert_source(ledger_engine, native_id="dup-1")


async def test_the_same_native_id_in_another_account_is_allowed(
    ledger_engine: AsyncEngine,
) -> None:
    """The uniqueness constraint is scoped per account, not global."""
    await _insert_source(ledger_engine, native_id="shared", account="acct-1")
    await _insert_source(ledger_engine, native_id="shared", account="acct-2")


async def test_a_failed_source_object_must_record_why(ledger_engine: AsyncEngine) -> None:
    """A source_objects row in the failed state with no failure_reason is rejected."""
    with pytest.raises(IntegrityError):
        await _insert_source(ledger_engine, native_id="broken", state="failed")


async def test_two_source_objects_with_one_fingerprint_share_a_content_object(
    ledger_engine: AsyncEngine,
) -> None:
    """Deduplication: the same bytes at two paths resolve to one content object."""
    content_id = await _insert_content(ledger_engine, digest="a" * 64)
    first = await _insert_source(ledger_engine, native_id="copy-a")
    second = await _insert_source(ledger_engine, native_id="copy-b")

    async with ledger_engine.begin() as connection:
        await connection.execute(
            text("UPDATE source_objects SET content_object_id = :cid WHERE id IN (:a, :b)"),
            {"cid": content_id, "a": first, "b": second},
        )
        distinct = await connection.scalar(
            text(
                "SELECT count(DISTINCT content_object_id) FROM source_objects WHERE id IN (:a, :b)"
            ),
            {"a": first, "b": second},
        )
    assert distinct == 1


async def test_a_duplicate_content_fingerprint_is_rejected(ledger_engine: AsyncEngine) -> None:
    """Inserting the same fingerprint algorithm and digest twice violates uniqueness."""
    await _insert_content(ledger_engine, digest="b" * 64)
    with pytest.raises(IntegrityError):
        await _insert_content(ledger_engine, digest="b" * 64)


async def test_one_content_object_supports_several_concurrent_placements(
    ledger_engine: AsyncEngine,
) -> None:
    """A content object may have one placement per destination simultaneously."""
    content_id = await _insert_content(ledger_engine, digest="c" * 64)
    async with ledger_engine.begin() as connection:
        for destination in ("archive", "working"):
            await connection.execute(
                text(
                    "INSERT INTO placements (id, content_object_id, destination_name, locator,"
                    " template_version) VALUES (:id, :cid, :dest, :locator, 'v1')"
                ),
                {
                    "id": uuid.uuid4(),
                    "cid": content_id,
                    "dest": destination,
                    "locator": f"/{destination}/x.png",
                },
            )
        count = await connection.scalar(
            text("SELECT count(*) FROM placements WHERE content_object_id = :cid"),
            {"cid": content_id},
        )
    assert count == 2


async def test_a_second_placement_at_the_same_destination_is_rejected(
    ledger_engine: AsyncEngine,
) -> None:
    """A second placements row for the same content object and destination is rejected."""
    content_id = await _insert_content(ledger_engine, digest="d" * 64)
    async with ledger_engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO placements (id, content_object_id, destination_name, locator,"
                " template_version) VALUES (:id, :cid, 'archive', '/a/x.png', 'v1')"
            ),
            {"id": uuid.uuid4(), "cid": content_id},
        )
    with pytest.raises(IntegrityError):
        async with ledger_engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO placements (id, content_object_id, destination_name, locator,"
                    " template_version) VALUES (:id, :cid, 'archive', '/a/other.png', 'v1')"
                ),
                {"id": uuid.uuid4(), "cid": content_id},
            )


async def test_two_content_objects_at_one_locator_are_rejected(
    ledger_engine: AsyncEngine,
) -> None:
    """A destination path belongs to one content object."""
    first = await _insert_content(ledger_engine, digest="e" * 64)
    second = await _insert_content(ledger_engine, digest="f" * 64)
    insert = text(
        "INSERT INTO placements (id, content_object_id, destination_name, locator,"
        " template_version) VALUES (:id, :cid, 'archive', '/a/x.png', 'v1')"
    )
    async with ledger_engine.begin() as connection:
        await connection.execute(insert, {"id": uuid.uuid4(), "cid": first})
    with pytest.raises(IntegrityError):
        async with ledger_engine.begin() as connection:
            await connection.execute(insert, {"id": uuid.uuid4(), "cid": second})


async def test_a_placement_cannot_be_present_without_verification(
    ledger_engine: AsyncEngine,
) -> None:
    """The zero-loss guarantee rests on this: a finished copy is not a checked copy."""
    content_id = await _insert_content(ledger_engine, digest="e" * 64)
    with pytest.raises(IntegrityError):
        async with ledger_engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO placements (id, content_object_id, destination_name, locator,"
                    " template_version, state) VALUES (:id, :cid, 'archive', '/a/x.png',"
                    " 'v1', 'present')"
                ),
                {"id": uuid.uuid4(), "cid": content_id},
            )


async def _insert_audit(engine: AsyncEngine) -> int:
    """Insert a retention_decided audit entry, returning its assigned id."""
    async with engine.begin() as connection:
        entry_id = await connection.scalar(
            text(
                "INSERT INTO audit_entries (entry_type, decided_by, details)"
                " VALUES ('retention_decided', 'policy:default', '{\"outcome\": \"retain\"}')"
                " RETURNING id"
            )
        )
    assert isinstance(entry_id, int)
    return entry_id


async def test_an_audit_entry_cannot_be_updated(ledger_engine: AsyncEngine) -> None:
    """An UPDATE on audit_entries is rejected by the append-only trigger."""
    entry_id = await _insert_audit(ledger_engine)
    with pytest.raises(DBAPIError, match="append-only"):
        async with ledger_engine.begin() as connection:
            await connection.execute(
                text("UPDATE audit_entries SET decided_by = 'someone else' WHERE id = :id"),
                {"id": entry_id},
            )


async def test_an_audit_entry_cannot_be_deleted(ledger_engine: AsyncEngine) -> None:
    """A DELETE on audit_entries is rejected by the append-only trigger."""
    entry_id = await _insert_audit(ledger_engine)
    with pytest.raises(DBAPIError, match="append-only"):
        async with ledger_engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM audit_entries WHERE id = :id"), {"id": entry_id}
            )


async def test_the_audit_table_cannot_be_truncated(ledger_engine: AsyncEngine) -> None:
    """TRUNCATE bypasses row-level triggers, so it needs its own guard."""
    await _insert_audit(ledger_engine)
    with pytest.raises(DBAPIError, match="append-only"):
        async with ledger_engine.begin() as connection:
            await connection.execute(text("TRUNCATE audit_entries"))


async def test_an_unknown_audit_entry_type_is_rejected(ledger_engine: AsyncEngine) -> None:
    """An entry_type outside the CHECK constraint's enumeration is rejected."""
    with pytest.raises(IntegrityError):
        async with ledger_engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO audit_entries (entry_type, decided_by, details)"
                    " VALUES ('made_up', 'test', '{}')"
                )
            )


async def test_the_entry_type_check_allows_exactly_the_audit_entry_types(
    ledger_engine: AsyncEngine,
) -> None:
    """The migrated entry_type CHECK enumerates every AuditEntryType and nothing else."""
    from curate_d.domain.audit import AuditEntryType

    async with ledger_engine.connect() as connection:
        definitions = (
            await connection.scalars(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint"
                    " WHERE conrelid = 'audit_entries'::regclass AND contype = 'c'"
                    " AND conname LIKE '%entry_type_known'"
                )
            )
        ).all()

    assert len(definitions) == 1, definitions
    allowed = set(re.findall(r"'([a-z_]+)'::text", definitions[0]))
    assert allowed == {member.value for member in AuditEntryType}
