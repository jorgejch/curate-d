# Workflow recovery

Workflows strand in two ways, and nothing in DBOS recovers either on its own:

- **A retired version.** DBOS pins a workflow to the application version that started it, and
  only that version's workers dequeue it. Once a rollout retires the version, its PENDING and
  ENQUEUED rows wait for workers that never come.
- **A retired executor.** Each worker recovers its own PENDING rows at startup, by its stable
  executor identity (its pod name). A pod that never comes back, such as a StatefulSet scaled
  down after a hard kill, leaves its PENDING rows owned by a dead identity.

The recovery sweep (`runtime/sweep.py`) handles both. It forks a retired version's rows onto
the latest version, under a deterministic successor ID, then cancels the originals. It
resumes a dead executor's rows onto their own queues. It runs on the `recovery-sweep`
persisted schedule on the I/O profile (`recovery_sweep_schedule`, default every ten minutes),
and on demand through the operator sweep.

## Gates

The sweep never touches a version that some process still runs.

| Gate | Rule | When it fails |
| --- | --- | --- |
| Target | The target is DBOS's latest version, read when the sweep starts. Only a sweeper running the target forks or retires. | A sweeper on another version counts those rows as `skipped` and still re-enqueues dead executors' rows. |
| Registration | A version's rows are forked only if it registered in `lease_versions` before the target. Workers register before launch. | Unregistered: `recovery_sweep.unregistered_version`. Registered after the target, as after a rollback: `recovery_sweep.newer_than_target`. Both are left alone. |
| Version lock | The sweep forks or retires a version's rows only while it holds that version's lock exclusively. Every live worker holds its version shared. | Held: `recovery_sweep.version_live`, with the mode held (`shared` for live workers, `exclusive` for another sweep). A lock error puts the rows in `failed`. |
| Promotion fence | Every fork and retirement runs under the promotion fence, held shared and tried once without waiting. Promotion holds it exclusively. | A promotion holds it or waits for it: every fork and retire row is `skipped` (`recovery_sweep.skipped`, reason `a promotion is in progress`); dead executors' rows are still re-enqueued. A fence error puts the rows in `failed`. |
| Latest re-read | Once it holds the fence, and again once it holds each version's lock, the sweep reads the latest version again. | If it changed, nothing more is forked, and the rest is counted as `skipped`. |
| Executor lock | A PENDING row is re-enqueued only while the sweep holds its executor's lease. Under it, the sweep re-reads the row, resumes it only if it is still PENDING under that executor, and reads it back. It resumes a row at most `MAX_SWEEP_REDISPATCHES` (10) times per application version, counted in the row's workflow attributes, because a resume resets DBOS's own recovery-attempt count. | Live executor: `left_running`. Unknown liveness: `failed`. Changed since listing: left alone. Read back neither ENQUEUED nor running elsewhere: `recovery_sweep.redispatch_unverified`, in `failed`. At the cap: `recovery_sweep.redispatch_cap`, in `failed`, not resumed. |
| Queue | A row is forked or re-enqueued only onto one of curate.d's declared queues (`QueueName` in `runtime/queues.py`). A NULL queue is refused because DBOS sends a queue-less resume to its internal queue, which every worker dequeues; DBOS's internal queue is not a declared queue. | `recovery_sweep.queue_refused`, in `failed`. Retiring such a row still cancels it. |
| Lock sessions | Before each fork, and before every cancel of a forked original or a stale tick, the sweep asks the promotion fence's session and the source version lock's session whether they still hold. After the cancel, the original must read CANCELLED. | Lost before the fork: the row is `failed`, `recovery_sweep.version_lock_lost`, and the rest of the batch is `skipped`. Lost before the cancel of an original whose successor is confirmed, forked this tick or found committed: the original is still cancelled, then the same. Lost before a stale tick's cancel: the tick is left, then the same. Not CANCELLED: `recovery_sweep.lineage_unverified`, in `failed`. The next tick's retire plan finishes the lineage. |

A stale tick of `recovery-sweep` itself is cancelled under the same gates (`retired_ticks`),
never forked or re-enqueued; the next tick does its work. Rows past DBOS's recovery attempts
(`MAX_RECOVERY_ATTEMPTS_EXCEEDED`) are left for an operator. `DELAYED` rows wait until they
become `ENQUEUED`. A resumed row's timeout clock restarts: DBOS clears its deadline on resume.

Every tick reports `left_running` of at least 1, because its own row is PENDING on a live
executor. Don't alert on it.

## Alerts

Alert on these events:

- `recovery_sweep.fork_failed`: one row could not be forked; see Triage.
- `recovery_sweep.unregistered_version`: rows of a build from before the version lease; see
  [reset.md](reset.md#after-the-version-lease-first-rolls-out).
- `recovery_sweep.newer_than_target`: rows of a build newer than latest, after a rollback;
  roll forward, or promote, as under Rollback and roll-forward.
- `recovery_sweep.redispatch_unverified`: a resumed row read back neither ENQUEUED nor
  running elsewhere. Inspect it (`DBOSClient.list_workflows(workflow_ids=[<id>])`); the next
  tick tries again while it is still PENDING under the dead executor.
- `recovery_sweep.redispatch_cap`: a row was resumed `MAX_SWEEP_REDISPATCHES` times under its
  version, and most likely kills the worker that runs it. Find why from that worker's logs,
  then cancel the row, or fix it and clear its count (Triage).
- `recovery_sweep.version_lock_lost`: a lock session dropped mid-batch; the batch stopped and
  the next tick carries on. If it repeats, check the database and pooler connections; see
  [Connection pooling](#connection-pooling).
- `recovery_sweep.lineage_unverified`: a forked original did not read CANCELLED after its
  cancel. The next tick retires it again; if it stays live, cancel it with
  `DBOSClient.cancel_workflow(<id>)`, since its successor carries the work.
- `worker.schedule_reconciliation_unverified`: a worker lost the schedule authority, or saw
  the latest version move, while writing schedules. A worker on the latest version repairs the
  rows within `schedule_reconciliation_interval_seconds`; if it repeats, check that an I/O
  worker of the latest version runs, then the pooler mode under
  [Connection pooling](#connection-pooling).
- `worker.schedule_reconciliation_failed`: a periodic reconciliation raised (WARNING for a
  database error, ERROR for anything else). The next interval retries; if it repeats, read
  the error.
- `workflow_history.revived_during_prune`: a row the retention prune listed was no longer
  terminal and aged when it re-read the batch just before the delete; it was kept. No action
  unless it repeats; then find who resumes rows older than `workflow_retention_days` (see
  Known gaps).
- `worker.drain_state_unknown`: a stopping worker could not read whether it left workflows
  running, so it keeps its executor lease until the process exits. If it repeats, check the
  database's reachability at shutdown.

## Connection pooling

The recovery machinery holds session-level advisory locks on connections the app opens
itself through `runtime/lease.py`: the executor lease (every worker), the version lease and
promotion fence (`runtime/version_lease.py`, workers and the sweep), and the schedule
authority (`runtime/schedule_authority.py`, each I/O worker at startup and then every
`schedule_reconciliation_interval_seconds`, default 600 s, from `worker/schedules.py`).

A session lock lives only on one pinned server connection. A transaction-mode pooler, as
PgBouncer defaults to, swaps the server connection between statements. The lock is taken on
one backend and the next statement runs on another, so probes report the lock lost, the
unlock misses it, and the orphaned lock blocks that key until the pooler recycles the
backend. You would see `recovery_sweep.version_lock_lost`,
`worker.schedule_reconciliation_unverified` and executor-lease loss at startup.

The ledger database URL must therefore reach PostgreSQL directly or through a session-mode
pooler. That pooler must also:

- detect vanished clients with TCP keepalives, so a dead worker's locks are released;
- keep `server_reset_query` at its default `DISCARD ALL`, which releases advisory locks when
  the client goes. An emptied reset query hands a dead worker's lock to the next client.

Both environments connect through a CloudNativePG `Pooler` (PgBouncer) in session mode, with
keepalives that close a dead client within about 90 seconds and the default reset query. The
deployment repository's manifests and their tests pin session mode and the URL's route
through it. `compose.yaml` connects directly, with server-side `tcp_keepalives_*` set.

For capacity, count one extra short-lived pooler client per I/O replica per reconciliation
interval, on top of each worker's two long-lived lease sessions.

## Before deploying

A build reads every stored configuration version as strictly as a submission, except for the
taxonomy check and retired fields. A version in force that it cannot read, such as a policy
naming a profile it dropped or a template set conditioned on an unknown `media_kind`, fails
every read of its kind: reconciliation, classification, review resolution and dry runs fail
with a `ConfigurationError`, `GET /configuration/<kind>` fails, and
`PUT /configuration/taxonomy` is refused. Check the stored versions first:

```bash
kubectl -n <namespace> exec statefulset/curate-d-worker-io -- python -m curate_d.configuration.verify
```

- **When:** before the rollout, from the image about to be deployed, against the target
  database. Run from the current build, it checks nothing the new build changed.
- **What it checks:** it parses every stored version of every kind with the read parsers,
  on one read-only connection, and writes nothing. Each version that fails is logged as
  `configuration_verify.unreadable` with its kind, version, whether it is in force, and its
  problems: an ERROR for a version in force, a WARNING for an older one. A database with no
  stored versions passes.

  | Exit | Meaning |
  | --- | --- |
  | `0` | Every version in force reads. Older versions that fail are listed but don't change the code. |
  | `1` | A version in force does not read. Don't roll out. |
  | `2` | The database could not be read (`configuration_verify.database_unreadable`), the settings did not load or the engine could not be built (`configuration_verify.setup_failed`), or invalid arguments; nothing was checked. |
- **Recovery:** `PUT` a corrected document for each failing kind through the current build's
  control plane, one the new build also reads, which makes it the version in force; then run
  the check again until it exits `0`. Policy and template activation never read the version
  in force, so their `PUT` succeeds while it is unreadable. A taxonomy `PUT` is refused while a
  policy or template set in force does not read, so correct those first. A policy or template
  `PUT` is checked against the taxonomy in force, so it is refused while that taxonomy does not
  read.

**Pickled rows across a deploy.** DBOS pickles workflow inputs and step outputs, so a row
written by the build being replaced reaches the new one whenever the recovery sweep forks it
across versions, and a fork copies every completed step's output. A deploy is blue-green with
a budget of 15 minutes: promote the new version (`python -m curate_d.runtime.promote`, under
Rollback and roll-forward), so schedules and new enqueues move to it within one
`schedule_reconciliation_interval_seconds`; let the old version's workers drain what they
hold; stop them after at most 15 minutes. What they leave is forked by the sweep, and that
fork is the exception, not the deploy path. A drain command and the pipeline gate are #74.

A forked row is reconstructed by `DomainModel.__setstate__`: a field added since the row was
pickled reads as its declared default, which must mean what the old build did; a required
field the pickle lacks raises `StaleCheckpointError`, naming the model and the fields, and
the row ends ERROR (Triage). A change that adds a required field to a pickled model, or
retypes one, is therefore a breaking deploy: the drain must reach zero before the old workers
stop. Say so in the PR.

## Rollback and roll-forward

DBOS registers a version again without retiming it. A build deployed a second time, by
rollback or by rolling forward to it, therefore stays non-latest. Every schedule then ticks onto
a version nothing runs, version-less API enqueues wait unrun, and the sweep targets the wrong
build. Promote it explicitly:

```bash
kubectl -n <namespace> exec statefulset/curate-d-worker-io -- python -m curate_d.runtime.promote <version>
```

- **When:** before the target build's workers start, so they apply their schedules at startup.
  Promoted afterwards, they are re-applied within `schedule_reconciliation_interval_seconds`
  (default 10 min); restart the I/O workers to apply them at once.
- **Where:** from an image that has the module. A build from before the recovery sweep lacks it,
  so run the command from a current image against the same database.
- **What it checks:**
  - it refuses a version DBOS does not list for this application;
  - it refuses a local clock more than 1 s off the database's, because DBOS stamps the
    promotion with the caller's clock;
  - it changes the latest version only while it holds the promotion fence exclusively, so no
    sweep forks onto the version it replaced; it waits up to `--fence-timeout` seconds
    (default 120) for a sweep mid-batch, and new sweeps skip their forks while it waits;
  - it logs the version it replaced;
  - it exits non-zero unless the latest version reads back as `<version>`.

  | Exit | Meaning |
  | --- | --- |
  | `0` | Promoted; the latest version reads back as `<version>`. |
  | `1` | Refused, failed, or read back as another version. A read-back mismatch happens after the write. |
  | `2` | Invalid arguments. |
  | `3` | A recovery sweep held the promotion fence past `--fence-timeout`; nothing was written. Retry. |
- **When not to promote:**
  - a first deploy of a new build, which its first launch registers as latest;
  - a rollout still in progress, because the newer replicas may only be crash-looping.

Workers never promote themselves. A worker can't tell a rollback from newer replicas
crash-looping mid-rollout (the I/O and CPU StatefulSets roll independently), and a wrong
promotion persists until someone promotes again.

The fence matters on a rollback. A sweep forks only versions registered before its target, so
had it kept forking onto the replaced newer build after the promotion, the restored build's
sweeper could never fork that work back, and it would wait for the roll-forward. A roll-forward
is not fenced: a new build's first launch makes it latest without promotion, and a sweep
mid-batch finishes forking onto the old target. That heals itself, because the old target
registered before the new one: the new build's sweeper forks those rows once the old build's
workers are gone.

After a rollback, the newer build's rows are `newer_than_target` and stay put. Rolling forward,
then promoting that build if it isn't latest, lets its workers resume them through startup
recovery. Rolling back to a build from before the recovery sweep stops the sweep: that build
deletes the undeclared `recovery-sweep` schedule, and the roll-forward applies it again.

## Scale-down: the operator sweep

The schedule recovers a removed pod's rows within one interval. To recover them now, or
when the schedule can't run (it is paused, it was deleted by a rollback, or no I/O worker runs
the latest version), run the sweep by hand in a pod of the latest build:

```bash
kubectl -n <namespace> exec statefulset/curate-d-worker-io -- \
  python -m curate_d.runtime.operator_sweep --target-version <latest version>
```

It never launches DBOS: it drives the sweep through `DBOSClient`.

| Exit | Meaning |
| --- | --- |
| `0` | Done; no row failed. Every `SweepOutcome` field is printed as JSON. |
| `1` | Done, but some rows are in `failed`. |
| `2` | Refused, nothing written. The target is missing, isn't both DBOS's latest and this build's version, never registered, or its version lease couldn't be taken. |
| `3` | The version lease was lost mid-run; nothing was written after the loss was detected. |

A local checkout's version never matches a deployed build, so run it in the cluster. It is safe
alongside live workers and a scheduled sweep: successor IDs, executor leases and version locks
arbitrate between them.

## Triage

**`recovery_sweep.fork_failed`, every tick, for one workflow.** The usual cause is a hand fork
of a still-pending row. Its successor isn't the recovery successor
(`recovery:<sha256 of the original ID>`), so the sweep refuses to fork it again. Check the
original's lineage (`DBOSClient.list_workflows(forked_from=<id>)`). Once the hand fork is the
one you want, cancel the original with `DBOSClient.cancel_workflow(<id>)`, and the sweep stops
reporting it.

**`StaleCheckpointError` in a workflow's error.** A row pickled by an older build reached
this one lacking a required field; its forked successor ended ERROR before any step ran. It
is a deploy that stopped the old workers before they drained, or a hand fork. Nothing is
half-done: the error is raised while the input or a copied step output is reconstructed. A
placement is started again by the next reconciliation pass; a reconcile part runs again on
its schedule; a dry run is started again by hand. Then find the field in the error and the
build that added it without a default, since that is the bug.

**Hand resume and fork.** Always pass `queue_name` (and `queue_partition_key` on a
partitioned queue). Without one, DBOS assigns its internal queue, which every worker dequeues.
The workflow could then run on the wrong profile, crossing the credential split, and the sweep
refuses to recover such a row.

**Recovered originals in the jobs list.** A forked original ends CANCELLED, so the API's
jobs list shows it as a failed job until retention collects it. Its successor, linked by
`forked_from`, carries the work.

**Kill switch.** Pause the schedule, and resume it when ready:

```python
from curate_d.config import get_settings
from curate_d.runtime.dbos_app import client

dbos = client(get_settings())
dbos.pause_schedule("recovery-sweep")  # dbos.resume_schedule("recovery-sweep") undoes it
```

**Changing a schedule during an incident.** The declarations in code and their settings are
the only source of a schedule's cron, queue and context: a hand edit to a DBOS schedule row is
reverted within one `schedule_reconciliation_interval_seconds`. Pause the schedule, which
survives reconciliation, or change `recovery_sweep_schedule` and restart the I/O workers.

**Cancelling a dead executor's rows.** A resume is unconditional, so a sweep resuming a
PENDING row of a dead executor can undo a cancel that lands just before it (Known gaps: resume
after a cancel). Pausing the schedule stops future ticks only: a tick already ENQUEUED or
running still finishes. So:

1. Pause the `recovery-sweep` schedule (Kill switch), and don't run the operator sweep.
2. Wait until no tick is ENQUEUED or PENDING, that is until this returns an empty list:

   ```python
   dbos.list_workflows(name="recover_stranded_workflows", status=["ENQUEUED", "PENDING"])
   ```

   A tick left PENDING by a dead executor waits for that executor's recovery; cancel it with
   the rows rather than wait.
3. Cancel the rows.
4. Resume the schedule.

A cancel is safe once no sweep is running: the next sweep's re-read sees CANCELLED and leaves
the row.

**Clearing a row's redispatch count.** The sweep keeps it in the row's workflow attributes,
under `curate_d.recovery_redispatches` and `curate_d.recovery_redispatch_version`, and starts
again from zero under a new application version. A row enqueued without a version, as the API
enqueues, takes the dequeuing worker's version, so the count is always keyed by a real version.
To let it resume a row at the cap under the same version, remove both keys. An update
replaces every attribute, so keep the others:

```python
from curate_d.config import get_settings
from curate_d.runtime.dbos_app import client

dbos = client(get_settings())
[status] = dbos.list_workflows(workflow_ids=["<id>"])
counter = {"curate_d.recovery_redispatches", "curate_d.recovery_redispatch_version"}
kept = {key: value for key, value in (status.attributes or {}).items() if key not in counter}
dbos.update_workflow_attributes("<id>", kept)
```

## Integration tests and the local latest version

The recovery and promotion integration tests promote their own test versions, and restore
the previous latest version on teardown. If a run is interrupted before teardown, the local
stack's build stays non-latest: its schedules stop ticking and API enqueues wait. Promote it
back:

```bash
docker compose exec worker-io python -m curate_d.runtime.promote <the stack's version>
```

`docker compose exec worker-io python -c "from curate_d.runtime.dbos_app import APPLICATION_VERSION as v; print(v)"`
prints that version.

## Known gaps

The follow-up `harden-recovery-sweep`, which moves the sweep's writes out of its step,
addresses the two that depend on that: writes outside the lock and the fork boundary.
dbos-transact-py#886 is an upstream DBOS bug, left to a DBOS fix. The rest are accepted for
this release: no replay compatibility check, redispatch count before the resume, resume after
a cancel, schedule authority check to write, and prune re-read to delete.

- **Writes outside the lock.** Forks, cancels and resumes still commit on DBOS's own
  connections, not on the lock's session. The sweep detects a lost fence or version lock
  before each fork and before every cancel, stops the batch (`recovery_sweep.version_lock_lost`),
  and verifies every resume and cancel by reading the row back. What remains is the window
  between a check and the write it guards. A source-version worker that starts there can
  revive an original as it is forked: the original runs until its next step, where the cancel
  stops it, and the steps it completed past the fork point run again under the successor (the
  fork boundary, below). A promotion that lands there lets that one fork go to the replaced
  target. The lease monitor polls every 5 s, so a worker can also run briefly after losing
  its lease.
- **No replay compatibility check.** A fork onto code that can't replay the original's
  checkpoints isn't prevented. A missing required field fails by name (`StaleCheckpointError`,
  Triage) and an added field with a default is filled, but a retyped or renamed field, or a
  changed step sequence, is not detected. Review a version bump for replay compatibility; the
  pickled-model baseline test of #74 makes the first case a declared decision.
- **The fork boundary.** A fork copies step outputs only. A ledger transaction that committed
  without its step checkpoint runs again under the successor.
- **dbos-transact-py#886.** A superseded execution's exception can become a datasource step's
  durable error, which a scheduled sweep turns from a rare takeover into routine work.
- **Redispatch count before the resume.** The sweep records a row's redispatch count before
  it resumes the row. A crash between the two over-counts by one, so the row reaches
  `MAX_SWEEP_REDISPATCHES` one resume early; it is never resumed uncounted.
- **Resume after a cancel.** A resume is unconditional; an operator cancel of a dead
  executor's PENDING row landing between the sweep's last re-read and its resume is undone,
  and the read-back cannot tell. Before cancelling a dead executor's rows, pause the
  `recovery-sweep` schedule, wait until no `recover_stranded_workflows` tick is ENQUEUED or
  PENDING, and don't run the operator sweep; resume the schedule after (Triage: cancelling a
  dead executor's rows). A cancel is safe once no sweep is running: the next sweep's re-read
  sees CANCELLED and leaves the row.
- **Schedule authority, check to write.** A reconciliation's writes commit on DBOS's pool, not
  on the authority's session, so a worker that loses the authority, or sees latest move,
  between its check and its write can leave a stale schedule row. It reads back after and
  logs `worker.schedule_reconciliation_unverified`; a worker of the latest version rewrites
  the rows within one `schedule_reconciliation_interval_seconds`.
- **Prune re-read to delete.** DBOS has no conditional delete, and a delete leaves nothing to
  read back. The retention prune re-reads each batch under its terminal and cutoff filter
  just before deleting it, which keeps a row resumed before the re-read
  (`workflow_history.revived_during_prune`), but a hand resume of a row older than
  `workflow_retention_days` landing between the re-read and the delete loses that run: its
  record and its aged checkpoints are deleted. Don't resume aged terminal rows; enqueue new
  work under a new ID.
