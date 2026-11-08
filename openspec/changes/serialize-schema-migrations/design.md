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
- Deployment (the maintainer's deployment manifest repository, read at its default branch on
  2026-11-07, as context only): the API Deployment (2 replicas, rolling update) runs
  `alembic upgrade head` in a `migrate` init container on every pod start, from the same image,
  through the session-mode pooler; its comment notes that pods starting together race and
  nothing serializes them. Both worker StatefulSets (2 replicas, `podManagementPolicy:
  Parallel`) have no init container; a worker that starts before the API's migration finishes
  fails `register_version` and restarts. Dev runs one pod of each role, which is why the race
  has not fired. The maintainer decided (2026-11-07) that this open-source repository must be
  self-contained: the guarantee lives in the application, and no design here may depend on a
  change to a deployment repository.

## Goals / Non-Goals

**Goals:**

- Any number of `alembic upgrade` / `alembic downgrade` processes against one database, started
  by anyone: one migrates, the rest wait, then find the schema current and exit 0. The Makefile,
  the reset script and any deployment keep calling `alembic upgrade head` unchanged.
- One migrator for all three schemas in the ledger database (DBOS system schema, the datasource
  tracking table, the ledger revisions); nothing else writes DDL. A process that runs workflows
  refuses to start on a schema behind its build.
- A waiter that gives up is explicit: one named ERROR event, non-zero exit, nothing changed.
- An older image starts on a newer, backward-compatible schema without a hand step, so a
  deployment that migrates from every pod can roll back.
- Everything above lives in this repository and holds for every deployment of it.

**Non-Goals:**

- Serializing a migration against running workers' DML; a migration that needs a drained
  pipeline stays a breaking deploy (`docs/recovery.md`).
- A separate DBOS system database (unsupported, CLAUDE.md).
- Deciding how or when a deployment invokes the migration (an init container on every pod, a
  release job, a hand step). That is the deployer's choice; this change makes every choice safe.
- A database role without DDL rights for the workloads; every workload keeps the one role.
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

### D2. The wait is a poll of `pg_try_advisory_lock`, each probe in its own committed transaction

A waiting run probes `SELECT pg_try_advisory_lock(:key)` and commits after every probe, so the
probe's snapshot ends at once; between probes it sleeps a short fixed interval (0.5 s). The loop
ends when a probe returns true or when `Settings.migration_lock_timeout_seconds` (`float`,
`gt=0`, default `1800.0`, same form as `staging_transfer_timeout_seconds`) has elapsed since the
first probe; then the run logs `migration.lock_timeout` and exits 3 (D5). 30 minutes covers every
migration in the chain with room, and a stuck holder is reported, not waited on forever. Nothing
sets `lock_timeout`, so Alembic's DDL keeps today's unbounded table-lock waits.

