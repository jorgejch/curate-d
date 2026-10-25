# Proposal

## Why

Only the control-plane API exports to Logfire (#72). `worker/main.py:main()` configures
structlog alone, so every I/O and CPU worker event (`staging.*`, `worker.*`,
`recovery_sweep.*`, ingest, classification, placement) exists only in pod stderr, readable
one pod at a time with `kubectl logs` and gone with the pod. #4's regression check of the
`staging.*` events against `docs/staging.md`'s "Lock contention alarms" needs at least seven
days across every I/O pod, which stderr cannot give. Both worker containers already receive
`LOGFIRE_TOKEN` in the deployment (flux-ovh `apps/curate-d/base/worker-{io,cpu}/statefulset.yaml`);
nothing reads it.

## What Changes

- Workers configure Logfire the way the API does, through the shared
  `observability/tracing.py`: only when a token is present, with `service.name`
  (`curate-d-worker-io` / `curate-d-worker-cpu`), `service.version` and
  `deployment.environment.name` from `Settings.environment`. No token means nothing is
  sent and nothing else changes.
- Every structlog event an API or worker process emits is also sent to Logfire as a log
  record, with its fields as attributes, after `redaction.py` has masked it. The processor
  sits between the redaction processor and the renderer, so stderr output is unchanged and
  nothing unmasked leaves the pod. Logfire's own scrubber keeps the API's extra patterns.
  Fields are exported as they are logged: provider paths, file names, staging keys and
  account identifiers included (owner decision, 2026-10-25). Credentials stay masked.
- DBOS's OpenTelemetry spans for workflows and steps are enabled (`enable_otlp`), so a
  file's path through ingest, classification and placement reads as one trace, with
  DBOS's own log lines exported beside them. Settled against the `dbos-python` skill: the
  skill lists `enable_otlp` as a supported option and the DBOS Logfire integration
  documents exactly this arrangement (Logfire SDK configured first, no OTLP endpoints).
  The ordering constraint it creates is recorded in `CLAUDE.md`.
- Every workflow enqueue in `src/curate_d` runs under DBOS's `PropagateOtelContext`, which
  is what makes a dequeued child join the enqueuer's trace (DBOS does not do it by
  itself), and `make lint`'s workflow audit fails on an enqueue outside it.
- The `dbos` dependency gains its `otel` extra. DBOS attaches an OpenTelemetry logging
  handler to its logger when `enable_otlp` is on and a logger provider is installed, and
  that handler lives in `opentelemetry-instrumentation-logging`, which only the extra
  brings (verified: without it, `DBOS()` raises `ModuleNotFoundError` after
  `logfire.configure()`).
- Export never changes what a worker does: no token sends nothing; an unreachable or
  rejecting Logfire drops spans and records in the background while stderr stays
  complete; an exporter failure fails neither readiness nor a workflow.
- No sampling and no new settings. Workers log at INFO and the event inventory is small
  (46 distinct info-and-above call sites, none per listed object), so volume is a few
  records and spans per file. Debug events are already dropped by structlog's level filter
  before any processor runs.
- `docs/staging.md` gains a Logfire SQL version of every query in "Lock contention events"
  (seven), beside the `jq` versions, so the alarms can be read across pods and past a
  restart; the "log shipper's store" sentence points at Logfire instead.
- Not exported: the rclone sidecar, as today.

## Capabilities

### New Capabilities

- `telemetry-export`: what a curate.d process exports to Logfire, under which conditions
  (token present, environment label, service identity), what reaches the exporter
  (masked log events, workflow and step spans) and how export failure is isolated from
  the process's work. The API's existing request tracing is covered by its first
  requirement; nothing about it changes.

### Modified Capabilities

None. The `content-staging` lock-contention events keep their fields; the SQL queries
are documentation, not behaviour.

## Impact

- Code: `src/curate_d/observability/logging.py` (Logfire structlog processor),
  `observability/tracing.py` (no behaviour change; docstring widens from "request
  tracing" to process telemetry), `worker/main.py:main()` (configure tracing before
  `run`), `runtime/dbos_app.py:build_dbos_config` (`enable_otlp`, semconv attribute
  names), `api/__main__.py` (`to_logfire=True` on its logging call), the 14
  `DBOS.enqueue_workflow_async` sites under `workflows/` (wrapped in
  `PropagateOtelContext`), `runtime/workflow_audit.py` (the enqueue rule).
- Dependencies: `pyproject.toml` `dbos[otel]>=3.3,<3.4` (adds
  `opentelemetry-instrumentation-logging`), `uv.lock`.
- Tests: `tests/unit/observability/test_tracing.py` (instance id, failing exporter and
  processor), `tests/unit/observability/test_logging.py` (masked fields arrive masked,
  background-thread events, debug filtering, unchanged stderr, no export without the flag)
  over a shared capture helper `tests/unit/observability/capture.py`,
  `tests/unit/runtime/test_worker_main.py` (main configures tracing before DBOS),
  `tests/unit/runtime/test_dbos_app.py` (config carries `enable_otlp`), one integration
  test that a launched workflow produces a span under a local processor
  (`tests/integration/test_workflow_telemetry.py`).
- Docs: `docs/staging.md` "Lock contention events" (SQL queries), `CLAUDE.md` DBOS list
  (configure Logfire before constructing DBOS), `README.md` tech stack line for Logfire if
  it names the API alone.
- Deployment: none. `LOGFIRE_TOKEN` and `CURATE_D_ENVIRONMENT` are already set on both
  worker containers (flux-ovh `base/worker-*/statefulset.yaml`, `overlays/dev/environment.yaml`).
- Volume: one Logfire log record per structlog event at INFO and above, DBOS's own log
  lines, and one span per workflow and per step, from every worker pod.
- Issue: #72 closes on merge after the dev acceptance check (a `staging.writer_lock_held`
  record from an I/O worker and a workflow span from each profile, labelled `dev`).
