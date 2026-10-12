# DBOS Conductor

Curate.d can report its workers to hosted [DBOS Conductor](https://docs.dbos.dev/production/conductor)
for workflow dashboards and metrics. It is read-only: every write from the Console is
refused (see [Commands](#commands)). **Conductor is optional.**
With it off, unreachable, or rejecting the key, the system runs exactly as it does without it:
executor identity, startup recovery, the recovery sweep, retention and queues never depend on
it. The requirements are in the
[conductor-integration spec](../openspec/specs/conductor-integration/spec.md).

`<namespace>` below is the Kubernetes namespace the workers run in.

## How it connects

DBOS is never given `conductor_key`: DBOS 3.2 would then replace the stable executor ID with a
random UUID and skip startup recovery. Instead, after a worker holds its executor lease, has
launched DBOS and has registered its queues, `runtime/conductor.py` opens the connection
itself under its environment's application name (`curate-d-<environment>`, from
`CURATE_D_ENVIRONMENT`), through a gate that classifies every command
(see [Commands](#commands)). `DBOS.destroy()` closes it on shutdown.

| Process | Reports | Why |
| --- | --- | --- |
| I/O worker | yes | DBOS executor |
| CPU worker | yes, optionally through a forward proxy | DBOS executor |
| API | **no** | Not a DBOS executor: it only holds a `DBOSClient` and never launches DBOS. Launching DBOS there would run recovery and queues, which `identity.py` rules out. The code never makes the connection, even with the flag and key in its environment. |

Every executor appears in the Console under its pod name (`<pod>`, for example `curate-d-worker-io-0`) with its profile in the executor metadata.

## Settings

| Variable | Meaning / default |
| --- | --- |
| `CURATE_D_CONDUCTOR_ENABLED` | The switch. Default `false`. Off keeps Conductor off whatever key is set |
| `CURATE_D_CONDUCTOR_KEY` | The credential. Secret: never logged, redacted from exceptions |
| `CURATE_D_CONDUCTOR_URL` | `wss://`, or `ws://` to a loopback host only. Unset: `wss://cloud.dbos.dev/conductor/v1alpha1` |
| `CURATE_D_CONDUCTOR_PROXY_URL` | `http://` only, with an empty or `/` path. Used by the Conductor connection and nothing else; unset ignores `HTTPS_PROXY` |

A malformed URL or proxy URL fails settings validation, naming the setting, and the process
does not start. So does a plaintext `ws://` URL to any host but `localhost`, `127.0.0.1` or
`[::1]`, or one through a proxy that is not loopback: the key travels in the handshake path,
so plaintext never leaves the machine (the proxy's own `http://` scheme is fine under `wss://`,
where the `CONNECT` tunnel carries only ciphertext). A proxy URL with any other path (`http://proxy:3128/x`) passes validation
but fails every connect attempt as `conductor.connect_failed`; pydantic adds the trailing
`/` itself. DBOS's own `DBOS_CONDUCTOR_KEY` is never read.

DBOS puts the key in the websocket URL path. It logs that URL at debug level, websockets
logs the handshake's request line, and DBOS's loop logs every failed attempt's error text
and a rejected handshake's body, all on DBOS's logger. A filter on that logger removes the
key, raw or percent-encoded, from every record. The gate's own `conductor.connect_failed`
event has the key removed from its `reason` too.

Deployments set the flag on both worker processes and read the key from a secret with an
optional reference, so a missing secret never blocks a pod. Only the CPU worker may set
`CURATE_D_CONDUCTOR_PROXY_URL`. The API gets neither.

## Startup outcomes

Each worker logs exactly one of these at startup:

| Event | Level | Condition | Effect |
| --- | --- | --- | --- |
| `conductor.disabled` | info | flag off, with or without a key | no connection |
| `conductor.misconfigured` | error | flag on, key or `CURATE_D_ENVIRONMENT` unset (`setting` names which) | no connection; the worker runs normally |
| `conductor.connecting` | info | flag on, key and environment set | connection opened in the background; logs `profile`, `app_name`, `host`, `proxied` |

Each successful handshake then logs `conductor.connected` (info, `proxied`). Connection
failures never affect workflows or readiness:

| Event | Level | Cause |
| --- | --- | --- |
| `conductor.rejected` | error | Conductor refused the handshake (`status`): placeholder key, revoked key, expired trial |
| `conductor.connect_failed` | error | Conductor or the proxy unreachable, proxy refusing, proxy URL with a path, connection lost (`reason`, key removed) |

One event per attempt, with `retry_in_seconds`. Attempts back off from 1 s, doubling to at
most one every 300 s, and the backoff resets after a successful connection.

Once connected, the gate logs:

| Event | Level | Meaning |
| --- | --- | --- |
| `conductor.command_refused` | warning | a refused type (`message_type`, `request_id`, and `executor_ids` for `RECOVERY`); Conductor gets an error response. A `RECOVERY` naming only executors already warned about on this connection logs at debug |
| `conductor.message_malformed` | warning | a binary frame or unparseable message (`reason`); dropped, since it has no request ID to answer |

Conductor sends a Console write to every connected executor, so one Console action logs
one `conductor.command_refused` per executor.

```bash
kubectl -n <namespace> logs <pod> -c worker | grep '"conductor\.'
```

## Key and proxy

Set `CURATE_D_CONDUCTOR_KEY` through your secret mechanism and `CURATE_D_CONDUCTOR_ENABLED`
to `true`; the key is an environment variable, so restart the workers after changing either.
Register the application in the DBOS Console under the name DBOS runs under
(`runtime/dbos_app.py`, `application_name`): a mismatch connects the executors but lists no
workflows. A key Conductor does not accept logs `conductor.rejected` with backoff and
affects nothing else. If the CPU worker has no direct route to Conductor, configure an
optional forward proxy with `CURATE_D_CONDUCTOR_PROXY_URL`; it is used by the Conductor
connection only. With the proxy down, the executors behind it drop off the Console and log
`conductor.connect_failed` with backoff, and workflows are unaffected.

## Data sent to Conductor

Workflow inputs and outputs are sent to the Conductor service. Turning the flag off, or
removing the key, stops it.

## Commands

**Conductor is read-only in curate.d.** The gate passes read messages to DBOS and refuses
every write; the Console is for looking, not for acting. A unit test fails the build if a
DBOS upgrade adds a type the table does not classify.

| Class | Commands | Handling |
| --- | --- | --- |
| Read-only | `EXECUTOR_INFO`, `LIST_WORKFLOWS`, `LIST_QUEUED_WORKFLOWS`, `GET_WORKFLOW`, `LIST_STEPS`, `GET_WORKFLOW_EVENTS`, `GET_WORKFLOW_NOTIFICATIONS`, `GET_WORKFLOW_STREAMS`, `GET_WORKFLOW_AGGREGATES`, `GET_STEP_AGGREGATES`, `EXIST_PENDING_WORKFLOWS`, `GET_METRICS`, `ALERT`, `EXPORT_WORKFLOW`, `LIST_SCHEDULES`, `GET_SCHEDULE`, `LIST_APPLICATION_VERSIONS`, `LIST_QUEUES`, `GET_QUEUE` | passed to DBOS unchanged |
| Refused | `CANCEL`, `RESUME`, `PAUSE_SCHEDULE`, `RESUME_SCHEDULE`, `TRIGGER_SCHEDULE`, `BACKFILL_SCHEDULE`, `DELETE`, `FORK_WORKFLOW`, `FORK_FROM_FAILURE`, `REWIND_WORKFLOW`, `SET_LATEST_APPLICATION_VERSION`, `RECOVERY`, `RETENTION` (including its global timeout), `IMPORT_WORKFLOW`, any unknown type | error response `curate.d refuses Conductor command type '<type>': Conductor is read-only in curate.d`, followed by `; <reason>` for retention, import, backfill and recovery, `conductor.command_refused` with type and request ID, never dispatched; nothing is enqueued and no audit entry is written |

**Writes are refused** because Conductor cannot deliver them safely to curate.d's executors
(verified against a live Conductor):

- Conductor sends each Console write to every connected executor in turn, each copy under a
  fresh request ID, even after the first executor answered. One cancel ran once per
  executor; a trigger or a fork would fire once per executor.
- Conductor keeps one record per executor ID and does not refresh its application version
  when the same ID reconnects. curate.d's executor IDs are stable pod names, so after a
  rollout Conductor finds no executor on a workflow's version, and a resume fails with 503.

Every write stays available by hand, through `DBOSClient` or the `dbos workflow` CLI against
the ledger database. Do it there, and record who did what and why. A management layer for
operator commands of our own is an open question (issue #5).

An earlier design ran cancel, resume and the schedule commands as audited command workflows.
The `workflow_command_issued` and `workflow_command_completed` entry types stay valid and
nothing writes them any more. To read entries recorded earlier, until a [reset](reset.md)
wipes them:

```sql
SELECT id, occurred_at, entry_type, details
FROM audit_entries
WHERE entry_type IN ('workflow_command_issued', 'workflow_command_completed')
ORDER BY id;
```

Three refusals have reasons of their own, beyond read-only:

**Retention** is refused so `retention.py` stays the single owner of pruning, and no workflow
is cancelled by a Console age limit. Setting a retention policy in the Console has no effect.

**Workflow import** is refused without decoding its payload, and leaves the workflow tables
unchanged. The payload is a gzipped Python pickle, and DBOS's handler would unpickle it:
that would run code supplied from the Conductor side. Export (`EXPORT_WORKFLOW`) still works. The only thing lost is copying a workflow
between environments from the Console.

**Recovery** is refused. DBOS's handler would set every PENDING workflow the named executors
own back to `ENQUEUED` without checking whether they are alive. curate.d's executors keep
their pod name across restarts, so Conductor naming a pod it thinks is dead, after a restart
or a dropped connection, would hand the pod's running work to a second worker mid-step. Each
worker recovers its own PENDING workflows at startup instead. The refusal is an honest
failure: the Console may show the recovery as failed, because nothing was recovered. DBOS
does not document whether Conductor asks again after a failed `RECOVERY`. If it does, each
executor is warned about once per connection, and the repeats log at debug.

## Cautions

- **Executor versions in the Console go stale after a rollout.** Conductor keeps the
  application version an executor ID first reported, and curate.d's IDs survive restarts.
  Workflow, step and queue listings come from the shared system database and are current;
  only the executor list's versions are not.
- **A removed pod's work waits.** Each worker recovers its own PENDING workflows when it
  restarts, and Conductor's `RECOVERY` is refused, so a pod that never comes back (a
  StatefulSet scaled down after a hard kill) leaves its PENDING workflows until its identity
  returns. Bring the identity back (scale up, let it drain, scale down) as issue #5
  describes. A clean scale-down drains first and leaves nothing behind.
- **The gate rests on private DBOS internals.** Upstream is moving `dbos._conductor` into the
  commercial `dbos-enterprise` package (dbos-transact-py PR #873). A DBOS upgrade that ships
  it fails the tests that pin those internals, and would stop the workers whatever the flag:
  `worker/main.py` imports the gate, which imports `dbos._conductor` at load. Hold that
  upgrade until the gate follows it.
