"""Baseline: the ledger schema as of the squash, built in one revision.

Replaces the chain 0001_source_objects ... 0019_command_completed_audit (last present at
commit 4ad75ee) before the first production deploy. The chain is append-only from here: a
schema change is a new revision on top of this one, never an edit to it.

The schema is the chain's end state, column for column in the chain's physical order:
columns the chain added later sit last in their table, in the order it added them, so
`pg_dump --schema-only` of a database built here matches one built by the old chain. The
old chain's DBOS queue-row updates (0008, 0009) are gone: on an empty database
`runtime/queues.py` writes those rows from the current specs. DBOS's own tables are not
created here.

Three views read `dbos.workflow_status` and `dbos.workflow_output`, so the DBOS system
schema must exist first; `migrations/env.py` brings it up to date before an online run.

Nothing here is imported from `curate_d`: a revision is frozen history, and a later edit
to the application must not change what this builds on a fresh database. The
one-entry-per-request index's expression and predicate are spelled out rather than taken
from `curate_d.ledger.tables`, and the integration tests compare the built index with the
metadata's. The append-only trigger and its function are defined here only.

Check constraint names are the ones the old chain built, given whole through `op.f()` so
the naming convention in `curate_d.ledger.tables` does not touch them. The chain passed
full names through that convention, which prefixed them a second time
(`ck_audit_entries_ck_audit_entries_entry_type_known`) and shortened the two longest with a
hash suffix. No application code names a check constraint; the names are kept so the
baseline's schema is the chain's.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-10-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Kept in sync with curate_d.runtime.dbos_app.SYSTEM_SCHEMA.
SYSTEM_SCHEMA = "dbos"

_AUDIT_ENTRY_TYPES = (
    "'classification_decided', 'classification_rejected', 'render_failed', "
    "'placement_started', 'placement_registered', 'placement_failed', 'placement_absent', "
    "'verification_succeeded', 'verification_failed', 'retention_decided', "
    "'source_read_failed', 'source_discrepancy_detected', 'workflow_command_issued', "
    "'workflow_command_completed'"
)

# Enforced in the database rather than the application: a bug, a migration, or a person
# with psql must all be unable to rewrite history.
APPEND_ONLY_FUNCTION = """
CREATE OR REPLACE FUNCTION curate_d_reject_audit_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION
        'audit_entries is append-only; % rejected', TG_OP
        USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql;
"""

APPEND_ONLY_TRIGGER = """
CREATE TRIGGER audit_entries_append_only
BEFORE UPDATE OR DELETE ON audit_entries
FOR EACH ROW EXECUTE FUNCTION curate_d_reject_audit_mutation();
"""

# TRUNCATE bypasses row-level triggers, so it needs its own statement-level one.
APPEND_ONLY_TRUNCATE_TRIGGER = """
CREATE TRIGGER audit_entries_no_truncate
BEFORE TRUNCATE ON audit_entries
FOR EACH STATEMENT EXECUTE FUNCTION curate_d_reject_audit_mutation();
"""

DROP_APPEND_ONLY = """
DROP TRIGGER IF EXISTS audit_entries_no_truncate ON audit_entries;
DROP TRIGGER IF EXISTS audit_entries_append_only ON audit_entries;
DROP FUNCTION IF EXISTS curate_d_reject_audit_mutation();
"""

# At most one operator-command entry per Conductor request. Nothing writes these entries
# since Conductor became read-only, but the metadata still declares the index.
COMMAND_REQUEST_ID = "(details ->> 'request_id')"
COMMAND_ENTRIES = "entry_type = 'workflow_command_issued'"

# The views answer "what is the system doing" and "what is stuck" from psql. They read
# DBOS's own tables, so a DBOS upgrade that moves a column is a one-file migration rather
# than a hunt through the control plane.
WORKFLOW_STATUS_BY_QUEUE = f"""
CREATE OR REPLACE VIEW workflow_status_by_queue AS
SELECT
    coalesce(queue_name, '(unqueued)') AS queue_name,
    -- Provider-facing queues are partitioned on provider identity, so a backlog is a
    -- per-partition fact. Aggregating it away would hide which provider is stalled.
    coalesce(queue_partition_key, '(unpartitioned)') AS partition_key,
    status,
    application_version,
    count(*) AS workflow_count,
    min(to_timestamp(created_at / 1000.0)) AS oldest_created_at,
    max(to_timestamp(updated_at / 1000.0)) AS newest_updated_at
