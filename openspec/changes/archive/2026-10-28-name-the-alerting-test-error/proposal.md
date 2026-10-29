# Proposal

## Why

The alerting test route answers the generic `Internal Server Error` body, so whoever sends the
request (the PyCharm scratch request, a curl from an incident drill) cannot tell from the
response that it reached the deliberate failure rather than some other bug. Review on PR #120
asked for the exception's message in the body instead.

## What Changes

- `GET /__alerting_test_internal_error__` answers 500 with a `Problem` body whose `detail` is
  the `AlertingTestError` message, the shape every other API error answers with.
- The exception still escapes to the request span and the server log: the alert under test
  watches those, not the body.
- Every other unhandled error keeps the generic `Internal Server Error` body, so no other
  exception's message (a DSN, a path) reaches a client.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `control-plane`: "Deliberate server error for alerting tests" answers with the exception's
  message instead of the generic body.

## Impact

- `src/curate_d/api/app.py`, `tests/unit/api/test_alerting_test.py`.
- The PyCharm scratch request asserts the new body.
