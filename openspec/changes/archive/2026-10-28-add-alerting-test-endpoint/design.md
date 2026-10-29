# Design

## Context

See proposal.md for motivation. `create_app` (`src/curate_d/api/app.py`) resolves `Settings`
and mounts the health routes inline; every backend error type maps to 404, 422 or 503 through
`errors.install`, and anything else propagates to Starlette's `ServerErrorMiddleware`, which
answers a plain-text `Internal Server Error`. `trace_requests` wraps the app with Logfire's
FastAPI instrumentation, which records an escaping exception on the request span.
`Settings.environment` (`DeploymentEnvironment | None`) is `local` in `compose.yaml` and set per
deployment; unset deliberately means "not known to be local".
`tests/unit/api/test_contract.py` requires every 2xx response in the served schema to declare
a typed body.

## Goals / Non-Goals

**Goals:**
- A 500 produced by the same path an unanticipated bug takes, so the test covers the alert
  rule, the span's error status and the server log together.
- Always on in local and dev; off in prod (or with no environment named) unless an
  operator forces it on.

**Non-Goals:**
- Other failure kinds (timeouts, 503, a crashed process).
- Setting the variable in the deployment manifests (another repository).

## Decisions

- **Raise, don't return a 500.** The handler raises `AlertingTestError` (a `RuntimeError`
  subclass declared in `app.py`) and nothing catches it. Returning
  `JSONResponse(status_code=500)` would leave the span without an exception and the log
  without a traceback, so an alert keyed on either would not fire. A dedicated type makes the
  event unambiguous when searching Logfire.
- **Generic body.** `ServerErrorMiddleware` already answers without the message outside debug
  mode; a handler returning a `Problem` with it would change how every unhandled error answers.
- **`include_in_schema=False`.** A test hook is not part of the API contract, and a route
  with no declared 2xx body would fail `test_contract.py`.
- **One-way switch: `alerting_test_routes_force_on_in_prod: bool = False`**, resolved by a
  `Settings.alerting_test_routes_served` property: true in `local` and `dev`, else the
  switch. The name says what it does where it counts; it cannot turn the routes off in local
  or dev, which have no reason to. Alternative rejected: a tri-state "enabled" setting that
  overrides every environment, which lets dev be switched off and makes the prod opt-in look
  like any other value.
- **Unset environment is treated as prod.** `environment`'s own description refuses to treat
  a missing label as local; the routes follow it, so a deployment that forgets the label
  fails closed, and the prod switch applies to it.
- **Not mounted rather than mounted-and-404.** When off, `create_app` skips the route, so the
  path is indistinguishable from any unknown one and the handler has no runtime branch.
- **Mounted inline in `create_app`, beside the health routes.** It belongs to no backend area.

## Risks / Trade-offs

- [Prod enabled and left on lets any authenticated caller fire an alert] → The variable is
  explicit in the prod manifest, reviewed there; the alert names the test exception.
- [Uvicorn logs a full traceback per request] → Accepted: that log line is under test.