Why not the first choice, a blocking `SELECT pg_advisory_lock(:key)` under `lock_timeout`
(the `lock_within` pattern): it deadlocks with DBOS's own migration. `DBOS.migrate` runs
`CREATE INDEX CONCURRENTLY`, which waits for every transaction in the database whose snapshot is
older than its own (PostgreSQL's `WaitForOlderSnapshots`). A run blocked inside
`pg_advisory_lock` holds a statement snapshot for as long as it waits. So the holder's index
build waits for the waiters, the waiters wait for the holder, and the cycle runs through the
holder's client process, where PostgreSQL's deadlock detector cannot see it. Three concurrent
`alembic upgrade head` on an empty database hung until the 1800 s timeout (found by the Stream F
test, 2026-11-07). A blocked `pg_advisory_lock` has this problem in autocommit too: the snapshot
belongs to the statement. The polling probe never holds a snapshot across a sleep, so the index
build proceeds. The cost is a wake-up latency of one interval and a sleep in the loop, which
`ExecutorLease.acquire` already accepts for the same reason. Any later DBOS version that adds
another `CONCURRENTLY` migration is covered by the same rule: no session that waits for the
migration lock may hold a snapshot while it waits.

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

The two DBOS steps write on connections of their own, and the public API gives no way to run
them on the lock's connection. So the run follows CLAUDE.md's rule for that case: lock on our
own connection, write through the public API, verify afterwards that the lock still holds, and
name one ERROR event when it does not. After each DBOS step, `env.py` asks the lock's session
whether it still holds the key (`holds_lock`, factored out of `PostgresLeaseSession.holds` as
`describe_holder` was). A dead session answers with a disconnect (`DBAPIError` with
`connection_invalidated`); a live session cannot lose a session lock, but pg_locks is asked
rather than assumed. A loss, there or during Alembic's own step, logs `migration.lock_lost`
(ERROR, with the step) and exits 4 with no ledger revision applied. The window itself stays:
a DBOS step already running when the session dies finishes without the lock, and a waiter may
start its own DBOS step beside it. Detection makes that visible and keeps the ledger step out of
it; both DBOS steps are idempotent, so the repair is to run the migration again (added after
review, 2026-11-07).

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
- `migration.lock_lost` (ERROR): the lock's session ended mid-run (D4); names the step that
  found it. `MigrationLockLostError` is raised and `env.py` exits with code 4. No ledger
  revision was applied; the DBOS steps are idempotent, so the repair is a rerun.
- `migration.lock_released` (INFO) after unlock, with `outcome` committed or rolled back.

No heartbeat from the holder: the waiter's one line names the holder's pid, application name,
address and backend start, and the holder's Alembic log shows its progress.

### D6. Downgrades and offline mode

`alembic downgrade` runs the same `run_migrations_online` path, so it takes the lock and the DBOS
and datasource steps run (both no-ops on a current schema). Offline mode stays lock-free and keeps
its note that the DBOS schema is migrated separately; the module docstring says both.

### D8. The application guarantees safety, not scheduling

This is the DBOS-recommended split (`dbos-python` skill, `references/lifecycle-config.md`,
"Least-Privilege Deployments"): a migrate step out of band, the application with
`run_migrations=False` so launch only verifies. DBOS leaves "before the application starts" to
the deployer and documents nothing about concurrent migrate runs. This change fills the second
gap inside the application and leaves the first where DBOS leaves it:

- `alembic upgrade head` is correct from any number of invokers at once (D1 to D3), so an init
  container on every pod, a release job and a hand `make migrate` are all safe, alone or
  together.
- A process that runs workflows refuses to start on a schema behind its build (D4), so a
  deployment that skips the step fails loudly and restarts, instead of running on the wrong
  schema.
- An upgrade on a schema ahead of its build exits 0 (D9), so a deployment that migrates from
  every pod can roll an older image out onto a newer, backward-compatible schema.

Nothing in this repository names, reads or requires a deployment repository
(`tests/unit/test_isolation.py`). The maintainer's deployment keeps its API init container as
it is, and becomes correct the moment an image with this change runs; adding the init container
to the workers or moving to a release job is a later choice there, outside this change.

Alternatives considered and rejected:

- Worker init containers or a Flux release job in the deployment repository: both put the
  guarantee in a private repository, which the maintainer ruled out for an open-source
  application. Either remains a valid deployer choice on top of this design.
- The worker migrating in-process before `register_version`, under the same lock: fully
  self-contained, but it puts DDL back in the worker, and it breaks the stop-then-migrate order
  of a breaking deploy. The skill marks migrate-on-launch as the wrong production pattern.
- A curate.d-owned release command (`python -m curate_d.migrate`): adds a name over
  `alembic upgrade head` and nothing else; `make migrate` already is that name.

### D9. An upgrade on a schema ahead of its build is a no-op that exits 0

After it takes the lock, `env.py` reads the database's current heads
(`MigrationContext.configure(connection).get_current_heads()`) and resolves each against the
build's `ScriptDirectory`. An unknown revision means a newer build migrated this database. For an
upgrade (the Alembic command, read from `config.cmd_opts`; only a missing `cmd_opts`, as from a
programmatic call, counts as an upgrade, and every other command, `stamp --purge` the operator's
repair of `alembic_version` included, reaches Alembic) `env.py` logs `migration.schema_ahead` (WARNING, the
database revision and the build's head), runs none of the DBOS, datasource and Alembic steps,
releases the lock and exits 0. A newer build has already migrated all three schemas, and
`DBOS.migrate` against a newer DBOS schema is undocumented, so the DBOS steps are skipped too. A
downgrade from an unknown revision is left to Alembic, which fails and changes nothing: an
operator who downgrades wants a result, not a silent exit.

Why: a deployment that runs the migration from an init container on every pod blocks a rollback
without this: the old image's `env.py` fails on `Can't locate revision`, the pod never starts,
and someone must downgrade the schema by hand. The event is WARNING, not ERROR: it is the
expected path of a rollback. It is listed in `docs/recovery.md`'s second list (logged, not
alerted on). The workers accept a DBOS schema ahead of their build (public docs), and a newer
ledger schema must stay backward compatible within a release series, which the deployment
already requires; a breaking migration follows `docs/recovery.md`'s breaking deploy and its own
rollback boundary.

### D10. Twelve-Factor rules this change touches

- V.3 (complexity in the build, minimal run) and XII.1 (admin tasks as one-off processes): the
  application side is done here: no process of this repository migrates at launch (D4), the
  migration is one idempotent, serialized admin command (D1 to D3), and `make migrate` is its
  name. What `docs/twelve-factor.md` can no longer promise is that a deployment runs it once per
  release rather than from every pod: that is the deployer's choice (D8). Stream E rewrites both
  rows' evidence to say so; the status stays `Gap`, tracked by the Roadmap item that owns these
  rules, with the note that the gap is now a deployment choice, not an application defect.
- No other rule changes status. The lock, the setting and the events touch III (config from the
  environment: the setting is an environment variable, as every setting is) and XI (logs as
  event streams: the events go to stderr like every other event) in ways that already conform.

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
- Integration (D9): upgrade the created database to head, then set `alembic_version` to a
  revision id that is not in the chain (an `UPDATE`, standing in for a newer build); run
  `alembic upgrade head`: exit 0, stderr holds `migration.schema_ahead`, `alembic_version` and the
  table set are unchanged. Then `alembic downgrade base` on the same database: non-zero exit,
  nothing changed.
- Unit: the key (D3); the setting's default, bound and error; `build_dbos_config()["run_migrations"]
  is False`; `create_ledger_datasource` passes `run_migrations=False`.

## Risks / Trade-offs

- [A reset kills a concurrent migrator] `ops/reset` ends every other session of the application
  role before it wipes; a migration running at that moment loses its connection, rolls back and
  exits non-zero. → Intended: the reset owns the database then; `docs/reset.md` says so.
- [A transaction-mode pooler breaks the session lock] → Same constraint as every lock holder; the
  Connection pooling section lists the migration among them. Both environments use session mode.
- [A worker of a new DBOS version starts before the migration] → Launch fails explicitly with
  `DBOSInitializationError` and the process restarts until the migration has run;
  `docs/recovery.md` Before deploying names the rule, and the breaking deploy step already runs
  the migration first. The deployment decides what runs the migration (D8); the application only
  refuses to run without it.
- [A 1800 s wait in a pod's init container delays a rollout] → Only while another run migrates;
  a no-op run takes the lock at once. The waiter's `migration.lock_waiting` line names the holder.
- [A schema ahead is a wrong database, not a rollback] → `migration.schema_ahead` names the
  revision; an environment's URL comes from one secret, so a wrong database is a secret error
  that `docs/recovery.md` triage would surface. Accepted.
- [The deployment still migrates from every pod] → Correct but not Twelve-Factor V.3/XII.1;
  recorded as a deployment choice in `docs/twelve-factor.md` (D10), not hidden.
- [The lock session dies while a DBOS step runs on its own connection] → The step finishes
  unprotected and a waiter may take the lock meanwhile; that window cannot be closed through
  the public API. The run verifies the lock after each DBOS step and before Alembic (D4), logs
  `migration.lock_lost`, exits 4 and applies no ledger revision; the integration test ends the
  holder's session mid-run and asserts exactly that. The DBOS steps are idempotent, so a rerun
  repairs. Accepted and named in `docs/recovery.md`'s alert list.
- [`DBOS.migrate` or the datasource migrate internally waits or fails under our lock] → They run
  one at a time under our lock, so their own contention path is never taken. If one raises, the
  run exits non-zero naming the cause and the lock is released in `finally`.
- [A waiter's snapshot blocks the holder's `CREATE INDEX CONCURRENTLY`] → The waiter polls and
  commits after each probe (D2), so it holds no snapshot while it sleeps. The concurrent-upgrade
  integration test on an empty database (D7) runs the DBOS index builds under contention and
  fails on a hang (pytest timeout well below the 1800 s default), so a regression is caught.
- [`configure_logging` in `env.py` changes Alembic's log output] → Alembic's logger config from
  `alembic.ini` is applied first; the integration tests assert both the Alembic lines and the
  named events are present in stderr.
- [Hash collision of the new key with an existing one] → 64-bit; the unit test shows the four
  keys apart for a like-spelled name, as the fence test does.

## Migration Plan

One step: merge the change; the deployment's image automation deploys it. Any existing init
container runs the new `env.py`: it finds the DBOS and ledger schemas current and creates the
datasource tracking table only if a worker never has (it always has, in every environment).
Workers of the new image launch verify-only on that current schema.

No data migration. Rollback: the previous build's `env.py` and workers migrate as before; the
lock is not persistent state, and the tracking table the new `env.py` may have created is the
same one the old worker would create.

## Open Questions

None that change the specs or tasks. The pinned key value (D3) is computed during apply.
