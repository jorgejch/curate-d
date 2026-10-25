# Tasks

Five streams (design.md D12). Task 1.1 is the shared dependency bump and runs first, alone.
Then streams A (1.2–1.5), B (2.x) and D (4.x) start at once: they touch disjoint files.
Stream C (3.x) waits on 1.2 and 1.3, whose signatures it calls. Stream E (5.x) waits on
streams A and B. Group 6 waits on everything. Work on one branch cut from `origin/dev`; the
PR targets `dev`. Integration tests need `make test-stack` once before the first run.

## 1. Stream A: dependency and `observability/`

- [x] 1.1 Change `pyproject.toml`'s `"dbos>=3.3,<3.4"` to `"dbos[otel]>=3.3,<3.4"` with a
  comment naming why (DBOS's `enable_otlp` log handler lives in
  `opentelemetry-instrumentation-logging`), run `uv lock` and `uv sync`; verify with
  `.venv/bin/python -c "import opentelemetry.instrumentation.logging.handler"` succeeding
  and `git diff --stat uv.lock` showing only the one added package and the extra marker
- [x] 1.2 Add `to_logfire: bool = False` to `configure_logging` in
  `src/curate_d/observability/logging.py`, appending `logfire.StructlogProcessor(console_log=False)`
  after `redaction_processor` and before the renderer when set, and widen the module
  docstring to say the export reads the same masked dict stderr gets; verify with
  `make lint typecheck` and the new tests in 1.4
- [x] 1.3 Add `instance_id: str | None = None` to `configure_tracing` in
  `src/curate_d/observability/tracing.py`, passed as
  `resource_attributes={"service.instance.id": instance_id}` when set, and widen the
  module docstring from request tracing to process telemetry (events, spans, which
  processes call it); verify with `make lint typecheck` and a test in
  `tests/unit/observability/test_tracing.py` asserting an exported span's resource
  carries the given `service.instance.id` and that none is forced when omitted
- [x] 1.4 Add tests to `tests/unit/observability/test_logging.py` driving
  `configure_logging(to_logfire=True)` under `_traced_to`-style capture (import the
  helper from `test_tracing.py` or move it to a shared module under `tests/unit/observability/`):
  a `staging.writer_lock_held` event logged from a `threading.Thread` arrives as one record
  with `caller`, `wait_seconds`, `held_seconds`, `entry_claims`; a `refresh_token` field
  and a `Bearer <secret>` text field arrive masked and the secret is absent from
  `repr(exporter.exported_spans)`; a debug event under INFO produces no record and no
  stderr line; the stderr JSON line of one event is identical (timestamp aside) with
  `to_logfire` on and off; `configure_logging()` without the flag adds no record; verify
  with `uv run pytest tests/unit/observability --no-cov` green
- [x] 1.5 Add failure-isolation tests to `tests/unit/observability/test_tracing.py`: with
  `local_processor=SimpleSpanProcessor(<exporter whose export raises>)` and with a
  `SpanProcessor` whose `on_end` raises, a structlog event under `to_logfire=True` still
  writes its stderr line, `logger.info` returns normally and no exception reaches the test;
  verify with `uv run pytest tests/unit/observability --no-cov` green and `make test`
  keeping the coverage gate

## 2. Stream B: DBOS configuration and the `CLAUDE.md` record

- [x] 2.1 Set `enable_otlp=True` and `otel_attribute_format="semconv"` in
  `build_dbos_config` (`src/curate_d/runtime/dbos_app.py`) with a comment naming the
  provider contract (spans and the log handler go to whatever OpenTelemetry providers are
  installed when `DBOS()` is constructed, which is why entry points configure Logfire
  first) and extend the module docstring's construction rule accordingly; verify with a
  test in `tests/unit/runtime/test_dbos_app.py` asserting both keys and `make lint typecheck`
- [x] 2.2 Add a bullet to `CLAUDE.md`'s DBOS list: workflow and step spans and DBOS's log
  lines export through `enable_otlp` onto the process's Logfire providers; an entry point
  that exports calls `configure_tracing` before `dbos_app.configure`, DBOS constructed
  first still runs but exports no DBOS log lines, and the `otel` extra is what supplies
  DBOS's handler; verify with `grep -n "enable_otlp" CLAUDE.md` returning one bullet and
  `uv run pytest tests/unit/test_isolation.py tests/unit/test_no_library_internals.py --no-cov`
  green

## 3. Stream C: entry points (after 1.2 and 1.3)

