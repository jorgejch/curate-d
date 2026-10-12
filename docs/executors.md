# Operating the DBOS executors

Each worker is a DBOS executor with a stable identity (its pod name) and recovers its own
PENDING workflows when it starts. This runbook covers what that leaves to an operator: work
stranded by an executor that never comes back, work pinned to an application version no
worker runs any more, and the one-off commands on a workflow (cancel, resume, fork, run a
schedule now).

Everything here goes through the control plane API. The CLI wraps it and is meant to be run
inside the API pod:

```sh
kubectl exec -it deploy/<api> -- python -m curate_d.manage executors
```

`--api` (or `CURATE_D_API_URL`) points it elsewhere; the default is `http://localhost:8000`.
Inside the pod no proxy names you, so pass `--operator you@example` (or set
`CURATE_D_OPERATOR`) and the API's logs record who acted.

Every write prints its request ID first:

```
$ python -m curate_d.manage recover worker-io-1
request_id 3f0b5c1e-...
```

If the answer is lost, run the same command again with `--request-id 3f0b5c1e-...`: it lands
once. A new request ID is a new command. Exit status: `0` done, `3` refused or conflicting,
`4` not found, `2` anything else.

None of these commands writes an audit entry. The audit trail records decisions about files;
these act on DBOS workflow rows. Their record is DBOS's own (status, `forked_from`, the
`curate_d_resumed_by_request` attribute a resume leaves) and the `operator_command.*` and
`recovery_sweep.*` log lines, which carry the operator and the request ID.

## Reading the executor view

`python -m curate_d.manage executors` (or `GET /executors`) lists every executor identity
that holds a lease or owns PENDING or ENQUEUED work:

| Column | Meaning |
|---|---|
| `LIVE` | A session holds the identity's lease (`pg_locks`). The API only reads `pg_locks`; it never takes a lock. |
| `VERSION` | The version whose lease the holding session also holds; `-` for a build that predates the version lease. |
| `PENDING` | Workflows the executor started and has not finished. |
| `OLDEST` | How long the oldest of them has been running. |
| `STRANDED` | PENDING work and no lease: nothing runs that work until it is recovered. |

Each version line says whether it is the `latest` and whether it is `running`: a worker
holds its version lease, or a live executor without a version lease still owns PENDING work
on it. `executors <id>` shows one executor; `--json` prints the API's answer, which also has
queue depth per queue, partition and status.

`lease_since` and the holder's name come from `pg_stat_activity`. If the API's database role
cannot see other roles' sessions they are null; liveness, which `pg_locks` answers, is
unaffected.

## Scaling down

A worker that receives SIGTERM drains: it stops dequeuing, lets running workflows finish
within its grace period, then releases its lease. A clean scale-down leaves nothing behind
and needs no recovery.

## A pod that crashed and was removed

A pod killed hard (OOM, node loss, `--force` delete) and then scaled away leaves its PENDING
workflows owned by an identity that no longer starts. `executors` shows it `STRANDED`. Either:

- scale the StatefulSet back up so that ordinal starts, recovers its own work at startup and
  can then be drained and scaled down cleanly; or
- recover it from here:

  ```sh
  python -m curate_d.manage recover worker-io-1
  ```

Recovery takes the executor's lease, so it is refused (exit 3) while the executor is alive:
a live executor recovers its own work. Holding the lease also keeps the identity from
starting halfway through. Its PENDING rows on the current version are re-enqueued under
their own IDs; its work on an older version is forked onto the current one (next section).
The report lists what was re-enqueued, forked, retired, skipped and why. Recovery runs as a
workflow on an I/O worker: if it takes longer than `executor_recovery_wait_seconds`
(default 60), the API answers 202 and the CLI polls until the report is ready.

## Version changes

Every dev image is its own DBOS application version, and a release bump is one too. Workers
dequeue only their own version's work, so a deploy strands the previous version's queued and
in-flight work until it is recovered:

1. Roll out the new version. Keep the old workers until they have drained (a rolling update
   does this pod by pod; blue-green keeps the old set until it is idle).
2. Once no old worker is left, recover everything:

   ```sh
   python -m curate_d.manage recover --all
   ```

   Work on a retired version is forked onto the current one under a deterministic successor
   ID, from the step after its last recorded output, keeping its queue and partition; the
   original is then cancelled. A repeat finds the successor and only finishes cancelling.

Forking is refused while the old version still runs: each worker holds its version's lease
shared, and recovery must take it exclusively before forking that version's work, and holds
it until the forks are made, so no worker of that version starts meanwhile. A refused
version is reported in `running_versions`, its rows in `skipped`; when that was all there was
to do the API answers 409 naming the versions. Run it again once the old workers are gone.

The first deploy that carries the version lease cannot see its predecessor's workers through
it, because they hold none. For that case a version also counts as running while a live
executor without a version lease still owns PENDING work on it. Still hold cross-version
recovery until the previous build's workers are gone.

## Refusals

| Answer | Meaning | What to do |
|---|---|---|
| `executor <id> is alive (lease held)` | The executor runs and recovers its own work. | Nothing; or drain it first if it is meant to go. |
| `versions still running: <v>` | A worker of `<v>` holds its version lease, or a live pre-lease worker owns PENDING work on it. | Wait for those workers to drain and go, then recover again. |
| `workflow <id> is PENDING ... recovered with recover, not resumed` | DBOS's resume would set the row back to ENQUEUED while an executor may still be running it. | `recover` its executor if that executor is gone. |
| `workflow <id> is <status>; only CANCELLED or MAX_RECOVERY_ATTEMPTS_EXCEEDED workflows are resumed` | Nothing to resume. | Fork it from a step instead, if it should run again. |
| `workflow ID <id> belongs to an unrelated workflow` | The fork's successor ID is taken by other work. | Use a new request ID. |
| `queue <q> is partitioned and workflow <id> has no partition key` | The fork cannot be dequeued from that queue. | Fork onto its own queue (omit `--queue`). |

## Commands on one workflow

```sh
python -m curate_d.manage cancel <workflow>
python -m curate_d.manage resume <workflow> [--queue <queue>]
python -m curate_d.manage fork <workflow> --start-step N [--version V] [--queue <queue>]
python -m curate_d.manage trigger <schedule>
```

- **cancel** is idempotent; a finished workflow is left as it is.
- **resume** applies to CANCELLED and MAX_RECOVERY_ATTEMPTS_EXCEEDED workflows only.
  Cancelling clears a workflow's queue, so a resumed one goes to DBOS's internal queue, which
  every worker dequeues from, unless `--queue` names the profile's queue it belongs on.
- **fork** copies the workflow's recorded steps below `N` and runs it from `N` on the latest
  version (or `--version`), under a successor ID derived from the request ID.
- **trigger** enqueues one run of a schedule's workflow now, on its queue and the latest
  version, under an ID derived from the request ID. It does not change the schedule.

Rewinding a workflow is not offered here; see issue #2.
