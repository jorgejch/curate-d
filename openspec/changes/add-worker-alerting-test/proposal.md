# Proposal

## Why

The worker error alert ("Workers errors or exceptions" in Logfire) has no way to be tested
end to end. `GET /__alerting_test_internal_error__` fails in the API, so it trips the API
alert and never the worker one, and nothing makes a worker fail on demand. In dev the
workers have exported only info-level records since they started exporting, so the worker
alert has never had anything to match: whether it fires, and whether its notification
arrives, is unknown until a real worker bug happens.

## What Changes

- Add `POST /__alerting_test_worker_error__/{profile}` to the control-plane API, `profile`
  `io` or `cpu`. It enqueues a workflow, `fail_for_alerting_test`, on a queue only that
  profile dequeues (`maintenance` for I/O, `fingerprint` for CPU) and answers
  `202 Accepted` with the profile, the queue and the workflow ID. The API request itself
  succeeds; the failure is the worker's.
- The workflow fails as an unanticipated bug does: its one step raises
  `WorkerAlertingTestError`, with a message naming it a deliberate worker alerting test, and
  nothing catches it. DBOS ends the step and workflow spans with an error status (exported at
  Logfire level `error`), logs the exception at ERROR, and records the workflow as `ERROR`:
  every one of those matches the worker alert's `level >= 17 OR is_exception`.
- The workflow ID is `alerting-test:worker-<profile>:<UTC minute>`, so requests within one
  minute collapse into one failure, and the next minute fails again.
- The route is served where the existing alerting test routes are (`local` and `dev`
  always; `prod` or unset only with `CURATE_D_ALERTING_TEST_ROUTES_FORCE_ON_IN_PROD=true`)
  and is left out of the OpenAPI document.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `control-plane`: adds a requirement for a request that makes a chosen worker profile fail
  deliberately, for testing worker-error alerting.

## Impact

- `src/curate_d/workflows/alerting_test.py` (new), `src/curate_d/workflows/registry.py`:
  the failing workflow, registered on both profiles.
- `src/curate_d/runtime/workflow_ids.py`: its identity.
- `src/curate_d/api/alerting_test.py` (new), `src/curate_d/api/backends.py`,
  `src/curate_d/api/app.py`: the route and the backend that enqueues.
- `src/curate_d/config.py`, `ops/README.md`: the switch's description names the new route.
- Tests: `tests/unit/api/`, `tests/unit/workflows/`, `tests/unit/runtime/`,
  `tests/integration/test_alerting_test_workflow.py`.
- No dependency, schema, migration, queue or OpenAPI contract change. Each test leaves one
  `ERROR` workflow row, listed among the jobs endpoint's failures until the workflow-history
  prune removes it. The API and the workers must run a build with this change: a worker
  without it fails the row as an unknown workflow, which also alerts, but not as this test.
