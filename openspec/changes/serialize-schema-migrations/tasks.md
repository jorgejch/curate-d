# Tasks

Streams A, B, D and E touch disjoint files and can start at once. Stream C (`migrations/env.py`)
waits for A and B, because it imports the key and the setting. Stream F (integration tests) waits
for C and D, because a fresh database gets its datasource tracking table only from the new
`env.py`. Group 7 waits for all of them. Stream G lives in the deployment repository and is
opened only after this change is merged and its image runs in dev (design D8, Migration Plan).

## 1. Stream A: the lock key (`src/curate_d/runtime/lease.py`, `tests/unit/runtime/test_lease.py`)

- [ ] 1.1 Add `_MIGRATION_LOCK_DOMAIN = b"curate-d/migration-lock/v1"`, the fixed name `"schema"` and `migration_lock_key(application_name)` beside `promotion_fence_key`, as in design D3. Verify with tests that pin the key's value, show it is a signed 64-bit integer, stable, different for another application name, and apart from `lease_key`, `version_lease_key`, `promotion_fence_key` and `schedule_authority_key` for the name `"schema"`: `uv run pytest tests/unit/runtime/test_lease.py --no-cov`.
- [ ] 1.2 Factor the holder description of `PostgresLeaseSession.holder` into a public `describe_holder(execute, key)` that takes a callable running one query and returning one row, and make `holder` use it, as in design D5. Verify with the existing `holder` tests plus one that calls `describe_holder` with a fake row and with no row: `uv run pytest tests/unit/runtime/test_lease.py --no-cov`.

## 2. Stream B: the setting (`src/curate_d/config.py`, `tests/unit/test_config.py`)

- [ ] 2.1 Add `migration_lock_timeout_seconds: float = Field(default=1800.0, gt=0, description=...)` after the ledger fields, in the form of `staging_transfer_timeout_seconds`, as in design D2. Verify with tests that the default is 1800, that `CURATE_D_MIGRATION_LOCK_TIMEOUT_SECONDS=90` reads back as 90.0, and that `0`, `-1` and `abc` stop settings load with an error naming the field: `uv run pytest tests/unit/test_config.py --no-cov`.

## 3. Stream C: the serialized run (`migrations/env.py`) — after A and B

- [ ] 3.1 Rewrite `run_migrations_online` as in design D1, D2 and D5: open the engine and its connection first; call `configure_logging(level=INFO)` after `fileConfig`; log `migration.lock_waiting` with the holder from `describe_holder` when the key is held; set `lock_timeout` from `migration_lock_timeout_seconds`, run `pg_advisory_lock(migration_lock_key(APPLICATION_NAME))`, reset `lock_timeout` to `0` in a `finally`, commit; on SQLSTATE `55P03` log `migration.lock_timeout` naming `CURATE_D_MIGRATION_LOCK_TIMEOUT_SECONDS` and the holder, and exit with code 3; log `migration.lock_acquired`; unlock in a `finally` after Alembic and log `migration.lock_released` with the outcome. Verify with `uv run ruff check migrations/env.py` and, on a fresh `make test-stack`, `uv run pytest tests/integration/test_migrations.py --no-cov` (the existing tests: empty database, second run no-op, downgrade round trip).
- [ ] 3.2 Replace `run_dbos_database_migrations` with `DBOS.migrate(system_database_url(settings), schema=SYSTEM_SCHEMA)` and add `await AsyncSQLAlchemyDatasource.migrate(str(settings.ledger_database_url), schema=TRACKING_SCHEMA)`, both after the lock and before Alembic, as in design D4. Verify on a fresh `make test-stack` that `alembic upgrade head` on the `empty_database` fixture creates `dbos.datasource_outputs` (query `information_schema.tables` in the existing empty-database test): `uv run pytest tests/integration/test_migrations.py --no-cov`.
- [ ] 3.3 Update the module docstring and `run_migrations_offline`'s docstring: online runs serialize under the migration lock, downgrades take it, offline mode takes none and emits no DBOS SQL, as in design D6. Verify with `uv run ruff check migrations/env.py` and `grep -n "lock" migrations/env.py`.

