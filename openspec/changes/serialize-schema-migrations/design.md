# Design

## Context

See proposal.md, Why. The facts the approach rests on, read at ab773b8:

- `migrations/env.py` online mode runs `run_dbos_database_migrations` on DBOS's own engine, then
  opens a `NullPool` async engine (psycopg) and runs every pending revision in one Alembic
  transaction. Nothing serializes two runs. Offline mode emits SQL only.
- Every caller is the Alembic CLI: `make migrate`, `make test-stack`,
  `tests/parallel_integration.py`, `ops/reset/reset-environment.sh` and the subprocess in
  `tests/integration/test_migrations.py`. No code in `src/` runs Alembic.
- `build_dbos_config` (`runtime/dbos_app.py`) does not set `run_migrations`, and
  `create_ledger_datasource` (`runtime/datasource.py`) does not pass it, so the only production
  `DBOS.launch()` (`worker/main.py`) and the datasource `create` both migrate on start, racing
  `env.py`. The API and `promote` use `DBOSClient`, which never migrates. `env.py` does not
  create the datasource's tracking table today; only the worker does.
- Workers already need the ledger revisions before launch: `register_version` inserts into
  `lease_versions` (0020) first.
- DBOS's public docs (configuration, CLI, `DBOS.migrate`, datasources, production checklist,
  read 2026-11-07) say nothing about running its system-schema or datasource migration from
  several processes at once, name no lock, and document no "already migrated" no-op. They do
  document `run_migrations=False`: launch then only verifies, raises `DBOSInitializationError`
  for a missing or older schema, accepts a newer one; the datasource `create(...,
  run_migrations=False)` behaves the same. They describe the production pattern: a privileged
  migrate step (`dbos migrate`, `DBOS.migrate`, `SQLAlchemyDatasource.migrate`), then the
  application with `run_migrations=False`. The `dbos-python` skill
  (`references/lifecycle-config.md`, `step-transactions.md`) says the same.
- `runtime/lease.py` derives every advisory key through `_advisory_key(domain, application_name,
  name)` under a versioned domain; `promotion_fence_key` is the pattern for one fixed lock per
  application. `PostgresLeaseSession.lock_within` is the pattern for a bounded `pg_advisory_lock`
  under `lock_timeout`. `version_lease.hold_fence_exclusive` and `promote.py` are the pattern for
  a timeout error, its ERROR event and its exit code (`EXIT_FENCED = 3`).
- No spec owns migrations; `docs/recovery.md` documents the migration only inside the breaking
  deploy of `0023_destination_identity` (step 4) and lists every session-lock holder under
  Connection pooling. `AGENTS.md` is a symlink to `CLAUDE.md`.
- `tests/unit/test_no_library_internals.py` and ruff `TID251` forbid DBOS private modules, so
  `env.py` cannot read DBOS's own lock id or migration internals; the design uses public API only.
- Deployment (the maintainer's deployment manifest repository, read at its default branch):
  the API Deployment (2 replicas, rolling update) runs `alembic upgrade head` in a `migrate`
  init container on every pod start, from the same image, with the ledger URL from
  `curate-d-db-app` through the session-mode pooler; its comment notes that pods starting
  together race and nothing serializes them. Both worker StatefulSets (2 replicas,
  `podManagementPolicy: Parallel`) have no init container; a worker that starts before the
  API's migration finishes fails `register_version` and restarts. The manifest repository's
  tests already iterate `initContainers` for the token, URL, tag and node-pool checks.

## Goals / Non-Goals

**Goals:**

- Any number of `alembic upgrade` / `alembic downgrade` processes against one database: one
  migrates, the rest wait, then find the schema current and exit 0. The Makefile, the reset
  script and the deployment repository keep calling `alembic upgrade head` unchanged.
- One migrator for all three schemas in the ledger database (DBOS system schema, the datasource
  tracking table, the ledger revisions); nothing else writes DDL.
- A waiter that gives up is explicit: one named ERROR event, non-zero exit, nothing changed.

**Non-Goals:**

- Serializing a migration against running workers' DML; a migration that needs a drained
  pipeline stays a breaking deploy (`docs/recovery.md`).
- A separate DBOS system database (unsupported, CLAUDE.md).
- Changing how the deployment repository schedules the migration job.
- A code registry of event names; the alert list stays prose in `docs/recovery.md`.

## Decisions

### D1. The lock is a session advisory lock on Alembic's own connection

`run_migrations_online` opens the async engine and its one connection first, takes the lock on
that connection, runs the DBOS and datasource steps, then hands the same connection to Alembic,
and unlocks after Alembic's transaction commits or rolls back (in a `finally`, before the
connection closes). A lost connection therefore ends the lock and the migration together, which
is what the requirement asks. Session locks survive a commit, so taking the lock in the
connection's autobegun transaction and committing is fine; the connection is left with no open
transaction when Alembic configures on it.