- [x] 3.1 In `src/curate_d/worker/main.py:main()`, resolve `profile = resolve_profile()`,
  `settings = get_settings()` and `executor_id = resolve_executor_id()`, call
  `configure_logging(level=logging.INFO, to_logfire=True)` then
  `configure_tracing(service_name=f"curate-d-worker-{profile.value}",
  service_version=dbos_app.APPLICATION_VERSION, environment=settings.environment,
  instance_id=executor_id)`, then `run(settings, profile)`; update the docstring; verify
  with `make lint typecheck` and `python -m curate_d.runtime.workflow_audit` clean
- [x] 3.2 In `src/curate_d/api/__main__.py:main()`, pass `to_logfire=True` to
  `configure_logging`; verify with `make lint typecheck` and the existing API tests green
  (`uv run pytest tests/unit/api --no-cov`)
- [x] 3.3 Add to `tests/unit/runtime/test_worker_main.py` a test of `main()` that patches
  `configure_tracing` and `dbos_app.configure` (through the existing `calls` fixture's
  recording style) and asserts the call order `logging → tracing → configure`, the
  service name for each profile, the environment from settings and `instance_id` equal to
  the executor id; and a test that a missing executor identity makes `main()` raise
  `ExecutorIdentityError` before `configure_tracing` is called; verify with
  `uv run pytest tests/unit/runtime/test_worker_main.py --no-cov` green

## 4. Stream D: `docs/staging.md` Logfire queries

- [x] 4.1 Under "Lock contention events", after the `jq` block, add a "Reading the same
  events in Logfire" subsection with one SQL query per `jq` query (writer-lock wait and
  hold by caller; reservation phases by outcome; entry-claim holdings; writer-lock duty
  cycle across I/O pods, per clock hour of the holding's end, with the approximation
  stated; reader waits by `registered` with `blocked_1s`; transfer deadlines by
  direction; lost reservations and failed abandonments by `service_instance_id`), each
  filtered by `kind = 'log'`, `span_name`, `service_name`, `deployment_environment` and a
  `start_timestamp` window, with an intro naming where each `jq` field lives
  (`attributes->>'<field>'`, cast where numeric) and that `service_instance_id` is the
  pod; replace the "log shipper's store" sentence at the end of the `jq` block with a
  pointer to the subsection; verify by running each query against the dev project through
  the Logfire MCP (`query_run`) with a 7-day window: every query executes without error
  (empty results are expected before the change deploys)
- [x] 4.2 Add one sentence to "Lock contention alarms" saying the seven-day reading is
  taken with the Logfire queries across every pod, the `jq` pipelines remaining for a pod
  without a token; verify with `grep -n "Reading the same events in Logfire" docs/staging.md`
  returning the heading and one reference from the alarms subsection

## 5. Stream E: span integration test (after streams A and B)

- [ ] 5.1 Add `tests/integration/test_workflow_telemetry.py` (design.md D11): configure
  tracing over a `TestExporter`, `dbos_app.configure`, `DBOS.launch`, a module-level test
  workflow with two steps that enqueues a child workflow on a queue the test registers and
  listens to; assert the parent's three spans share one trace with the step spans parented
  to the workflow span, the child's span carries the same trace id,
  `dbos.operation.workflow_id` and `dbos.executor.id` are set, and the DBOS `dbos` logger
  has a handler other than its console handler; tear down with `DBOS.destroy()` and a
  send-nowhere `logfire.configure`; verify with `make test-stack` then
  `uv run pytest tests/integration/test_workflow_telemetry.py --no-cov -m integration` green
- [ ] 5.2 Confirm an integration test that launches DBOS without configuring Logfire still
  passes and logs DBOS's provider warnings rather than failing; verify with
  `uv run pytest tests/integration/test_recovery_schedule.py --no-cov -m integration`
  green and its captured log showing `OTLP is enabled but` warnings only

## 6. Integration check

- [ ] 6.1 Run the whole gate; verify with `make check` green (lint, typecheck, unit suite
  with the coverage floor) and
  `openspec validate export-worker-telemetry-to-logfire --strict` passing
- [ ] 6.2 Open the pull request against `dev` titled
  `feat(observability): export worker events and workflow spans to Logfire`, body naming
  #72 and #4, the owner decision on exported fields, the `otel` extra and the dev
  acceptance steps from design.md's Migration Plan; verify every required check is green
  and `Integration tests` ran (the diff touches `src/**`)

## Workflow follow-up

- After merge and the dev reconcile: query Logfire for a `staging.writer_lock_held` record
  from `curate-d-worker-io` and one workflow span per profile with
  `deployment_environment = 'dev'`; comment the result on #72 and close it.
- Sync the delta into `openspec/specs/telemetry-export/spec.md` and archive the change.
- Once seven days have accrued, take the first #4 reading with the new SQL and comment it
  on #4.
