# Proposal

## Why

Two `alembic upgrade head` runs against the same ledger database at the same time race: the
second reads `alembic_version` before the first commits, then fails on duplicate DDL or blocks on
a table lock, and every `DBOS.launch()` migrates the DBOS system schema on its own, racing
`migrations/env.py`'s DBOS step. The deployment repository decides how many instances run the
migration at once, so the migration itself must be safe for any number.

## What Changes

- Online migration runs (`alembic upgrade`, `alembic downgrade`) take one session advisory lock
  on the connection Alembic uses, before the DBOS system-schema step, and hold it until Alembic
  commits or rolls back. The lock key is a new domain in `runtime/lease.py`'s key scheme, scoped
  by the application name and distinct from the executor, version and promotion-fence keys.
- A run that finds the lock held waits for it, bounded by a new setting
  `migration_lock_timeout_seconds` (`CURATE_D_MIGRATION_LOCK_TIMEOUT_SECONDS`, default 1800). On
  timeout it logs one ERROR event, `migration.lock_timeout`, that names the setting, exits
  non-zero and changes nothing.
- After it takes the lock, a run reads the current revision again and applies only what is still
  pending; a run that finds the schema current exits 0.
- `migrations/env.py` becomes the only migrator of the DBOS system schema and of the ledger
  datasource's tracking table: `build_dbos_config` sets `run_migrations=False` and
  `create_ledger_datasource` passes `run_migrations=False`, so `DBOS.launch()` and the datasource
  only verify the schema. `env.py` migrates the datasource table under the same lock, through the
  public `AsyncSQLAlchemyDatasource.migrate`. A worker that starts on a database behind its DBOS
  version fails launch instead of migrating it, so a DBOS upgrade now needs `make migrate` before
  its workers roll out. The DBOS skill recommends this split for production; it is not a
  departure.
- A run that holds the lock logs `migration.lock_acquired` when it starts and a progress line per
  applied revision, so a waiting run's operator can see why it waits and the waiting run logs
  `migration.lock_waiting` with the holder's description.
- Offline mode (`alembic upgrade --sql`) takes no lock; its docstring says so.
- An `alembic upgrade` that finds the database at a revision its build does not know treats the
  schema as ahead of the build: it logs one WARNING event, `migration.schema_ahead`, changes
  nothing and exits 0. That lets an older image start on a newer, backward-compatible schema,
  which a rollback needs when the deployment runs the migration from every pod. A downgrade
  from an unknown revision keeps Alembic's failure.
- The guarantee this change gives is safety, not scheduling: `alembic upgrade head` is correct
  from any number of invokers at once, and a process that runs workflows refuses to start on a
  schema behind its build. How and when a deployment invokes the migration (an init container
  on every pod, a release job, a hand step) stays the deployer's choice and is not part of this
  repository. No change to any deployment repository is required or assumed.
- Docs: `docs/recovery.md` (the breaking-deploy step that runs the migration, the alert list, the
  connection-pooling list of session-lock holders, Before deploying), `docs/reset.md`,
  `docs/staging.md`'s settings table, `docs/twelve-factor.md` (rows V.3 and XII.1), `CLAUDE.md`'s
  DBOS section (and `AGENTS.md`, its copy) and `env.py`'s module docstring.

## Capabilities

### New Capabilities

- `schema-migrations`: how the ledger and DBOS system schemas are brought to a revision: which
  process migrates, how concurrent runs are serialized, how a waiting run gives up, what a run
  does on a schema ahead of its build, and what a process that does not migrate does when the
  schema is behind.

### Modified Capabilities

None. `ledger-connection`'s "Direct connections keep the URL parameters" already covers every
libpq connection opened through `runtime/lease.py`; the migration lock rides on Alembic's own
SQLAlchemy connection, not a libpq one, so no requirement there changes.

## Impact

- `migrations/env.py`: the lock, the bounded wait, the DBOS and datasource steps under it, the
  schema-ahead check, the progress and timeout events, the exit code.
- `src/curate_d/runtime/lease.py`: `migration_lock_key` and its key domain.
- `src/curate_d/config.py`: `migration_lock_timeout_seconds`.
- `src/curate_d/runtime/dbos_app.py`, `src/curate_d/runtime/datasource.py`: `run_migrations=False`.
- `tests/integration/test_migrations.py`: concurrent runs, the timeout, a schema ahead, a launch
  on a schema behind; `tests/unit/runtime/test_lease.py`, `tests/unit/test_config.py`,
  `tests/unit/runtime/test_dbos_app.py`: the key, the setting, the config flag.
- Docs listed above. `Makefile` and `ops/reset/reset-environment.sh` do not change: they keep
  calling `alembic upgrade head`, now safe to call from several instances at once.
- No deployment repository changes. A deployment that runs `alembic upgrade head` from an init
  container on every pod becomes correct without any edit; a deployment that wants one run per
  release can add it later, on its own schedule. The curate.d code reads nothing from any
  deployment repository (`tests/unit/test_isolation.py`).
- Operators: the migration must run before a build's workers start, for a ledger revision and
  for a DBOS package bump alike; a worker that starts first fails launch with
  `DBOSInitializationError` and restarts. Locally and in a breaking deploy it is `make migrate`.
