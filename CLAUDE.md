# curate.d

> **Load the `dbos-python` skill before any DBOS design or change** — a workflow, step,
> queue, schedule, datasource or `DBOSClient` call. It holds DBOS's own patterns,
> conventions and architecture guidance: decide against it, and treat a departure from it as
> a decision to record under [DBOS](#dbos). It is vendored in `.agents/skills/dbos-python`
> (`.claude/skills/dbos-python` links to it) and pinned in `skills-lock.json`. A change that
> bumps the `dbos` package version refreshes the skill from upstream (`dbos-inc/agent-skills`)
> and its `skills-lock.json` hash in the same change, so the guidance matches the DBOS in use.
> Keep it whole, as upstream ships it: don't trim it by hand, references to DBOS products
> curate.d does not use included.

## Changes

Make every change except a simple fix (a typo, a one-line bug fix, a doc correction)
through OpenSpec (`openspec/`): propose it (`openspec-propose`), apply it
(`openspec-apply-change`), sync its delta specs into `openspec/specs/`
(`openspec-sync-specs`), then archive it (`openspec-archive-change`). No ad-hoc plan files.

Write a proposal so its apply can run on several agents in parallel, as far as the change
allows: group `tasks.md` into work streams that touch disjoint files, state which streams
depend on which and which can start at once, and keep shared edits (a migration, a
settings field, this file) in one stream or one up-front task the others wait on. Say so
in the design when a change can't be split.

## Debugging

IMPORTANT: When debugging, prefer using pycharm-debugger MCP tools to interact with the IDE debugger.

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

- All ledger mutations executed within DBOS workflows MUST use a DBOS datasource
  transaction defined through `runtime/datasource.py`. Never perform ledger mutations
  through `@DBOS.step()`, unmanaged SQLAlchemy sessions, or direct database access
  within workflow functions. Ledger mutations and their DBOS execution checkpoints must
  commit atomically. This is the exactly-once guarantee. `make lint`
  (`python -m curate_d.runtime.workflow_audit`) catches raw engine/connection calls in
  workflow bodies; writes from steps or unmanaged sessions must be caught in review.
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
- No private internals of a library (an underscore module or attribute, or a copy of its
  private logic or SQL) without the owner's explicit approval.
  `tests/unit/test_no_library_internals.py` and ruff `TID251` (`banned-api` in
  `pyproject.toml`) fail the build on a DBOS one. Where the public API can't give a
  guarantee, as DBOS has no public write on a caller's connection: take the lock on our own
  connection (`runtime/lease.py`), write through the public API, verify afterwards that the
  lock and the premise still hold, log one named ERROR event when they don't, and let a
  periodic idempotent pass repair the drift. Name the event in `docs/recovery.md`'s alert
  list. A detection without a repair and an alert is "log and hope", not a guarantee.
- Migrations 0005, 0007, 0008 and 0009 (views and data migrations), `retention.py`'s prune
  of the datasource's `datasource_outputs` tracking table, and integration fixtures read
  DBOS's documented tables by raw SQL. That is a schema dependency, not a private API:
  re-check it on every DBOS upgrade (`tests/integration/test_workflow_views.py`,
  `test_migrations.py`). Those migrations need the DBOS system schema, so
  `migrations/env.py` brings it up to date before Alembic's own migrations run. The
  workflow-history prune itself goes through the public `list_workflows` and
  `delete_workflows`; it re-reads each batch's status before deleting it, and since a
  delete has no read-back, the re-read→delete window is a documented gap
  (`docs/recovery.md`, `workflow_history.revived_during_prune`).
- The recovery sweep (`runtime/sweep.py`, scheduled by `workflows/recovery.py`) forks,
  cancels and resumes from inside one `@DBOS.step`, and those DBOS calls are not
  checkpointed. That departs from the skill's rule against starting workflows from a step.
  A re-run repeats the whole sweep, which is safe only because every fork is keyed by the
  deterministic successor ID, every cancel is gated on a re-read status, and every resume on
  a re-read status under the dead executor's lease, re-read again right before the resume (a
  cancel landing inside that round trip is the documented gap); keep it that way. Each
  write is verified
  after: a resumed row must read back ENQUEUED or dequeued elsewhere
  (`recovery_sweep.redispatch_unverified`), a cancelled original CANCELLED
  (`recovery_sweep.lineage_unverified`). A resume resets DBOS's recovery-attempt count, so
  the sweep counts its own resumes of a row in its workflow attributes and stops at
  `MAX_SWEEP_REDISPATCHES` per application version (`recovery_sweep.redispatch_cap`). Before
  each fork and before every cancel, it asks the fence's and the version lock's sessions
  whether they still hold; a loss stops the batch (`recovery_sweep.version_lock_lost`).
  `harden-recovery-sweep` moves the writes out of the step.