Alternative: a dedicated `connect_session` psycopg connection held around both steps (the
executor-lease pattern). Rejected: the lock could outlive a broken Alembic connection and keep
waiters out until the dedicated session noticed; and it is a second connection through the pooler
for no gain.

### D2. The wait is PostgreSQL's `lock_timeout`, reset before Alembic runs

As `lock_within`: `SELECT set_config('lock_timeout', '<ms>ms', false)`, then
`SELECT pg_advisory_lock(:key)`; SQLSTATE `55P03` (`LockNotAvailable`, reached through
`sqlalchemy.exc.OperationalError.orig`) means the timeout passed. The `finally` resets
`lock_timeout` to `0` on both paths, so Alembic's DDL keeps today's unbounded table-lock waits.
The timeout comes from `Settings.migration_lock_timeout_seconds` (`float`, `gt=0`, default
`1800.0`, same form as `staging_transfer_timeout_seconds`); 30 minutes covers every migration in
the chain with room, and a stuck holder is reported, not waited on forever.

Alternative: a retry loop on `pg_try_advisory_lock` with backoff (`ExecutorLease.acquire`).
Rejected: a blocking `pg_advisory_lock` queues FIFO behind the holder and wakes at once on
release, and it needs no thread or sleep.

### D3. Key: `migration_lock_key(application_name)` under `b"curate-d/migration-lock/v1"`

Added to `runtime/lease.py` beside `promotion_fence_key`, with the fixed name `"schema"`. Pinned
by value in `tests/unit/runtime/test_lease.py`, and shown apart from `lease_key`,
`version_lease_key`, `promotion_fence_key` and `schedule_authority_key` for a name spelled alike.
`APPLICATION_NAME` is `"curate-d"` bare, as the other keys use it.

### D4. `env.py` is the only migrator: `run_migrations=False` everywhere else

`build_dbos_config` sets `"run_migrations": False`; `create_ledger_datasource` passes
`run_migrations=False`. Both then verify only. Under the lock, `env.py` calls the public
`DBOS.migrate(system_database_url, schema=SYSTEM_SCHEMA)` and
`await AsyncSQLAlchemyDatasource.migrate(url, schema=TRACKING_SCHEMA)` before Alembic. The
tracking-table step is new: without it a worker's verify-only `create` fails on a fresh database.
`DBOS.migrate` replaces `run_dbos_database_migrations`, whose CLI wrapper replaces any cause with
`click.exceptions.Exit(1)`; a failure now exits non-zero naming the cause. `DBOS.migrate` is
synchronous and runs on the loop thread; the process does nothing else meanwhile, so that is
acceptable.

Why not the alternative, the same lock around `DBOS.launch()`: the public docs give no ground to
call concurrent DBOS migrations safe, and a worker that waits on a migration lock couples its
start to a migrator; the skill recommends verify-only for production. Consequence: a build that
bumps the `dbos` package must run `make migrate` before its workers start, or launch fails with
`DBOSInitializationError`; a ledger migration already has that rule. This is a setting the skill
lists, not a departure, and is recorded in CLAUDE.md's DBOS section as `enable_otlp` is.

Integration fixtures that `configure(...)` and `DBOS.launch()` run on a stack `make test-stack`
migrated through `env.py`, so they keep working; the unit pin on `run_dbos_database_migrations`'s
signature in `test_dbos_app.py` moves to `DBOS.migrate` and `AsyncSQLAlchemyDatasource.migrate`.

### D5. Events and exit code

`env.py` calls `configure_logging(level=INFO)` after Alembic's `fileConfig`, so the named events
render like the workers' (Alembic's own `Running upgrade a -> b` lines stay, and they are the
per-revision progress a holder's operator sees). Events:

- `migration.lock_waiting` (WARNING, once, before blocking): the key's holder description from a
  public `describe_holder(execute, key)` factored out of `PostgresLeaseSession.holder`, or
  "holder not visible".
- `migration.lock_acquired` (INFO): with the wait in seconds.
- `migration.lock_timeout` (ERROR): names `CURATE_D_MIGRATION_LOCK_TIMEOUT_SECONDS`, the timeout
  and the holder; then `MigrationLockTimeoutError` is raised and `env.py` exits with code 3, as
  `promote` does on its fence (`EXIT_FENCED`). Nothing was written: the lock is taken before the
  DBOS step.
- `migration.lock_released` (INFO) after unlock, with `outcome` committed or rolled back.

No heartbeat from the holder: the waiter's one line names the holder's pid, application name,
address and backend start, and the holder's Alembic log shows its progress.

### D6. Downgrades and offline mode

`alembic downgrade` runs the same `run_migrations_online` path, so it takes the lock and the DBOS
and datasource steps run (both no-ops on a current schema). Offline mode stays lock-free and keeps
its note that the DBOS schema is migrated separately; the module docstring says both.

### D8. Every pod migrates before it starts: the `migrate` init container on every role

