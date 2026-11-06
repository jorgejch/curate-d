# Tasks

Two work streams. Stream 1 has no dependency and starts at once. Stream 2 imports stream
1's workflow name and queue mapping, so it starts when 1.1 is done. Stream 3 waits on both.

## 1. Worker side

- [x] 1.1 Add `ALERTING_TEST_PREFIX` and `alerting_test_workflow_id(profile, minute)` to `src/curate_d/runtime/workflow_ids.py`; add `src/curate_d/workflows/alerting_test.py` with `ALERTING_TEST_WORKFLOW`, `WorkerAlertingTestError`, `alerting_test_queue(profile)` (I/O `maintenance`, CPU `fingerprint`), a step that always raises and the workflow that runs it; import it in `workflows/registry.py`; add `tests/unit/workflows/test_alerting_test.py` and a case to `tests/unit/runtime/test_workflow_ids.py`; verify with `uv run pytest tests/unit/workflows tests/unit/runtime/test_workflow_ids.py`
- [x] 1.2 Add `tests/integration/test_alerting_test_workflow.py`: a launched worker dequeues the workflow by its registered name; the row ends `ERROR`, the workflow and step spans end with error status and `logfire.level_num` 17 or above, and DBOS logs the `WorkerAlertingTestError` at ERROR; verify with `make test-stack` then `uv run pytest -m integration tests/integration/test_alerting_test_workflow.py`

## 2. Control plane

- [x] 2.1 Add `src/curate_d/api/alerting_test.py` (`WorkerAlertingTest`, `WorkerAlertingTestBackend`, `DbosWorkerAlertingTest` keyed on an injectable clock's UTC minute); add `worker_alerting_test` to `Backends`, `ledger_backends` and `backends.worker_alerting_test`; add `FakeWorkerAlertingTest` to `tests/unit/api/fakes.py`
- [x] 2.2 Mount `POST /__alerting_test_worker_error__/{profile}` (202, `include_in_schema=False`) beside the internal-error route in `create_app`; name it in `alerting_test_routes_force_on_in_prod`'s description and in `ops/README.md`; add `tests/unit/api/test_worker_alerting_test.py` (202 body, per-profile routing, 422 on an unknown profile, 405 on GET, 404 in prod by default, served when forced on, absent from `/openapi.json`, enqueue options and per-minute collapse); verify with `uv run pytest tests/unit/api` and `make lint`

## 3. Whole change

- [x] 3.1 `make check`

## Workflow follow-up

- Sync the delta spec into `openspec/specs/control-plane/spec.md`, then archive the change.
- Run `openspec validate --all --strict` on the archive.
- In dev, after deploy: `POST` the route for `io`, then `cpu`; confirm the worker alert fires
  once per profile and its notification arrives.
