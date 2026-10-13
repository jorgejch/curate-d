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
re-enqueues a dead executor's rows on their own queues. It runs on the `recovery-sweep`
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
| Executor lock | A PENDING row is re-enqueued only while the sweep holds its executor's lease. | Live executor: `left_running`. Unknown liveness: `failed`. |
| Queue | A row whose queue is NULL or DBOS's internal queue is never forked or re-enqueued, because every worker dequeues the internal queue. | `recovery_sweep.queue_refused`, in `failed`. Retiring such a row still cancels it. |

A stale tick of `recovery-sweep` itself is cancelled under the same gates (`retired_ticks`),
never forked or re-enqueued; the next tick does its work. Rows past DBOS's recovery attempts
(`MAX_RECOVERY_ATTEMPTS_EXCEEDED`) are left for an operator. `DELAYED` rows wait until they
become `ENQUEUED`.

Every tick reports `left_running` of at least 1, because its own row is PENDING on a live
executor. Don't alert on it. Alert on `recovery_sweep.fork_failed`,
`recovery_sweep.unregistered_version` and `recovery_sweep.newer_than_target`.

## Rollback and roll-forward

DBOS registers a version again without retiming it. A build deployed a second time, by
rollback or by rolling forward to it, therefore stays non-latest. Every schedule then ticks onto
a version nothing runs, version-less API enqueues wait unrun, and the sweep targets the wrong
build. Promote it explicitly:

```bash
kubectl -n <namespace> exec statefulset/curate-d-worker-io -- python -m curate_d.runtime.promote <version>
```

- **When:** before the target build's workers start, so they apply their schedules at startup.
  Promoted afterwards, restart them.
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

These are left to the follow-up `harden-recovery-sweep`.

- **Unfenced writes.** Forks, cancels and re-enqueues commit on DBOS's own connections, not on
  the lock's session. A lost lock backend can therefore let a source-version worker start
  mid-sweep, and a lost promotion fence backend can let a promotion land mid-batch. The lease
  monitor polls every 5 s, so a loss can also precede its detection.
- **No replay compatibility check.** A fork onto code that can't replay the original's
  checkpoints isn't prevented. Review a version bump for replay compatibility.
- **The fork boundary.** A fork copies step outputs only. A ledger transaction that committed
  without its step checkpoint runs again under the successor.
- **dbos-transact-py#886.** A superseded execution's exception can become a datasource step's
  durable error, which a scheduled sweep turns from a rare takeover into routine work.