The deployment repository gets a matching PR: both worker StatefulSets receive the API's
`migrate` init container unchanged (same image, `["alembic", "upgrade", "head"]`, the
`curate-d-config` ConfigMap and `curate-d-secret`, the ledger URL from `curate-d-db-app`,
`LOGFIRE_TOKEN`, the same security context and resources). Kubernetes starts the main container
only after the init container exits 0, so no worker or API pod can start on a schema behind its
image, and the verify-only launch (D4) is the backstop, not the mechanism. With the lock, the two
parallel pods of a StatefulSet and the API pods of a scale-up run one migration and N-1 no-ops.
A pod whose init container times out exits 3 and restarts under the kubelet's backoff. The API
comment that says nothing serializes the runs is replaced by one that says the lock does; the
README's "Checks" paragraph names the new test, which asserts that every curate.d
Deployment and StatefulSet has an init container named `migrate` running
`alembic upgrade head` from the pod's own image.

Order: the deployment PR merges only after an image with the lock runs in the target environment
(dev follows the `dev` branch through image automation). Before that, adding init containers to
`Parallel` StatefulSets would add unserialized runs. The curate.d code still reads nothing from
the deployment repository (`tests/unit/test_isolation.py`); only the agent does, as CLAUDE.md
now allows.

Alternatives: a Flux `dependsOn` on a migration `Job` (immutable Jobs need a new name per tag), a
Helm hook (no Helm here), the worker migrating in-process before `register_version` (puts the
migrator back inside the worker and breaks the stop-then-migrate order of a breaking deploy).

### D7. Tests

- Integration (`tests/integration/test_migrations.py`, on the `empty_database` fixture, which
  creates its own database): N `alembic upgrade head` subprocesses started with `Popen` at once
  on an empty database, and again on a database upgraded to the revision before head; every exit
  code 0, `alembic_version` at head, the `dbos` tables and the tracking table present, and the
  sum of Alembic `Running upgrade` lines across all stderr equals the number of revisions applied.
- Integration: the test holds `migration_lock_key("curate-d")` on a `PostgresLeaseSession` to the
  created database, runs `alembic upgrade head` with
  `CURATE_D_MIGRATION_LOCK_TIMEOUT_SECONDS=1`; exit 3, stderr holds `migration.lock_timeout` and
  the setting's name, and the database has no `alembic_version` and no `dbos` schema.
- Integration: `DBOS.destroy()`, `configure` with settings pointing at the created, unmigrated
  database, `DBOS.launch()` raises `DBOSInitializationError`; the database stays unmigrated.
- Unit: the key (D3); the setting's default, bound and error; `build_dbos_config()["run_migrations"]
  is False`; `create_ledger_datasource` passes `run_migrations=False`.

## Risks / Trade-offs

- [A reset kills a concurrent migrator] `ops/reset` ends every other session of the application
  role before it wipes; a migration running at that moment loses its connection, rolls back and
  exits non-zero. → Intended: the reset owns the database then; `docs/reset.md` says so.
- [A transaction-mode pooler breaks the session lock] → Same constraint as every lock holder; the
  Connection pooling section lists the migration among them. Both environments use session mode.
- [A worker of a new DBOS version starts before the migration] → In Kubernetes the `migrate`
  init container (D8) runs it first on every pod start. If it is missing or fails, launch fails
  explicitly with `DBOSInitializationError` and the pod restarts; `docs/recovery.md` Before
  deploying names the rule, and the breaking deploy step already runs the migration first.
- [A 1800 s init-container wait delays a rollout] → Only while another pod migrates; a no-op run
  takes the lock at once. The waiter's `migration.lock_waiting` line names the holder.
- [`DBOS.migrate` or the datasource migrate internally waits or fails under our lock] → They run
  one at a time under our lock, so their own contention path is never taken. If one raises, the
  run exits non-zero naming the cause and the lock is released in `finally`.
- [`configure_logging` in `env.py` changes Alembic's log output] → Alembic's logger config from
  `alembic.ini` is applied first; the integration tests assert both the Alembic lines and the
  named events are present in stderr.
- [Hash collision of the new key with an existing one] → 64-bit; the unit test shows the four
  keys apart for a like-spelled name, as the fence test does.

## Migration Plan

1. Merge the curate.d change; dev's image automation deploys it. The API's existing init
   container runs the new `env.py`: it finds the DBOS and ledger schemas current and creates the
   datasource tracking table only if a worker never has (it always has, in every environment).
   Workers of the new image launch verify-only on that current schema.
2. Then open and merge the deployment PR (D8). The worker pods restart once with their new init
   container; the runs serialize under the lock.

No data migration. Rollback of step 1: the previous build's `env.py` and workers migrate as
before; the lock is not persistent state. Rollback of step 2 alone is harmless: workers go back
to depending on the API's init container.

## Open Questions

None that change the specs or tasks. The pinned key value (D3) is computed during apply.
