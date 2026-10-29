# Tasks

One work stream, run in order: 1.2 reads the setting 1.1 adds, and the change is too small to
split across agents.

## 1. Alerting test route

- [x] 1.1 Add `alerting_test_routes_force_on_in_prod: bool = False` and the `alerting_test_routes_served` property to `Settings` (`src/curate_d/config.py`); add tests to `tests/unit/test_config.py` for every environment (local, dev, prod, unset) under each switch value (unset, true, false), and a non-boolean value failing to load; document the variable in `ops/README.md`'s deployment configuration; verify with `uv run pytest tests/unit/test_config.py`
- [x] 1.2 Add `AlertingTestError` and `GET /__alerting_test_internal_error__` (`include_in_schema=False`) to `create_app` in `src/curate_d/api/app.py`, mounted only when `alerting_test_routes_served`, raising the error with a message naming it a deliberate alerting test; add `tests/unit/api/test_alerting_test.py` showing the route answers 500 with the generic body, raises `AlertingTestError` with that message, answers 404 when not served, and is absent from `/openapi.json`; verify with `uv run pytest tests/unit/api` and `make lint`
- [x] 1.3 Add requests for the route to the PyCharm scratch file `generated-requests.http` asserting `response.status === 500`, beside the existing `__alerting_test_not_found__` requests; verify by reading the entries back

## Workflow follow-up

- Sync the delta spec into `openspec/specs/control-plane/spec.md`, then archive the change.
- Run `openspec validate --all --strict` on the archive.
- To enable the route in prod, set `CURATE_D_ALERTING_TEST_ROUTES_FORCE_ON_IN_PROD=true` in the prod API
  deployment (deployment repository).
