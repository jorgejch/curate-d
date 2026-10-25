# Tasks

Streams A to D touch disjoint files and start at once (design.md, Parallelism). Task 5 runs once
all four are merged. Unit commands use `make check`; the integration suite needs
`make test-stack`, then `uv run pytest <files> --no-cov -m integration`.

## 1. Stream A: migrations and tests

- [ ] 1.1 Delete `migrations/versions/0017_workflow_command_audit.py`, `0018_command_request_unique.py` and `0019_command_completed_audit.py`; set `0020_lease_versions.py`'s `down_revision` and its docstring's `Revises:` line to `0016_dry_run_item_locators`. Verify with `uv run alembic heads` printing only `0022_dry_run_drafts (head)` and `uv run alembic history` showing `0016_dry_run_item_locators -> 0020_lease_versions`
- [ ] 1.2 Delete `test_the_migrated_command_request_index_matches_the_model_metadata` from `tests/integration/test_ledger_schema.py` and `test_a_historical_operator_command_entry_still_reads_back` from `tests/integration/test_ledger_repository.py`, with any import only they used, and their entries in `.test_durations`. Verify with `uv run ruff check tests/integration` and `python -m json.tool .test_durations` passing, and `git grep -n "command_request\|historical_operator" tests .test_durations` returning nothing

## 2. Stream B: domain and metadata

- [ ] 2.1 Delete `WORKFLOW_COMMAND_ISSUED` and `WORKFLOW_COMMAND_COMPLETED` and their comment from `AuditEntryType` in `src/curate_d/domain/audit.py`. Verify with `git grep -n "WORKFLOW_COMMAND" src tests` returning nothing
- [ ] 2.2 Delete `COMMAND_REQUEST_ID`, `COMMAND_ENTRIES`, the `uq_audit_entries_command_request_id` index and their comment from `src/curate_d/ledger/tables.py`, and any import left unused. Verify with `uv run python -c "from curate_d.ledger.tables import audit_entries as t; print([c.sqltext for c in t.constraints if c.name == 'entry_type_known'])"` printing 0014's `_TYPES_AFTER` list in its order, and `uv run ruff check src` passing

## 3. Stream C: spec

- [ ] 3.1 Write this change's `specs/content-ledger/spec.md`: "Immutable audit trail" as MODIFIED, without the retired-type sentence and the scenario "Entries of a retired type". Verify with `npx -y @fission-ai/openspec@1.15.0 show remove-conductor-ledger-remnants --json --deltas-only` listing one MODIFIED requirement with five scenarios (design.md, Decisions: `validate` passes only once the delta is synced)

## 4. Stream D: lint config and CLAUDE.md

- [ ] 4.1 Remove the `"dbos._conductor"` entry from `[tool.ruff.lint.flake8-tidy-imports.banned-api]` in `pyproject.toml`. Verify with `uv run pytest tests/unit/test_no_library_internals.py --no-cov` passing and `grep -n 'dbos\\._' tests/unit/test_no_library_internals.py` showing the general pattern
- [ ] 4.2 Reword CLAUDE.md's "Conductor references included" clause in the `dbos-python` skill note without changing its meaning. Verify with `grep -n -i conductor CLAUDE.md` returning nothing

## 5. Integration

- [ ] 5.1 Sync the delta into `openspec/specs/content-ledger/spec.md`. Verify with `npx -y @fission-ai/openspec@1.15.0 validate --all --strict` passing
- [ ] 5.2 Verify with `make check` passing, and `git grep -i conductor -- ':!.agents/skills/dbos-python' ':!.claude/skills/dbos-python' ':!openspec/changes/archive'` returning nothing once the change is archived
- [ ] 5.3 Run the integration files that depend on the chain: `tests/integration/test_migrations.py`, `test_ledger_schema.py` and `test_ledger_repository.py`. Verify with `make test-stack`, then the integration command on those files, all passing
