# Proposal

## Why

SonarQube Cloud's `dev` analysis (2026-11-04 04:23 UTC, commit `1b9ea4e`) lists 65 open
issues: 63 open and 2 confirmed, with no security hotspot to review. Most are maintainability
debt: 13 functions over the cognitive-complexity limit, 9 duplicated literals, 21 redundant
FastAPI `response_model` arguments. Two are real findings: the image build can run third-party
build scripts (`docker:S8541`, already confirmed), and an always-true condition is flagged in
transfer admission. The count grows with each merge. A clean baseline makes each new issue
visible in review.

## What Changes

- Resolve every issue that is OPEN or CONFIRMED on the SonarQube `dev` branch when the apply
  starts. The 65 issues listed in design.md are the snapshot from planning, not the scope
  limit: the apply reads the list again and adds or drops issues to match it.
- Fix each issue in code when the fix keeps the observed behavior. Refactors keep function
  contracts, raised exceptions, log event names, DBOS step order and workflow IDs unchanged.
- Where no code fix keeps a deliberate guarantee, mark the issue Accepted (or False positive
  where the analyzer is wrong) in SonarQube Cloud. Write the reason in the issue comment and in
  design.md. The design names the candidates. The apply decides each one from evidence.
- Install the image's third-party dependencies from wheels only: add `--no-build` to both
  `uv sync` calls in `docker/Dockerfile`, as `checks.yml` and `claude.yml` already do. A unit
  test pins this.
- Quote `TESSERACT_VERSION` in the Dockerfile's `apt-get install`, and add a default case
  and named locals to `ops/reset/reset-environment.sh`.
- Run the fixes as parallel work streams. Each stream is one Fable 5.1 sub-agent in its own
  worktree, and the streams touch disjoint files.

No API, schema, migration, configuration or workflow-input change. The served OpenAPI document
stays byte-identical.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `release-pipeline`: add a requirement that the container image installs its third-party
  dependencies from locked wheels only, and that a unit test fails when a `uv sync` in
  `docker/Dockerfile` loses `--no-build`.

## Impact

- Code: about 40 modules under `src/curate_d/` (`api/`, `runtime/sweep.py`, `worker/main.py`,
  `workflows/`, `placement/`, `classification/`, `staging/`, `ledger/tables.py` and smaller
  ones). Refactors only, plus their unit tests.
- DBOS-sensitive code: `runtime/sweep.py`, `worker/main.py` and the `workflows/` bodies. The
  refactors extract plain helpers only. No step, transaction or child-workflow call is added,
  removed or reordered, so in-flight workflows replay unchanged.
- Build: `docker/Dockerfile` (`--no-build`, a quoted variable) and
  `tests/unit/infrastructure/test_dockerfile_pins.py`. The image build fails if a future
  dependency ships as an sdist only. Today `uv.lock` has none.
- Ops: `ops/reset/reset-environment.sh`. The script validates its environment argument
  earlier, so the new default case cannot run today.
- SonarQube Cloud: status changes (Accepted or False positive, with a comment) on the issues
  that the apply cannot fix safely in code.
- No dependency, API, schema or migration change.