- Version lease: every process that runs workflows (the workers, nothing else) registers its
  version in `lease_versions` before launch and holds that version's advisory lock shared, on
  its executor lease's session, until it exits (`runtime/version_lease.py`). The sweep forks
  or retires a version's rows only under that lock held exclusively, and only for a version
  registered before its target, DBOS's latest. Promoting a version is an operator command
  (`python -m curate_d.runtime.promote`, see `docs/recovery.md`); workers never promote.
  The promotion fence (`version_lease.py`) keeps latest from moving backwards mid-sweep: the
  sweep holds it shared, tried once, across every fork and retirement, and promotion holds it
  exclusively, waiting a bounded time. Don't fork or retire outside it.
- Schedule reconciliation holds the schedule authority (`runtime/schedule_authority.py`), a
  session advisory lock on a dedicated `runtime/lease.py` connection, under the key older
  builds take as a transaction lock, so mixed builds exclude each other. It writes through
  the public `DBOS.get_latest_application_version`, `apply_schedules`, `list_schedules` and
  `delete_schedule`, which commit on DBOS's own pool: a lost lock no longer rolls the writes
  back, so the reconciliation verifies afterwards that it still held the lock and that
  latest did not move, and logs `worker.schedule_reconciliation_unverified` when not. Every
  I/O worker reconciles again every `schedule_reconciliation_interval_seconds` from a thread
  (`worker/schedules.py`), not a workflow: a schedule re-applying the schedules would be
  self-referential, and `DBOS.apply_schedules` refuses a workflow context. The declarations
  are the only source of truth: a hand edit to a schedule row reverts within one interval; a
  pause survives. A promoted version's workers apply its schedules within one interval,
  without a restart.
- `rewind_workflow` re-executes steps from `start_step` after deleting their checkpoints, and
  a re-executed step appends new audit entries. That is intended: the trail keeps the
  original decision and the new one. Don't deduplicate audit entries by workflow or step.
- Rewind does not re-run child workflows. A rewound parent calling a child under the same
  deterministic ID gets the child's old result back, so rewind the children after the
  parent; never delete them (`docs/rewind.md`).
- DBOS pickles workflow inputs and step outputs, and the sweep's fork carries them across
  versions. `DomainModel.__setstate__` fills a declared default for a field added since the
  pickle was written and raises `StaleCheckpointError` for a missing required one. So a field
  added to a model a workflow takes or a step returns carries a default that means what the
  old code did; a required field, a retyped or a renamed one is a breaking deploy that must
  drain first (`docs/recovery.md`, Before deploying). Retire a field; never retype it.
- Nothing in curate.d rewinds a workflow, so the two points above concern a rewind run by
  hand; record who ran it, from which step and why in the issue tracking the incident. The
  rewind itself writes no audit entry.
- New periodic work uses persisted schedules: declare them in `schedules_for`
  (`workflows/registry.py`); `worker/schedules.py`'s `apply_schedules` writes them after
  launch, and its reconciler periodically, through the schedule authority.
  `@DBOS.scheduled` no longer exists.
