# Design

## Context

See proposal.md for motivation. `GET /__alerting_test_internal_error__` (`api/app.py`,
archived change `2026-10-28-add-alerting-test-endpoint`) is mounted when
`Settings.alerting_test_routes_served`. The control plane already enqueues worker workflows
through `DBOSClient` by registered name (`api/reclassification.py`, `api/dry_runs.py`). Each
worker profile dequeues a fixed set of queues (`runtime/queues.py`, `runtime/profiles.py`),
and every workflow module is imported, so registered, by both profiles through
`workflows/registry.py`. DBOS 3.3 ends a failed step or workflow span with
`Status(ERROR, str(exception))`, and Logfire's span processor turns an error status into
`logfire.level_num` 17; a dequeued workflow that raises is also logged by DBOS at ERROR
through the handler it attaches to Logfire's logger provider. The worker alert queries
`service_name IN ('curate-d-worker-io', 'curate-d-worker-cpu') AND (level >= 17 OR
is_exception = true)`.

## Goals / Non-Goals

**Goals:**
- A worker failure produced by the same path an unanticipated bug in a workflow takes, in a
  profile the operator picks, so the test covers the alert rule, the exported error spans,
  the DBOS error log and the notification together.
- Triggered the way the API test is: one request to the API, in dev.

**Non-Goals:**
- Other worker failure kinds (a crashed process, a lost lease, a step retried to exhaustion).
- Changing the Logfire alert. Its query matches what this produces; see Risks for an
  optional improvement to what its notification shows.

## Decisions

- **Enqueue from the API, fail in the worker.** The worker alert watches the workers'
  service names, so the failure must happen in a worker process. Enqueuing through the
  API's existing `DBOSClient` keeps the trigger one request, as for the API alert.
  Alternative rejected: an operator command run with `kubectl exec` in a worker pod; it
  would need cluster access to test an alert, and a process started that way exports under
  whatever service name it configures, not necessarily the worker's.
- **`POST`, answering 202.** The request writes a workflow row, so a read must not do it; a
  link preview or a crawler cannot fire the alert. The body names the workflow ID to find in
  Logfire.
- **Raise from a step, uncaught.** A step raising is how a real workflow bug surfaces, and
  it gives two error spans (step and workflow) plus the DBOS ERROR log. The step is not
  retried: DBOS retries a step only when `retries_allowed` is set.
- **Existing queues, one per profile.** I/O on `maintenance`, CPU on `fingerprint`: each is
  dequeued by one profile only, so the failure lands where it was asked. A dedicated queue
  would change the topology and the throughput endpoint's stage list for a test. The cost:
  a test waits behind work already running on that queue (concurrency 1 on maintenance, 2
  on fingerprint).
- **Deterministic ID per UTC minute.** `runtime/workflow_ids.py` keeps identifiers
  deterministic so duplicate enqueues collapse; a per-minute key keeps that property, bounds
  repeated clicks to one failure (and one alert) a minute per profile, and lets a later
  minute test again. Alternatives rejected: a random ID (departs from the rule, lets a loop
  of requests flood the alert channel); a fixed ID (only the first test ever fails).
- **Same switch as the other alerting test routes.** No new setting; the switch's
  description and `ops/README.md` name the new route.
- **Mounted beside the internal-error route**, under the same `if`, through a
  `worker_alerting_test` backend so the isolated tests wire a fake.

## Risks / Trade-offs

- [Each test leaves an `ERROR` workflow row] → It is listed among `GET /jobs` failures, as
  `maintenance: fail_for_alerting_test` or `fingerprint: fail_for_alerting_test`, named as a
  test by its ID and error, until the workflow-history prune removes it. The recovery sweep
  only acts on `PENDING` and `ENQUEUED` rows, so it never touches it.
- [Prod forced on lets any caller reaching the API fire the worker alert, once a minute per
  profile] → Same exposure as the internal-error route; the switch is explicit in the prod
  manifest.
- [Mixed builds] → A worker on a build without the workflow fails the row as unregistered,
  which still alerts, but not as this test; deploy the API and workers together.
- [The alert notification shows no message] → Optional, outside this repository: the worker
  alert selects no `message` or level column, unlike the API alert, so its notification
  names the span but not the error. Adding `message` and `level_name(level)` to its SELECT
  and GROUP BY would show `deliberate worker error ...` for this test and the real error
  otherwise.
