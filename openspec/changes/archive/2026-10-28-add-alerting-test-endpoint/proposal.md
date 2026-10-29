# Proposal

## Why

Server-error alerting on the control plane cannot be tested end to end: no route fails on
demand, so the only test request available today (`/__alerting_test_not_found__`) answers 404,
which exercises client-error handling and never trips a server-error alert. An operator needs
one request that reliably produces a 500 through the same path a real bug takes.

## What Changes

- Add `GET /__alerting_test_internal_error__` to the control-plane API. It raises an unhandled
  exception carrying a message that names it as a deliberate alerting test, so the API answers
  `500 Internal Server Error` and the request span and server log record the exception.
- The route has no side effects, reads nothing, and is left out of the OpenAPI document.
- The route is always served when `CURATE_D_ENVIRONMENT` is `local` or `dev`. When it is
  `prod` or unset, the route is off unless the new setting
  `CURATE_D_ALERTING_TEST_ROUTES_FORCE_ON_IN_PROD` is `true`; in `local` and `dev` that
  setting has no effect. Where the route is off the path answers 404 like any unknown path.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `control-plane`: adds a requirement for a request that deliberately fails with a server
  error, for testing alerting, and one for where it is served.

## Impact

- `src/curate_d/config.py`: one setting.
- `src/curate_d/api/app.py`: one route, mounted conditionally, and its exception type.
- `tests/unit/api/`, `tests/unit/test_config.py`: unit tests.
- `ops/README.md`: the setting in the deployment configuration it documents.
- No dependency, schema, migration or OpenAPI contract change. Enabling it in prod is a
  deployment-repository change (set the variable), outside this repository.
