# Tasks

One work stream: one source file and its test.

## 1. Name the failure in the response

- [x] 1.1 Register, where the alerting test route is served, an `Exception` handler in `create_app` (`src/curate_d/api/app.py`) that answers `AlertingTestError` with `Problem(detail=str(error))` and any other error with the generic `Internal Server Error`; in `tests/unit/api/test_alerting_test.py`, assert the route's body is that `Problem`, that another unhandled error still answers the generic body, and keep the raise and span tests passing; verify with `uv run pytest tests/unit/api --no-cov` and `make lint`
- [x] 1.2 Update the PyCharm scratch request to assert the `Problem` body's `detail`; verify by reading it back

## Workflow follow-up

- Sync the delta spec into `openspec/specs/control-plane/spec.md`, then archive the change.
- Run `openspec validate --all --strict` on the archive.
