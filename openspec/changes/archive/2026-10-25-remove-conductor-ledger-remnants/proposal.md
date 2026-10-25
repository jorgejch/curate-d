# Proposal

## Why

Conductor was removed from curate.d on 2026-10-13, but its ledger remnants stayed so the
entries already recorded would remain readable: the `workflow_command_issued` and
`workflow_command_completed` audit entry types, the partial unique index on a command entry's
`request_id`, and migrations 0017 to 0019 that added them. Nothing writes those entries, and the
only deployed ledger is dev's, whose database can be reset. Keeping schema for a removed feature
costs a migration chain, an index, two enum members, a spec scenario and a test each, for entries
nobody needs to read. The manifest side is jorgejch/flux-ovh#64, another repository.

## What Changes

- **BREAKING** (schema): migrations `0017_workflow_command_audit`, `0018_command_request_unique`
  and `0019_command_completed_audit` are deleted, and `0020_lease_versions` revises
  `0016_dry_run_item_locators`. A fresh `alembic upgrade head` builds the audit entry-type CHECK
  as 0014 left it and no `uq_audit_entries_command_request_id` index. A database stamped at or
  past 0017 no longer resolves its `alembic_version` and must be recreated.
- `AuditEntryType` loses `WORKFLOW_COMMAND_ISSUED` and `WORKFLOW_COMMAND_COMPLETED`; the ledger
  metadata loses `COMMAND_REQUEST_ID`, `COMMAND_ENTRIES` and the index, so the metadata's CHECK,
  derived from the enum, matches what the migrations build.
- The integration tests that pinned the index and read a command entry back are deleted.
- The `content-ledger` requirement "Immutable audit trail" drops the scenario "Entries of a
  retired type" and the sentence that kept retired entry types valid.
- The ruff `banned-api` entry for `dbos._conductor` goes; `tests/unit/test_no_library_internals.py`
  still rejects every `dbos._` reference in shipped code.
- CLAUDE.md's note on keeping the vendored `dbos-python` skill whole is reworded so it no longer
  names Conductor, with the same meaning.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `content-ledger`: "Immutable audit trail" no longer keeps the `workflow_command_*` entry types
  valid, nor any retired entry type as a rule.

## Impact

- `migrations/versions/0017_workflow_command_audit.py`, `0018_command_request_unique.py` and
  `0019_command_completed_audit.py` (deleted), `0020_lease_versions.py`;
  `src/curate_d/domain/audit.py`, `src/curate_d/ledger/tables.py`;
  `tests/integration/test_ledger_schema.py`, `tests/integration/test_ledger_repository.py`,
  `.test_durations`; `openspec/specs/content-ledger/spec.md`; `pyproject.toml`; `CLAUDE.md`.
- Dev rollout, owner-run and outside this repository: the dev database has to be dropped and
  recreated, since its `alembic_version` and its audit CHECK reflect deleted revisions. The DBOS
  system tables live in the same database, so workflow history goes too. The pCloud account is
  then reconnected and policy and configuration re-applied. No prod database exists.
- The vendored `dbos-python` skill (`.agents/skills/dbos-python`) is upstream text and keeps its
  Conductor references.
