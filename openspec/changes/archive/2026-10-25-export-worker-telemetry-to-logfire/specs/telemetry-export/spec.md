# Spec Delta

## Purpose

Defines what a curate.d process (the control-plane API and the I/O and CPU workers) exports
to the Logfire observability backend, under which conditions it exports at all, what may
leave the pod, and how a failing export is kept from affecting the process's own work.

## ADDED Requirements

### Requirement: Every process exports telemetry only when a token is present
Each curate.d process (API, I/O worker, CPU worker) SHALL configure export to Logfire at
startup, labelled with `service.name` (one per process kind: `curate-d-api`,
`curate-d-worker-io`, `curate-d-worker-cpu`), `service.version` (the application version)
and, when `CURATE_D_ENVIRONMENT` is set, `deployment.environment.name`. Without a
`LOGFIRE_TOKEN` nothing MUST be sent anywhere and the process MUST behave as it would
without export configured.

#### Scenario: A worker with a token labels its telemetry
- **WHEN** an I/O worker starts with `LOGFIRE_TOKEN` and `CURATE_D_ENVIRONMENT=dev`
- **THEN** every record and span it exports carries `service.name` `curate-d-worker-io`, the application version and `deployment.environment.name` `dev`

#### Scenario: No token means no export
- **WHEN** a worker starts without `LOGFIRE_TOKEN`
- **THEN** no exporter is created, no connection is attempted, stderr logging is unchanged and the worker serves its queues as before

#### Scenario: No environment means no label
- **WHEN** a process starts with a token but without `CURATE_D_ENVIRONMENT`
- **THEN** its telemetry carries no `deployment.environment.name` rather than a guessed one

### Requirement: Structured log events reach Logfire masked
Every structured log event a process emits at its configured level or above SHALL be
exported to Logfire as a log record whose message is the event name and whose attributes
are the event's fields, exactly as they were written to stderr: after credential masking,
with no field unmasked for export. Events from background threads (the staging supervisor's
event loop, the schedule reconciler) MUST be exported like any other. The export MUST NOT
alter the stderr line.

#### Scenario: A staging event from a background thread arrives with its fields
- **WHEN** the staging supervisor's loop logs `staging.writer_lock_held` with `caller`, `wait_seconds`, `held_seconds` and `entry_claims`
- **THEN** one Logfire record named `staging.writer_lock_held` carries those four attributes with the logged values

#### Scenario: A masked field stays masked
- **WHEN** an event is logged with a field named `refresh_token` and a free-text field containing `Bearer <secret>`
- **THEN** the exported record carries a mask in both places (`redaction.py`'s, or Logfire's own scrub marker where its scrubber also matches the key) and the secret appears nowhere in it

#### Scenario: Debug events are not exported
- **WHEN** a process logging at INFO emits a debug event
- **THEN** no record is exported and no stderr line is written, as today

#### Scenario: Stderr is unchanged by export
- **WHEN** the same event is logged with and without export configured
- **THEN** the stderr JSON line is identical, with no export-added field

### Requirement: Workflow and step executions are exported as spans
When export is configured, every DBOS workflow and step execution SHALL be exported as a
span named after the function, the step span a child of its workflow span, carrying the
workflow ID, the application version, the executor ID and, for a queued workflow, the
queue name. A workflow enqueued from inside another workflow MUST join the enqueuing
workflow's trace. The DBOS library's own log lines SHALL be exported beside the spans.

#### Scenario: A workflow and its steps form one trace
- **WHEN** a launched worker runs a workflow with two steps
- **THEN** three spans are exported sharing one trace ID, the two step spans parented to the workflow span, each with the workflow ID and executor ID

#### Scenario: A child workflow continues the trace
- **WHEN** a workflow enqueues another workflow and a worker later dequeues it
- **THEN** the second workflow's span carries the first one's trace ID

#### Scenario: Spans are created without a token but go nowhere
- **WHEN** a worker without a token runs a workflow
- **THEN** the workflow completes with the same result and no span leaves the process

### Requirement: Export failure never changes what a process does
A failure anywhere in the export path (Logfire unreachable, the token rejected, the
exporter's queue full, an exception raised inside the export processor) SHALL be contained
to the exporter: the event or span is dropped, stderr logging stays complete, the workflow
or request completes as it would have, readiness is unaffected and the process keeps
running. A process MUST NOT retry an export synchronously on the thread that logged.

#### Scenario: A raising exporter drops the record and nothing else
- **WHEN** the configured exporter raises on every export and a worker logs an event inside a step
- **THEN** the stderr line is written, the step and its workflow complete normally, and no exception reaches the caller

#### Scenario: An unreachable backend does not block logging
- **WHEN** Logfire is unreachable and a worker logs a thousand events
- **THEN** each log call returns without waiting on the network, and the worker's readiness and queue consumption are unchanged

#### Scenario: A rejected token is reported once and ignored
- **WHEN** `LOGFIRE_TOKEN` is set but Logfire rejects it
- **THEN** the process logs the rejection to stderr, keeps running, and does not fail startup, readiness or any workflow

### Requirement: Logfire must be configured before DBOS is constructed
In a process that both exports telemetry and runs DBOS, the Logfire export MUST be
configured before the DBOS instance is constructed, because DBOS attaches its log handler
to whatever logger provider is installed at construction. A process that constructs DBOS
first SHALL still run correctly but its DBOS log lines are not exported; the startup order
is therefore pinned by a test.

#### Scenario: The worker configures export before DBOS
- **WHEN** the worker entry point starts
- **THEN** Logfire export is configured before the DBOS configuration is built, and the startup-sequence test records that order

#### Scenario: An integration test launches DBOS without configuring export
- **WHEN** an integration test configures and launches DBOS without configuring Logfire
- **THEN** DBOS launches and runs workflows as before, logging at most a warning that no provider is installed