## 4. Stream D: verify-only launch (`src/curate_d/runtime/dbos_app.py`, `src/curate_d/runtime/datasource.py`, `tests/unit/runtime/test_dbos_app.py`, `tests/unit/runtime/test_datasource.py` or the datasource's existing unit test file)

- [ ] 4.1 Set `"run_migrations": False` in `build_dbos_config`, as in design D4. Verify with a test that `build_dbos_config(settings, executor_id=...)["run_migrations"] is False`: `uv run pytest tests/unit/runtime/test_dbos_app.py --no-cov`.
- [ ] 4.2 Pass `run_migrations=False` in `create_ledger_datasource`. Verify with a test that replaces `AsyncSQLAlchemyDatasource.create` and checks the keyword: `uv run pytest tests/unit/runtime --no-cov -k datasource`.
- [ ] 4.3 Move the signature pin at `tests/unit/runtime/test_dbos_app.py:283-288` from `run_dbos_database_migrations` to `DBOS.migrate` (`schema` parameter) and `AsyncSQLAlchemyDatasource.migrate` (`schema` parameter), the two public calls `env.py` now makes. Verify with `uv run pytest tests/unit/runtime/test_dbos_app.py --no-cov`.

## 5. Stream E: documentation (`docs/recovery.md`, `docs/reset.md`, `docs/staging.md`, `CLAUDE.md`)

- [ ] 5.1 `docs/recovery.md`: in Breaking deploys, step 4 of `0023_destination_identity`, say several instances may run `alembic upgrade head` at once and that the rest wait, then exit 0; add `migration.lock_timeout` to the alert list in the form of the other entries (what happened; raise `CURATE_D_MIGRATION_LOCK_TIMEOUT_SECONDS` or find the holder named in the event); add the migration lock to Connection pooling's list of session-lock holders; under Before deploying, add that workers verify and never migrate, so the migration must run before a build's workers start: in Kubernetes each pod's `migrate` init container does it (the deployment repository), locally and in a breaking deploy it is `make migrate`; a `dbos` package bump is a migration in this sense. Verify with `grep -n "migration.lock_timeout\|MIGRATION_LOCK_TIMEOUT\|init container\|verify" docs/recovery.md` and `uv run pytest tests/unit/infrastructure/test_ops_paths.py --no-cov`.
- [ ] 5.2 `docs/reset.md`: in Wipe and migrate, say the migration takes the migration lock, and that the reset's ending of other sessions also ends a migration running at that moment, which then exits non-zero and must be rerun. Verify with `grep -n "migration lock" docs/reset.md`.
- [ ] 5.3 `docs/staging.md`: add `CURATE_D_MIGRATION_LOCK_TIMEOUT_SECONDS` to the settings table after the `LEDGER_DATABASE_*` rows (how long `alembic upgrade`/`downgrade` waits for another run; default 1800 seconds; on timeout the run exits 3 and changes nothing). Verify with `grep -n "MIGRATION_LOCK_TIMEOUT" docs/staging.md` and that the name matches the field: `grep -n "migration_lock_timeout_seconds" src/curate_d/config.py`.
- [ ] 5.4 `CLAUDE.md` DBOS section (`AGENTS.md` is a symlink to it): add one bullet after the `enable_otlp` one: `build_dbos_config` and `create_ledger_datasource` set `run_migrations=False`, `migrations/env.py` is the only migrator of the DBOS system schema and the datasource tracking table, under the migration lock, and a `dbos` package bump needs `make migrate` before its workers start; a setting the skill lists, not a departure. Update the bullet that says `env.py` brings the system schema up to date to name the lock. Verify with `grep -n "run_migrations" CLAUDE.md`.

## 6. Stream F: integration tests (`tests/integration/test_migrations.py`) — after C and D

