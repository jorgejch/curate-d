# Design

## Context

See proposal.md for motivation. Current state on `dev` (`6728069`), verified 2026-10-25;
the branch was rebased onto `dev` at `c2ecbf8` the same day, and nothing in between touches
the files this change edits:

- `observability/tracing.py:configure_tracing` wraps `logfire.configure(send_to_logfire=
  "if-token-present", console=False, scrubbing=extra patterns, additional_span_processors=
  [QueryStringStripper, local_processor?])`. Only `api/__main__.py` calls it.
  `tests/unit/observability/test_tracing.py` drives it with `local_processor=
  SimpleSpanProcessor(TestExporter())`, which disables the Logfire exporter.
- `observability/logging.py:configure_logging` installs the structlog chain
  `merge_contextvars → add_log_level → TimeStamper → StackInfoRenderer → format_exc_info →
  redaction_processor → renderer`, writing to the stderr current at write time.
  Debug events are dropped by `make_filtering_bound_logger(level)` before any processor.
- `worker/main.py:main()` calls `configure_logging` then `run()`; `run()` constructs DBOS
  through `dbos_app.configure` after the lease is held and before `DBOS.launch()`.
- `runtime/dbos_app.py:build_dbos_config` sets name, version, system URL, schema and
  executor id. No telemetry key. Integration tests build DBOS through it too.
- Installed: `dbos` 3.3.0, `logfire` 5.3.0, `opentelemetry-sdk` 1.45.1. Read from the
  installed packages (not depended on as private API; documented behaviour, re-checked on
  upgrade like the schema dependencies in `CLAUDE.md`):
  - `logfire.configure()` installs its own tracer and logger providers as the OpenTelemetry
    globals once per process. They are Logfire's proxy classes, not OpenTelemetry's
    `ProxyTracerProvider`/`ProxyLoggerProvider`.
  - DBOS with `enable_otlp=True` and no `otlp_*_endpoints` creates no provider of its own:
    spans come from `trace.get_tracer("dbos-tracer")` on the global provider, resolved at
    each span start; and at `DBOS()` construction it attaches
    `opentelemetry.instrumentation.logging.handler.LoggingHandler` to its `dbos` logger
    whenever the global logger provider is not OpenTelemetry's proxy. That handler ships in
    `opentelemetry-instrumentation-logging`, which only `dbos[otel]` requires. Probe:
    `logfire.configure(); DBOS(config=…enable_otlp=True)` raises `ModuleNotFoundError`
    in today's environment; `uv pip install --dry-run "dbos[otel]"` adds exactly
    `opentelemetry-instrumentation-logging==0.66b1`.
  - With no Logfire configured, DBOS logs one warning each for tracer and logger provider
    and creates no-op spans. Nothing fails.
  - DBOS 3.3 parents a queued workflow's span to the enqueuer's trace through a carrier
    stored on the workflow row, so an ingest→classification handoff stays one trace.
  - `logfire.StructlogProcessor(console_log=False)` emits each event as a Logfire log with
    `event` as message template and the remaining fields (minus `level`, `event`,
    `timestamp`, `exc_info` and stdlib-reserved keys) as attributes, and returns the event
    dict unchanged. `Logfire.log` runs under `handle_internal_errors`, so a failure inside
    it is logged, never raised. An unconfigured Logfire warns once
    (`LogfireNotConfiguredWarning`) and drops the record.
  - `logfire.configure(resource_attributes=…)` is a public top-level argument; without
    `service.instance.id` Logfire generates a random one per process.
- Logfire's query engine promotes `service.name`, `service.version`,
  `deployment.environment.name` and `service.instance.id` to flat columns on `records`;
  event fields live under `attributes` (`attributes->>'caller'`,
  `(attributes->>'wait_seconds')::double`); percentiles are `approx_percentile_cont`.
- Both worker StatefulSets already set `LOGFIRE_TOKEN`, `CURATE_D_WORKER_PROFILE` and
  `POD_NAME`; the dev overlay sets `CURATE_D_ENVIRONMENT=dev` for every container.

## Goals / Non-Goals

**Goals:**

- One telemetry configuration path for every process; the worker adds nothing the API
  does not already do except the DBOS spans.
- The stderr line is byte-for-byte what it is today; the Logfire record is built from the
  same post-redaction dict.
- Startup order (Logfire before DBOS) is pinned by a test, since getting it wrong fails
  silently.
- Every spec scenario maps to a unit test except span production, which needs a launched
  DBOS and is one integration test.

**Non-Goals:**

- Metrics, Logfire alerts or dashboards, sampling, and a per-field export allowlist
  (owner decision: fields export as logged).
- Changing DBOS's console logging format or routing DBOS's logger through structlog.
- Deployment manifest changes (nothing is needed; checks on them live in flux-ovh).
- Re-tracing the rclone sidecar.

## Decisions