FROM {SYSTEM_SCHEMA}.workflow_status
GROUP BY 1, 2, 3, 4;
"""

# Work that has been claimed but is not progressing. The application_version column is
# here on purpose: a workflow parked on a retired version needs a worker on that version
# to resume, and that is invisible in a plain count by state.
STALLED_WORKFLOWS = f"""
CREATE OR REPLACE VIEW stalled_workflows AS
SELECT
    workflow_uuid AS workflow_id,
    name AS workflow_name,
    coalesce(queue_name, '(unqueued)') AS queue_name,
    coalesce(queue_partition_key, '(unpartitioned)') AS partition_key,
    status,
    application_version,
    executor_id,
    recovery_attempts,
    to_timestamp(created_at / 1000.0) AS created_at,
    to_timestamp(updated_at / 1000.0) AS updated_at
FROM {SYSTEM_SCHEMA}.workflow_status
WHERE status IN ('PENDING', 'ENQUEUED')
ORDER BY updated_at;
"""

# DBOS 3 keeps outputs and errors in workflow_output; rows DBOS 2 wrote keep theirs in
# workflow_status.error, so the new column comes first and the old one is the fallback,
# matching how DBOS reads a result itself. The payload is a serialized exception, to be
# decoded through the DBOS API rather than shown to a person.
FAILED_WORKFLOWS = f"""
CREATE OR REPLACE VIEW failed_workflows AS
SELECT
    status.workflow_uuid AS workflow_id,
    status.name AS workflow_name,
    coalesce(status.queue_name, '(unqueued)') AS queue_name,
    status.application_version,
    status.recovery_attempts,
    to_timestamp(status.updated_at / 1000.0) AS failed_at,
    coalesce(output.error, status.error) AS error_payload
FROM {SYSTEM_SCHEMA}.workflow_status AS status
LEFT JOIN {SYSTEM_SCHEMA}.workflow_output AS output
    ON output.workflow_uuid = status.workflow_uuid
