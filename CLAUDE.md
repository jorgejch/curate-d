# curate.d

> **Load the `dbos-python` skill before any DBOS design or change** — a workflow, step,
> queue, schedule, datasource, `DBOSClient` or Conductor call. It holds DBOS's own patterns,
> conventions and architecture guidance: decide against it, and treat a departure from it as
> a decision to record under [DBOS](#dbos). It is vendored in `.agents/skills/dbos-python`
> (`.claude/skills/dbos-python` links to it) and pinned in `skills-lock.json`.

## Docstrings

Google-style. One-line imperative summary first, period-terminated ("Return the...",
"Compute...", "Create the source_objects table." — not "Returns the..." or "This function is
responsible for...").

- Every function, method and property takes the imperative summary, predicates included:
  "Return whether the host is loopback.", never "Whether the host is loopback.", "True
  if..." or a noun phrase. This overrides Google style's attribute-style property
  docstrings. Module and class docstrings aren't bound by it: they open with a descriptive
  one-line summary of what the module holds or what an instance is (a migration's states
  what the migration does, below).
- Add `Args:`, `Returns:`, `Raises:` sections when they carry information beyond the
  signature — a non-obvious parameter, a return value that isn't the plain type, a raised
  exception the caller must handle. Skip a section that would be trivial or empty (a
  no-arg function returning `None` doesn't need `Args:`/`Returns:` at all).
- Module docstrings: add one where the module's purpose isn't obvious from a glance
  (most of `domain/`, `ledger/`, `runtime/`, `api/`). Trivial re-export `__init__.py`
  files get a short one-liner.
- Pydantic/dataclass models: document the class itself and any non-trivial validators
  or computed properties. Plain fields already covered by the class docstring or field
  descriptions don't each need their own line.
- Alembic migrations (`migrations/versions/*.py`): the module docstring states what the
  migration does (see `0001_source_objects.py`). `upgrade()`/`downgrade()` each get a
  one-line docstring naming the table/column/index they act on; they don't repeat the
  module docstring's reasoning.
- Test functions (`test_*`): a single concise one-line summary of what the test shows,
  which may state the behaviour rather than give an imperative ("A failed audit write never
  reaches the command.") — no `Args:`/`Returns:`, fixtures are self-explanatory from their
  names. Add more only if the test body does something non-obvious. Fixtures, fakes and
  other test helpers are functions and take the imperative summary.

Keep the tone terse and technical, matching the rest of the codebase — state what the
code does, don't narrate that you're documenting it.

## Repository boundaries

No source file, test, migration or Make target may read another repository on the
machine: no sibling-path lookups, no environment variable pointing at another checkout.
Checks on deployment manifests or infrastructure code live in those repositories.
`tests/unit/test_isolation.py` fails the build on a reference to one.

## DBOS

DBOS 3.x, embedded as a library. Settle every DBOS pattern question — where an effect runs,
how it is made idempotent, whether it belongs in a workflow — against the `dbos-python` skill
(see the top of this file). This repo departs from it in these places; record any new departure here:

- Every ledger write inside a workflow goes through the ledger datasource
  (`runtime/datasource.py`), never a step or a raw session. This is the exactly-once
  guarantee, and `make lint` enforces it (`python -m curate_d.runtime.workflow_audit`).
- Create datasources before `DBOS.launch()`; DBOS 3 rejects one created after.
- Construct DBOS only through `runtime/dbos_app.py`, from an entry point, never at import
  time. Register queues through `runtime/queues.py`: `never_update` keeps limits changed at
  runtime across restarts.
- Workflow IDs are deterministic (`runtime/workflow_ids.py`) so duplicate enqueues
  collapse. Don't substitute random IDs.
- The DBOS system tables live in the ledger database (schema `dbos`), and separating them
  is not supported: one dump must hold the whole state, and a ledger write and a workflow
  enqueue must stay able to commit in one transaction (`DBOSClient.enqueue_in_transaction`;
  nothing uses it today, the executor management layer of #5 may). There is no setting for a separate system database; `runtime/dbos_app.py`
  derives the system URL from `ledger_database_url`. Test fixtures `DBOS.destroy()`,
  configure and launch; they never call `DBOS.reset_system_database`. Use unique workflow
  IDs per test instead.
- `retention.py`, `sweep.py`, `queues.py`, `conductor.py`, `worker/main.py` and
  `api/throughput.py` use private DBOS internals (`_workflow_commands.garbage_collect`,
  `_sys_db.reenqueue_for_recovery`, `dbos._utils.INTERNAL_QUEUE_NAME`, `dbos._queue`,
  `_active_workflows_set` to tell whether the drain left workflows running before the
  executor lease is released; `dbos._error.DBOSException`,
  `dbos._conductor.conductor.connect`, replaced at runtime, `ConductorWebsocket`'s
  constructor, `DBOS.conductor_websocket` for shutdown, the `dbos._conductor.protocol`
  dataclasses for refusal responses, and `dbos._dbos._get_dbos_instance`). Tests pin them;
  re-check them on every DBOS upgrade. `dbos._conductor` is moving to the commercial
  `dbos-enterprise` package (dbos-transact-py PR #873).
- Conductor is read-only: the gate (`runtime/conductor.py`) passes read messages to DBOS's
  handler and refuses every write (cancel, resume, the schedule commands, delete, the forks,
  rewind, version promotion, backfill, recovery, retention, import), enqueuing and
  auditing nothing. Conductor sends a write to every connected executor under fresh request
  IDs and cannot route one to our stable executor IDs after a rollout; see the
  `add-optional-dbos-conductor` design. Don't reopen a write path through Conductor.
- Schedule reconciliation runs in one system-database transaction that holds the
  schedule authority (`pg_advisory_xact_lock`), so losing the backend loses the lock and
  every pending write together. DBOS has no public way to write schedules on a caller's
  connection, so `runtime/schedule_authority.py` uses `_sys_db.engine`, `upsert_schedule`,
  `list_schedules`, `delete_schedule` (with `conn`), `SystemSchema.application_versions`,
  `SystemDatabase._name_filter`, and a copy of `DBOS.apply_schedules`'s row builder
  (`stored_form`, which also needs `_sys_db.get_queue`, `_sys_db.serializer`, `_registry`,
  `dbos._registrations`, `dbos._croniter`, `dbos._error` and `dbos._utils`).
  `tests/unit/runtime/test_schedule_authority.py` pins them; re-check them on every DBOS
  upgrade. Don't move those writes back onto `DBOS.apply_schedules` or
  `DBOS.delete_schedule`: they commit on their own connections, outside the lock.
- Migrations that read `dbos.*` tables (0005, 0007, 0008, 0009) need the DBOS system
  schema, so `migrations/env.py` brings it up to date before Alembic's own migrations run.
- `rewind_workflow` re-executes steps from `start_step` after deleting their checkpoints, and
  a re-executed step appends new audit entries. That is intended: the trail keeps the
  original decision and the new one. Don't deduplicate audit entries by workflow or step.
- Rewind does not re-run child workflows. A rewound parent calling a child under the same
  deterministic ID gets the child's old result back, so rewind or delete the children too.
- Nothing in curate.d rewinds a workflow, and the Conductor gate refuses `REWIND_WORKFLOW`,
  so the two points above concern a rewind run by hand; record who ran it, from which step
  and why in the audit trail.
- New periodic work uses persisted schedules: declare them in `schedules_for`
  (`workflows/registry.py`); `worker/main.py`'s `apply_schedules` writes them after launch
  through the schedule authority. `@DBOS.scheduled` no longer exists.