**D1. `configure_logging` gains `to_logfire: bool = False`; entry points pass `True`.**
When set, `logfire.StructlogProcessor(console_log=False)` is appended after
`redaction_processor` and before the renderer. Alternatives: always install it (every CLI,
migration and test process that never configures Logfire then emits
`LogfireNotConfiguredWarning` once, and the warning is a symptom of a wrong setup, not
noise to silence); install it from `configure_tracing` (the structlog chain is built in one
place, and splitting it across two functions hides the redaction-before-export ordering
the spec requires). The flag keeps export an explicit choice at each entry point and leaves
`make_filtering_bound_logger` as the volume control: nothing below the configured level
reaches the processor.

**D2. One `configure_tracing` for every process, with `instance_id`.** The function keeps
its name and behaviour and gains `instance_id: str | None = None`, passed as
`resource_attributes={"service.instance.id": instance_id}` when set. The worker passes its
executor id (the pod name under Kubernetes, `CURATE_D_EXECUTOR_ID` elsewhere), so every
record from a pod is attributable to it in SQL (`service_instance_id`) and matches DBOS's
`dbos.executor.id` span attribute. The API passes nothing and keeps Logfire's random id.
Alternative: a separate `configure_worker_telemetry` (two configurations to keep aligned).
`main()` resolves the executor id itself to pass it; `run()` keeps resolving its own, so
its signature and its identity tests are untouched, and a missing identity still raises
`ExecutorIdentityError` before anything connects.

**D3. Worker entry point order: `configure_logging(to_logfire=True)`,
`configure_tracing(...)`, then `run()`.** Service name `curate-d-worker-{profile}` from
`resolve_profile()`, version `dbos_app.APPLICATION_VERSION`, environment
`get_settings().environment`. Logfire is configured before `run()` constructs DBOS, which is
what lets DBOS find a logger provider at construction (Context). The API's `main()` adds
`to_logfire=True` and is otherwise unchanged. `worker.starting` is the first event either
way, logged inside `run()` after both are configured.

**D4. `build_dbos_config` always sets `enable_otlp=True` and
`otel_attribute_format="semconv"`.** Not flagged: with Logfire unconfigured, DBOS creates
no-op spans and logs two warnings at construction, which is the documented behaviour the
spec's last scenario pins, and one code path is easier to reason about than a parameter
that only entry points set. `semconv` gives `dbos.operation.workflow_id`,
`dbos.executor.id`, `dbos.queue.name` and friends instead of camelCase legacy names; there
are no existing dashboards to keep. `otlp_log_level` stays at its default (DBOS's
`log_level`, INFO): DBOS's INFO lines are startup, recovery and queue messages, a handful
per process lifetime. This is a DBOS configuration setting the `dbos-python` skill lists
(`references/lifecycle-config.md`), not a departure; the ordering it imposes on our
entry points is recorded in `CLAUDE.md`'s DBOS list.

**D5. Dependency through the extra: `dbos[otel]>=3.3,<3.4`.** The extra is how DBOS
declares what `enable_otlp` needs, and its floors move with DBOS. Alternative: add
`opentelemetry-instrumentation-logging` as a direct dependency (a second place to keep in
step with the OpenTelemetry SDK version Logfire pins).