- [ ] 6.1 Add a test that starts three `alembic upgrade head` subprocesses with `Popen` at once on the `empty_database` fixture; assert every exit code is 0, `alembic_version` holds the head revision, the `dbos` schema's `workflow_status` and `datasource_outputs` tables exist, and the count of `Running upgrade` lines over all stderr equals the revision count from `ScriptDirectory`. Verify on a fresh `make test-stack`: `uv run pytest tests/integration/test_migrations.py --no-cov -k concurrent`.
- [ ] 6.2 Add the same test on a database first upgraded to the revision before head; assert every exit 0, head reached, and exactly one `Running upgrade` line over all stderr. Verify as in 6.1.
- [ ] 6.3 Add a test that holds `migration_lock_key(APPLICATION_NAME)` through `connect_session` on the created database, runs `alembic upgrade head` with `CURATE_D_MIGRATION_LOCK_TIMEOUT_SECONDS=1`; assert exit code 3, stderr holds `migration.lock_timeout` and `CURATE_D_MIGRATION_LOCK_TIMEOUT_SECONDS`, and the database has neither `alembic_version` nor a `dbos` schema. Verify: `uv run pytest tests/integration/test_migrations.py --no-cov -k timeout`.
- [ ] 6.4 Add a test that `DBOS.destroy()`, configures with settings whose `ledger_database_url` is the created, unmigrated database, and asserts `DBOS.launch()` raises `DBOSInitializationError` and the database still has no `dbos` schema; `DBOS.destroy()` in teardown. Verify: `uv run pytest tests/integration/test_migrations.py --no-cov -k verify_only`.

## 7. Integration check

- [ ] 7.1 Run `make lint`, `make typecheck` and `uv run pytest -n auto tests/unit --no-cov`, and verify that all pass.
- [ ] 7.2 Run `make test-integration-parallel`, which starts a fresh stack per shard and migrates it through the new `env.py`, and verify that it passes, including the fixtures that `DBOS.launch()` and `create_ledger_datasource` on the migrated stack.

## 8. Stream G: deployment repository (the two worker StatefulSet manifests, the API Deployment manifest, the application README and the manifest tests) — a pull request against its default branch, after this change is merged and its image runs in dev

- [ ] 8.1 Add the API's `migrate` init container, unchanged (same image tag field, `["alembic", "upgrade", "head"]`, `envFrom` `curate-d-config` and `curate-d-secret`, `CURATE_D_LEDGER_DATABASE_URL` from `curate-d-db-app`, `LOGFIRE_TOKEN`, security context, resources), to both worker StatefulSets, as in design D8. Verify with `uvx --with pyyaml pytest tests` in that repository: the existing token, URL, pinned-tag, node-pool and Conductor-variable tests iterate init containers and must still pass.
- [ ] 8.2 Replace the API init container's comment: the runs of pods that start together are serialized by curate.d's migration lock; one migrates, the rest wait, then exit 0. Verify with `grep -n "serializ"` on the API Deployment manifest.
- [ ] 8.3 Add `test_every_curate_d_workload_migrates_before_it_starts` to the manifest tests: for each deployable overlay, every curate.d Deployment and StatefulSet has an init container named `migrate` whose command is `["alembic", "upgrade", "head"]` and whose image equals the pod's main curate.d container's image. Verify with `uvx --with pyyaml pytest tests -k migrates_before`.
- [ ] 8.4 Application README: in "Why it is shaped this way", add a paragraph on the `migrate` init container (every pod of every role migrates before it starts; curate.d's lock serializes pods that start together; the worker launch verifies and never migrates), and name the new test in "Checks". Verify with `grep -n "migrate"` on that README.
- [ ] 8.5 Open the pull request against the default branch with the Manifest checks workflow green, and link it from this change's PR. Verify with `gh pr checks` on that PR.

## Workflow follow-up

- Sync the delta spec into `openspec/specs/schema-migrations/` (`openspec-sync-specs`), then archive the change after review.
- Stream G merges only after an image with the lock runs in dev; archive this change once both PRs are merged.
