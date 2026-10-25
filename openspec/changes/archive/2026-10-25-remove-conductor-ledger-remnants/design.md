# Design

## Context

Migration 0004 created `audit_entries` with `ck_audit_entries_entry_type_known`; 0014 recreated
the CHECK to add `placement_absent`; 0017 and 0019 recreated it to add `workflow_command_issued`
and `workflow_command_completed`; 0018 added the partial unique index
`uq_audit_entries_command_request_id` on `(details ->> 'request_id')` for
`entry_type = 'workflow_command_issued'`. 0020 to 0022 do not touch `audit_entries`. In
`ledger/tables.py` the CHECK is `enum_check("entry_type", *AuditEntryType)`, so it lists the
enum's members in declaration order, and the index is declared beside the table.
`test_ledger_schema.py` compares the migrated schema with the metadata (`compare_metadata`) and,
for the index, the predicate PostgreSQL reports, since `compare_metadata` ignores it.

Conductor was removed on 2026-10-13; the command path that wrote these entries went with it. The
remnants stayed so recorded entries would stay readable.

## Goals / Non-Goals

**Goals:**
- No Conductor remnant in the ledger schema, domain, metadata, spec, tests or lint config.
- A linear migration chain with one head in which 0020 revises 0016.

**Non-Goals:**
- Resetting the dev database, reconnecting pCloud or re-applying policy (owner-run, below).
- The manifest side (jorgejch/flux-ovh#64).
- Editing the vendored `dbos-python` skill: it is upstream text, Conductor rows in
  `lifecycle-config.md` included.

## Decisions

- **Delete the migrations rather than add a reverting one.** A reverting 0023 would keep three
  migrations and a fourth to undo them, and it would fail on any database that holds a command
  entry: audit entries are append-only, so the narrower CHECK could not be restored over them.
  The owner decided a schema change is acceptable because only dev is deployed and its database
  can be reset. Deleting keeps the chain as if the types had never been added.
- **Splice 0020 onto 0016.** 0020 creates `lease_versions` and does not depend on the audit
  changes, so only its `down_revision` and docstring's `Revises:` line change. The CHECK built by
  the chain is then 0014's: `classification_decided`, `classification_rejected`, `render_failed`,
  `placement_started`, `placement_registered`, `placement_failed`, `placement_absent`,
  `verification_succeeded`, `verification_failed`, `retention_decided`, `source_read_failed`,
  `source_discrepancy_detected`. With the two members gone, `AuditEntryType` declares exactly
  these twelve in this order, so `enum_check` in `tables.py` renders the same expression.
- **Drop the retired-type sentence from the requirement.** "An entry type the system no longer
  writes SHALL stay valid in the ledger" was written for the `workflow_command_*` types, and this
  change contradicts it by retiring them from the CHECK. No other retired type exists. Whether a
  later retirement keeps its type valid is decided with that change.
- **Sync before validating.** A MODIFIED requirement replaces the whole block, and
  `openspec validate` rejects one that omits a scenario the main spec still has, with no
  scenario-level removal to express the drop. A REMOVED plus ADDED pair under the same name is
  rejected too. So the delta is a MODIFIED block without the scenario, and `validate --strict`
  passes once it is synced into `openspec/specs/content-ledger/spec.md` (task 5.1), not before.
- **Keep the general `dbos._` guard.** Only the named ruff ban for `dbos._conductor` goes;
  `tests/unit/test_no_library_internals.py` matches `dbos\._` in `src/` and `migrations/`, so a
  Conductor import would still fail the build.

## Risks / Trade-offs

- [A database at 0017 to 0022 no longer upgrades: Alembic cannot find its stamped revision, or
  finds a 0020 whose parent it never applied] → Only dev exists; it is recreated (Migration Plan).
- [Recorded `workflow_command_*` entries are lost with the dev reset] → Accepted by the owner;
  they described commands of a removed feature in a development database.
- [The DBOS workflow history in the same database goes too] → Accepted with the reset; it is dev
  history, and the system tables are recreated by `migrations/env.py` before Alembic runs.

## Migration Plan

Merge into `dev`. Then, owner-run and outside this repository: drop and recreate the dev
database (its `alembic_version` and audit CHECK reflect deleted revisions, and the DBOS system
tables live in the same database, so workflow history goes too), let the deployment migrate it
from empty, reconnect the pCloud account, and re-apply policy and configuration. No prod database
exists. Rollback is reverting the commits and resetting dev again.

## Parallelism

Four work streams touch disjoint files and can start at once; none depends on another's output.
Task 5 runs after all four.

- **A. Migrations and tests**: the three deleted migrations, `0020_lease_versions.py`,
  `tests/integration/test_ledger_schema.py`, `tests/integration/test_ledger_repository.py`,
  `.test_durations`.
- **B. Domain and metadata**: `src/curate_d/domain/audit.py`, `src/curate_d/ledger/tables.py`.
- **C. Spec**: this change's delta spec.
- **D. Lint config and CLAUDE.md**: `pyproject.toml`, `CLAUDE.md`.

A and B must both land before the unit suite or the integration suite runs: 0018 imports
`COMMAND_ENTRIES` from `tables.py`, and the deleted tests reference the deleted enum member. They
are separate files, so they still run in parallel and merge before task 5.