**D6. DBOS's own log lines go through DBOS's handler, not ours.** DBOS attaches an
OpenTelemetry `LoggingHandler` to the `dbos` logger when `enable_otlp` is on; that handler
writes to Logfire's logger provider, which scrubs with the configured patterns. Attaching
`logfire.LogfireLoggingHandler` as well would export every DBOS line twice. DBOS's console
handler keeps writing its own format to stderr as today. DBOS lines never carry our
credentials (they are DBOS's status messages and workflow ids), so they bypass
`redaction.py`; Logfire's scrubber still applies.

**D7. Failure isolation is Logfire's and OpenTelemetry's, verified by tests, not
re-implemented.** `send_to_logfire="if-token-present"` never validates a token
synchronously (Logfire checks it on a background thread and warns); the exporter runs on a
`BatchSpanProcessor` thread with a bounded queue that drops when full; `SimpleSpanProcessor`
and the batch processor catch exporter exceptions and log them; `Logfire.log` swallows and
logs internal errors. The unit tests pin the two paths we can drive without a network: an
exporter whose `export` raises, and a span processor whose `on_end` raises, each behind a
structlog event, asserting the stderr line and the caller's return. The redaction-order
proof is a free-text field carrying `Bearer <secret>`: Logfire has no bearer pattern, so
only `redaction.py` running first can produce the masked value the test demands.

**D8. Volume: no sampling, no new settings.** Inventory on `dev`: 16 info, 15 warning,
14 error and 1 critical distinct call sites in `src/curate_d`, none per listed object; the
busiest are one per staging holding, reservation and reader hold. Spans: one per workflow
and one per step. Logfire's plan limits are far off; if that changes, `sampling=` on
`configure_tracing` is the knob, and it is a one-line change.

**D9. Export fields as logged.** Owner decision (2026-10-25): paths, file names, staging
keys and account identifiers export as stderr carries them; credentials stay masked by
`redaction.py` before the processor and by Logfire's scrubber after. Alternatives rejected:
hashing path-like fields for export (defeats finding a file's trace by name) and dropping
them (same, and a Logfire-only shape that diverges from stderr).

**D10. SQL in `docs/staging.md`: every `jq` query gets a Logfire counterpart.** Records
are selected by `kind = 'log'`, `span_name = '<event>'`, `service_name`,
`deployment_environment` and a `start_timestamp` window; fields by `attributes->>'…'` with
casts; percentiles by `approx_percentile_cont`; per-pod groupings by
`service_instance_id`. The duty cycle buckets each holding into the clock hour it ended
(`date_trunc('hour', start_timestamp)`), the documented approximation of the `jq` split
across hours: holdings are seconds long, hours 3600. The `jq` versions stay for a pod
without a token. The "log shipper's store" sentence becomes a pointer to the SQL. Each
query is run against the dev project through the Logfire MCP during apply; before the
change deploys it returns no rows, which verifies syntax and column names, not values.

**D11. The span integration test launches DBOS the way `test_recovery_schedule.py` does.**
`configure_tracing(service_name="test", service_version="0", local_processor=
SimpleSpanProcessor(TestExporter()))`, then `dbos_app.configure`, `DBOS.launch`, a test
workflow with two steps that enqueues a child on a queue the test registers and listens to,
then assertions on `TestExporter.exported_spans`: three spans on one trace for the parent,
the child on the same trace, `dbos.operation.workflow_id` and `dbos.executor.id` set.
`logfire.configure(send_to_logfire=False, console=False, metrics=False)` in teardown, as
`test_tracing.py` does.

**D12. Work streams.** A: dependency and `observability/` (logging flag, tracing
`instance_id`, their unit tests). B: `runtime/dbos_app.py` and its test, plus the
`CLAUDE.md` bullet. C: entry points (`worker/main.py`, `api/__main__.py`) and
`test_worker_main.py`. D: `docs/staging.md`. E: the integration test. A's first task
(the dependency) runs before everything; B and D start at once with the rest of A; C
waits on A's signature tasks; E waits on A and B.

## Risks / Trade-offs

- [DBOS construction fails when Logfire is configured and the extra is missing] → the
  dependency task comes first and `tests/unit/runtime` asserts
  `opentelemetry.instrumentation.logging` imports; the integration test exercises the real
  path.
- [A future DBOS release changes how `enable_otlp` resolves providers or imports its
  handler] → the Context section names the behaviour relied on; the dependency bump that
  refreshes the skill re-runs the span integration test, which fails loudly.
- [Logfire's processor sees an event dict after `format_exc_info` turned `exc_info` into
  an `exception` string] → that is also what stderr gets; the record carries the traceback
  text as an attribute and `exc_info=False`. Accepted: identical to stderr, as the spec
  requires.
- [A pod with a bad token logs Logfire's rejection warning once and otherwise runs]
  → that is the intended isolation; the dev acceptance check catches a wrong secret.
- [`service.instance.id` from the executor id means a pod restart keeps its id] → wanted:
  the pod name is the identity `kubectl logs` showed and DBOS recovers under.
- [Two warnings per `DBOS()` in every integration test] → cosmetic; the alternative (a
  flag) costs a second configuration path. Revisit if the suite's output becomes hard to
  read.
- [Volume grows with a future per-object info event] → D8 names the knob; the event
  inventory is in the proposal for the next reviewer.
- [Logfire re-raises its internal errors whenever `PYTEST_CURRENT_TEST` is set, so under
  pytest a raising span processor escapes `Logfire.log` the way it never does in
  production] → the processor-failure test clears that variable inside its body
  (`tests/unit/observability/test_tracing.py`, `_log_exported_event`). Undocumented Logfire
  behaviour: re-check it on every Logfire upgrade.
- [Logfire's scrubber runs after ours and replaces `[REDACTED]` on a key it also matches
  (`refresh_token`) with its own marker] → harmless, the secret is gone either way; the spec
  scenario accepts either mask, and the order proof uses a bearer-token text field Logfire
  does not scrub (D7).

## Migration Plan

1. Merge to `dev`; the image build picks up the `otel` extra through `uv.lock`.
2. Flux reconciles the dev StatefulSets; nothing in flux-ovh changes.
3. Acceptance in dev: a `staging.writer_lock_held` record from `curate-d-worker-io` and
   one workflow span per profile, each with `deployment_environment = 'dev'`, through the
   Logfire MCP or UI. Comment the reading on #72 and close it; take the first #4 reading
   with the new SQL once seven days have accrued.
4. Rollback: revert the merge. Without the processor and `enable_otlp`, workers behave as
   before; no data or schema is involved.

## Open Questions

None that change the specs, the approach or the tasks. Whether the API should also pass a
`service.instance.id` (its `POD_NAME` is available) can be decided when a second API
replica exists.
