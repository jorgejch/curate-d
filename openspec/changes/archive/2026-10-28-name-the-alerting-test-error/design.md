# Design

## Context

See proposal.md. In FastAPI 0.142 / Starlette 1.7, a handler registered for `Exception` (or
500) runs inside `ServerErrorMiddleware`, which sends the handler's response and then re-raises
the exception, so it still reaches the request span and uvicorn's log. A handler registered for
`AlertingTestError` itself runs in `ExceptionMiddleware`, which swallows the exception: the
span would lose its exception event and the log its traceback.

## Goals / Non-Goals

**Goals:**
- The message in the body, with the span and log unchanged.

**Non-Goals:**
- Changing how any other unhandled error answers.

## Decisions

- **One `Exception` handler, dispatching on type.** It answers an `AlertingTestError` with
  `Problem(detail=str(error))` and anything else with `PlainTextResponse("Internal Server
  Error", 500)`, the body Starlette's own default sends. Alternative rejected: a handler for
  `AlertingTestError`, which catches it before telemetry sees it.
- **Registered only where the route is served.** Where the route is off the app keeps
  Starlette's default handler untouched.
- **`Problem` JSON, not plain text.** Every other API error answers `{"detail": ...}`
  (`errors.py`); the test route reads the same way.

## Risks / Trade-offs

- [The fallback reimplements Starlette's default 500 body] → It is one public response, pinned
  by a test that sends another unhandled error through the same app.
- [A later Starlette that stops re-raising after the handler] → `test_the_request_span_records_the_exception`
  and `test_the_route_raises_an_unhandled_alerting_test_error` fail on it.