WHERE status.status IN ('ERROR', 'CANCELLED', 'MAX_RECOVERY_ATTEMPTS_EXCEEDED')
ORDER BY status.updated_at DESC;
"""

_VIEWS = ("failed_workflows", "stalled_workflows", "workflow_status_by_queue")


def _now() -> sa.TextClause:
    """Return the `now()` server default."""
    return sa.text("now()")


def _create_identity_tables() -> None:
    """Create content_objects, source_objects and placements."""
    op.create_table(
        "content_objects",
        sa.Column("id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("fingerprint_algorithm", sa.Text(), nullable=False),
        sa.Column("fingerprint_digest", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("media_type", sa.Text(), nullable=False),
        sa.Column(
            "perceptual_fingerprint",
            sa.BigInteger(),
            nullable=True,
            comment="64-bit image hash, stored signed. Compared by bit distance, not equality, "
            "so it is deliberately not indexed for equality lookup.",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.CheckConstraint(
            "fingerprint_algorithm IN ('sha256', 'sha1', 'md5')",
            name=op.f("ck_content_objects_ck_content_objects_fingerprint_algor_46b2"),
        ),
        sa.CheckConstraint(
            "fingerprint_digest ~ '^[0-9a-f]+$'",
            name=op.f("ck_content_objects_ck_content_objects_fingerprint_digest_is_hex"),
        ),
        sa.CheckConstraint(
            "media_type ~ '^[a-z0-9][^/]*/[a-z0-9][^/]*$'",
            name=op.f("ck_content_objects_ck_content_objects_media_type_shape"),
        ),
        sa.CheckConstraint(
            "size_bytes >= 0", name=op.f("ck_content_objects_ck_content_objects_size_non_negative")
        ),
        sa.PrimaryKeyConstraint("id", name="pk_content_objects"),
        sa.UniqueConstraint(
            "fingerprint_algorithm",
            "fingerprint_digest",
            name="uq_content_objects_fingerprint_algorithm_fingerprint_digest",
        ),
    )

    op.create_table(
        "source_objects",
        sa.Column("id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("account", sa.Text(), nullable=False),
        sa.Column(
            "native_id",
            sa.Text(),
            nullable=False,
            comment="Provider-assigned file identifier; stable across renames within a provider.",
        ),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("modified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "version_marker",
            sa.Text(),
            nullable=True,
            comment="Provider change token. NULL where the provider supplies none.",
        ),
        sa.Column("state", sa.Text(), server_default="discovered", nullable=False),
        sa.Column("change_signal", sa.Text(), nullable=False),
        sa.Column(
            "last_verified_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When a full rescan last confirmed the recorded fingerprint. NULL means "
            "never re-examined, which is where a resumed full rescan picks up.",
        ),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column(
            "first_seen_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column(
            "content_object_id",
            sa.UUID(as_uuid=True),
            nullable=True,
            comment="Set once the file's content has been fingerprinted.",
        ),
        sa.Column(
            "reported_sha1",
            sa.Text(),
            nullable=True,
            comment="SHA-1 the provider reports, from its metadata rather than a content read.",
        ),
        sa.Column("reported_sha256", sa.Text(), nullable=True),
        sa.Column("reported_md5", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "change_signal IN ('version_marker', 'size_and_modified_time', 'content_fingerprint')",
            name=op.f("ck_source_objects_ck_source_objects_change_signal_known"),
        ),
        sa.CheckConstraint(
            "provider IN ('pcloud')",
            name=op.f("ck_source_objects_ck_source_objects_provider_known"),
        ),
        sa.CheckConstraint(
            "size_bytes >= 0", name=op.f("ck_source_objects_ck_source_objects_size_non_negative")
        ),
        sa.CheckConstraint(
            "state <> 'failed' OR failure_reason IS NOT NULL",
            name=op.f("ck_source_objects_ck_source_objects_failure_has_reason"),
        ),
        sa.CheckConstraint(
            "state IN ('discovered', 'fingerprinted', 'failed', 'excluded')",
            name=op.f("ck_source_objects_ck_source_objects_state_known"),
        ),
        sa.CheckConstraint(
            "reported_sha1 ~ '^[0-9a-f]{40}$'",
            name=op.f("ck_source_objects_ck_source_objects_reported_sha1_shape"),
        ),
        sa.CheckConstraint(
            "reported_sha256 ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_source_objects_ck_source_objects_reported_sha256_shape"),
        ),
        sa.CheckConstraint(
            "reported_md5 ~ '^[0-9a-f]{32}$'",
            name=op.f("ck_source_objects_ck_source_objects_reported_md5_shape"),
        ),
        # RESTRICT rather than CASCADE: content is never deleted out from under a source
        # object that still points at it, and a delete that would orphan one is a bug worth
        # surfacing rather than absorbing.
        sa.ForeignKeyConstraint(
            ["content_object_id"],
            ["content_objects.id"],
            name="fk_source_objects_content_object_id_content_objects",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_source_objects"),
        sa.UniqueConstraint(
            "provider",
            "account",
            "native_id",
            name="uq_source_objects_provider_account_native_id",
        ),
    )
    op.create_index("ix_source_objects_last_verified_at", "source_objects", ["last_verified_at"])
    op.create_index(
        "ix_source_objects_provider_account_path", "source_objects", ["provider", "account", "path"]
    )
    op.create_index("ix_source_objects_content_object_id", "source_objects", ["content_object_id"])

    op.create_table(
        "placements",
        sa.Column("id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("content_object_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("destination_name", sa.Text(), nullable=False),
        sa.Column("locator", sa.Text(), nullable=False),
        sa.Column("template_version", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), server_default="pending", nullable=False),
        sa.Column(
            "verification_level",
            sa.SmallInteger(),
            server_default="0",
            nullable=False,
            comment="VerificationLevel ordinal. Ordered so a policy can require 'at least'.",
        ),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("observed_source_digest", sa.Text(), nullable=True),
        sa.Column("observed_destination_digest", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column(
            "source_object_id",
            sa.UUID(as_uuid=True),
            nullable=True,
            comment="The source copied by the latest attempt.",
        ),
        sa.Column(
            "attempts",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
            comment="Attempts after the first; part of the next attempt's workflow identity.",
        ),
        sa.Column(
            "evidence",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="Both sides' observed values from the latest copy, recorded before "
            "verification. Validated by the application.",
        ),
        sa.CheckConstraint(
            "state <> 'present' OR verified_at IS NOT NULL",
            name=op.f("ck_placements_ck_placements_present_implies_verified"),
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'copying', 'verifying', 'present', 'absent', 'failed', "
            "'untrusted')",
            name=op.f("ck_placements_ck_placements_state_known"),
        ),
        sa.CheckConstraint(
            "state NOT IN ('failed', 'untrusted') OR failure_reason IS NOT NULL",
            name=op.f("ck_placements_ck_placements_failure_has_reason"),
        ),
        sa.CheckConstraint(
            "verification_level BETWEEN 0 AND 3",
            name=op.f("ck_placements_ck_placements_verification_level_in_range"),
        ),
        sa.CheckConstraint(
            "attempts >= 0", name=op.f("ck_placements_ck_placements_attempts_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["content_object_id"],
            ["content_objects.id"],
            name="fk_placements_content_object_id_content_objects",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_object_id"],
            ["source_objects.id"],
            name="fk_placements_source_object_id_source_objects",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_placements"),
        sa.UniqueConstraint(
            "content_object_id",
            "destination_name",
            name="uq_placements_content_object_id_destination_name",
        ),
        # One content object per locator at a destination: placement reserves the locator
        # when a copy starts, and this is the database's backstop for that.
        sa.UniqueConstraint(
            "destination_name", "locator", name="uq_placements_destination_name_locator"
        ),
    )
    op.create_index("ix_placements_state", "placements", ["state"])


def _create_audit_entries() -> None:
    """Create audit_entries, its indexes and the append-only enforcement triggers."""
    op.create_table(
        "audit_entries",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("entry_type", sa.Text(), nullable=False),
        sa.Column("source_object_id", sa.UUID(as_uuid=True), nullable=True),
        sa.Column("content_object_id", sa.UUID(as_uuid=True), nullable=True),
        sa.Column("placement_id", sa.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "decided_by",
            sa.Text(),
            nullable=False,
            comment="Which tier, rule, or operator produced the entry.",
        ),
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            comment="Serialized AuditDetails. Validated by the application before write; the "
            "column is schemaless only in the sense that entry types differ in shape.",
        ),
        sa.CheckConstraint(
            f"entry_type IN ({_AUDIT_ENTRY_TYPES})",
            name=op.f("ck_audit_entries_ck_audit_entries_entry_type_known"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_audit_entries"),
        comment="Append-only. Updates and deletes are rejected by trigger, not by convention.",
    )
    op.create_index(
        "ix_audit_entries_content_object_id_occurred_at",
        "audit_entries",
        ["content_object_id", "occurred_at"],
    )
    op.create_index(
        "ix_audit_entries_entry_type_occurred_at", "audit_entries", ["entry_type", "occurred_at"]
    )
    op.create_index(
        "uq_audit_entries_command_request_id",
        "audit_entries",
        [sa.text(COMMAND_REQUEST_ID)],
        unique=True,
        postgresql_where=sa.text(COMMAND_ENTRIES),
    )
    op.execute(APPEND_ONLY_FUNCTION)
    op.execute(APPEND_ONLY_TRIGGER)
    op.execute(APPEND_ONLY_TRUNCATE_TRIGGER)


def _create_staging_and_connection_tables() -> None:
    """Create staged_objects and provider_connections."""
    op.create_table(
        "staged_objects",
        sa.Column(
            "staging_key",
            sa.Text(),
            nullable=False,
            comment="Source-derived staging key. One published object per key is the "
            "invariant, so it is the primary key rather than a surrogate.",
        ),
        sa.Column(
            "attempt_id",
            sa.UUID(as_uuid=True),
            nullable=False,
            comment="Upload attempt that won this key. Part of the remote object name, so a "
            "losing writer's bytes are a collectable orphan rather than an overwrite.",
        ),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column(
            "sha256",
            sa.Text(),
            nullable=False,
            comment="Computed by the writer after upload and reverified on every download. "
            "Not an ETag: a multipart ETag is not a content hash.",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=_now(),
            nullable=False,
            comment="Creation time of the remote object, copied from the verified upload's "
            "LastModified. Not the row's insertion time, which is only what the server "
            "default supplies; age-based eviction reads the same value from the S3 listing.",
        ),
        sa.CheckConstraint(
            "size_bytes >= 0", name=op.f("ck_staged_objects_ck_staged_objects_size_non_negative")
        ),
        sa.CheckConstraint(
            "sha256 ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_staged_objects_ck_staged_objects_sha256_lowercase_hex"),
        ),
        sa.CheckConstraint(
            "staging_key ~ '^[0-9a-f]{32}$'",
            name=op.f("ck_staged_objects_ck_staged_objects_key_is_derived_identifier"),
        ),
        sa.PrimaryKeyConstraint("staging_key", name="pk_staged_objects"),
        sa.UniqueConstraint("attempt_id", name="uq_staged_objects_attempt_id"),
    )

    # Tokens are stored as Fernet ciphertext; the key comes from the environment and is
    # never written here.
    op.create_table(
        "provider_connections",
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("account", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), server_default="healthy", nullable=False),
        sa.Column(
            "api_host",
            sa.Text(),
            nullable=True,
            comment="Regional API endpoint serving the account, as the provider reported it.",
        ),
        sa.Column(
            "access_token_ciphertext",
            sa.LargeBinary(),
            nullable=False,
            comment="Fernet ciphertext. The key lives in the environment, never in this "
            "database, so a dump of this table is not a credential leak.",
        ),
        sa.Column("refresh_token_ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("token_type", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=True),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Absolute access-token expiry. NULL where the provider declared none.",
        ),
        sa.Column("last_refreshed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.CheckConstraint(
            "provider IN ('pcloud')",
            name=op.f("ck_provider_connections_ck_provider_connections_provider_known"),
        ),
        sa.CheckConstraint(
            "state IN ('healthy', 'expiring', 'requires_reauthentication')",
            name=op.f("ck_provider_connections_ck_provider_connections_state_known"),
        ),
        sa.CheckConstraint(
            "state <> 'requires_reauthentication' OR failure_reason IS NOT NULL",
            name=op.f("ck_provider_connections_ck_provider_connections_reauthe_45fa"),
        ),
        sa.PrimaryKeyConstraint("provider", "account", name="pk_provider_connections"),
    )


def _create_scan_tables() -> None:
    """Create scans and scan_folders."""
    op.create_table(
        "scans",
        sa.Column("id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("account", sa.Text(), nullable=False),
        sa.Column(
            "root",
            sa.Text(),
            nullable=False,
            comment="Configured subtree, as a provider path. One open scan per root at a time.",
        ),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('running', 'incomplete', 'complete')",
            name=op.f("ck_scans_ck_scans_status_known"),
        ),
        sa.CheckConstraint(
            "status <> 'incomplete' OR failure_reason IS NOT NULL",
            name=op.f("ck_scans_ck_scans_incomplete_has_reason"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_scans"),
        comment="One enumeration of a subtree. An incomplete scan is resumed by the next one "
        "rather than restarted, from the folders it had not yet listed.",
    )
    op.create_index(
        "uq_scans_open_root",
        "scans",
        ["provider", "account", "root"],
        unique=True,
        postgresql_where=sa.text("status <> 'complete'"),
    )

    op.create_table(
        "scan_folders",
        sa.Column("scan_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "folder_id", sa.Text(), nullable=False, comment="Provider-native folder identifier."
        ),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column(
            "listed_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Set once every file in the folder is recorded. NULL is the frontier.",
        ),
        sa.ForeignKeyConstraint(
            ["scan_id"],
            ["scans.id"],
            name="fk_scan_folders_scan_id_scans",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("scan_id", "folder_id", name="pk_scan_folders"),
    )


def _create_classification_tables() -> None:
    """Create configurations, classifications, review_items, remote_spend and holds."""
    op.create_table(
        "configurations",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("kind", sa.Text(), nullable=False, comment="taxonomy, templates or policy."),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "document",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            comment="The validated configuration document for this version.",
        ),
        sa.Column("active", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('taxonomy', 'templates', 'policy')",
            name=op.f("ck_configurations_ck_configurations_kind_known"),
        ),
        sa.CheckConstraint(
            "version >= 1", name=op.f("ck_configurations_ck_configurations_version_positive")
        ),
        sa.PrimaryKeyConstraint("id", name="pk_configurations"),
        sa.UniqueConstraint("kind", "version", name="uq_configurations_kind_version"),
        comment="Every configuration version ever activated. Changing configuration adds a "
        "version; the previous one stays, so a rejected change leaves it in force.",
    )
    op.create_index(
        "uq_configurations_active_kind",
        "configurations",
        ["kind"],
        unique=True,
        postgresql_where=sa.text("active"),
    )

    op.create_table(
        "classifications",
        sa.Column(
            "content_object_id",
            sa.UUID(as_uuid=True),
            nullable=False,
            comment="One classification per content fingerprint: repeat content reuses it.",
        ),
        sa.Column("tier", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("model_id", sa.Text(), nullable=True),
        sa.Column("taxonomy_version", sa.Integer(), nullable=True),
        sa.Column("produced_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("media_kind", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=True),
        sa.Column("subcategory", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("latitude", sa.Float(), nullable=True),
        sa.Column("longitude", sa.Float(), nullable=True),
        sa.Column("place_label", sa.Text(), nullable=True),
        sa.Column("event_group", sa.Text(), nullable=True),
        sa.Column("correspondent", sa.Text(), nullable=True),
        sa.Column("language", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "tier IN ('deterministic', 'local_inference', 'remote_model', 'human_review')",
            name=op.f("ck_classifications_ck_classifications_tier_known"),
        ),
        sa.CheckConstraint(
            "media_kind IN ('document', 'photo', 'video', 'audio', 'archive', 'other')",
            name=op.f("ck_classifications_ck_classifications_media_kind_known"),
        ),
        sa.CheckConstraint(
            "confidence BETWEEN 0 AND 1",
            name=op.f("ck_classifications_ck_classifications_confidence_in_range"),
        ),
        sa.CheckConstraint(
            "(latitude IS NULL) = (longitude IS NULL)",
            name=op.f("ck_classifications_ck_classifications_place_complete"),
        ),
        sa.ForeignKeyConstraint(
            ["content_object_id"],
            ["content_objects.id"],
            name="fk_classifications_content_object_id_content_objects",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("content_object_id", name="pk_classifications"),
        comment="Facts per content object. Columns rather than a document, so facts can be "
        "queried and clustered in place.",
    )
    op.create_index("ix_classifications_captured_at", "classifications", ["captured_at"])

    op.create_table(
        "review_items",
        sa.Column("id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("content_object_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column(
            "rejected_values",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
            comment="Serialized RejectedValue list, validated by the application.",
        ),
        sa.Column("status", sa.Text(), server_default="open", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "reason IN ('low_confidence', 'rejected_value', 'malformed_output', 'render_failed')",
            name=op.f("ck_review_items_ck_review_items_reason_known"),
        ),
        sa.CheckConstraint(
            "status IN ('open', 'resolved', 'excluded')",
            name=op.f("ck_review_items_ck_review_items_status_known"),
        ),
        sa.CheckConstraint(
            "status = 'open' OR resolved_at IS NOT NULL",
            name=op.f("ck_review_items_ck_review_items_closed_has_time"),
        ),
        sa.ForeignKeyConstraint(
            ["content_object_id"],
            ["content_objects.id"],
            name="fk_review_items_content_object_id_content_objects",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_review_items"),
    )
    op.create_index(
        "uq_review_items_open_content",
        "review_items",
        ["content_object_id"],
        unique=True,
        postgresql_where=sa.text("status = 'open'"),
    )

    op.create_table(
        "remote_spend",
        sa.Column("period", sa.Text(), nullable=False, comment="Calendar month, YYYY-MM, in UTC."),
        sa.Column("spent_usd", sa.Numeric(14, 6), server_default=sa.text("0"), nullable=False),
        sa.Column("calls", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("cap_usd", sa.Numeric(14, 6), nullable=False),
        sa.Column(
            "cap_reached_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When the period's spend first reached its cap; reported by the control plane.",
        ),
        sa.CheckConstraint(
            "spent_usd >= 0", name=op.f("ck_remote_spend_ck_remote_spend_spent_non_negative")
        ),
        sa.PrimaryKeyConstraint("period", name="pk_remote_spend"),
    )

    op.create_table(
        "classification_holds",
        sa.Column("content_object_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "period", sa.Text(), nullable=False, comment="The spend period whose cap held it."
        ),
        sa.Column("held_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "request",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            comment="The prepared remote request and base facts, so the retry needs no staged "
            "content. Validated by the application.",
        ),
        sa.ForeignKeyConstraint(
            ["content_object_id"],
            ["content_objects.id"],
            name="fk_classification_holds_content_object_id_content_objects",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("content_object_id", name="pk_classification_holds"),
        comment="Content waiting for the next spend period rather than being misclassified.",
    )


def _create_placement_run_tables() -> None:
    """Create dry_runs, dry_run_items and destination_checks."""
    op.create_table(
        "dry_runs",
        sa.Column("id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("template_version", sa.Text(), nullable=False),
        sa.Column("taxonomy_version", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_dry_runs"),
        comment="One evaluation of what placement would do, persisted for export.",
    )
    op.create_table(
        "dry_run_items",
        sa.Column("run_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("content_object_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "destination_name",
            sa.Text(),
            nullable=False,
            comment="Empty for content that did not reach rendering.",
        ),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("paths", sa.Text(), nullable=False, comment="The content's source paths."),
        sa.Column(
            "facts",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="Derived facts, validated by the application.",
        ),
        sa.Column("locator", sa.Text(), nullable=True),
        sa.Column("expected_level", sa.SmallInteger(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "rejected_values",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
            comment="Taxonomy values refused for this content, to inform refinement.",
        ),
        sa.CheckConstraint(
            "outcome IN ('proposed', 'already_present', 'render_failed', 'unclassified')",
            name=op.f("ck_dry_run_items_ck_dry_run_items_outcome_known"),
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["dry_runs.id"], name="fk_dry_run_items_run_id_dry_runs", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint(
            "run_id", "content_object_id", "destination_name", name="pk_dry_run_items"
        ),
    )
    # A dry run treats a locator it already proposed for other content as taken, and looks
    # up earlier pages' proposals by run, destination and locator.
    op.create_index(
        "ix_dry_run_items_run_id_destination_name_locator",
        "dry_run_items",
        ["run_id", "destination_name", "locator"],
    )

    op.create_table(
        "destination_checks",
        sa.Column("destination_name", sa.Text(), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reachable", sa.Boolean(), nullable=False),
        sa.Column(
            "reason",
            sa.Text(),
            nullable=True,
            comment="Why the destination could not be checked.",
        ),
        sa.Column(
            "absent",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
            comment="Registered placements found missing by this check.",
        ),
        sa.CheckConstraint(
            "absent >= 0",
            name=op.f("ck_destination_checks_ck_destination_checks_absent_non_negative"),
        ),
        sa.PrimaryKeyConstraint("destination_name", name="pk_destination_checks"),
        comment="The latest check of each destination for its registered placements.",
    )


def upgrade() -> None:
    """Create every ledger table, index, audit trigger and workflow view."""
    _create_identity_tables()
    _create_audit_entries()
    _create_staging_and_connection_tables()
    _create_scan_tables()
    _create_classification_tables()
    _create_placement_run_tables()
    op.execute(WORKFLOW_STATUS_BY_QUEUE)
    op.execute(STALLED_WORKFLOWS)
    op.execute(FAILED_WORKFLOWS)


def downgrade() -> None:
    """Drop the workflow views, the audit triggers and function, then every ledger table."""
    for view in _VIEWS:
        op.execute(f"DROP VIEW IF EXISTS {view}")
    op.execute(DROP_APPEND_ONLY)
    # Children before the tables their foreign keys reference.
    for table in (
        "destination_checks",
        "dry_run_items",
        "dry_runs",
        "classification_holds",
        "remote_spend",
        "review_items",
        "classifications",
        "configurations",
        "scan_folders",
        "scans",
        "provider_connections",
        "staged_objects",
        "audit_entries",
        "placements",
        "source_objects",
        "content_objects",
    ):
        op.drop_table(table)
