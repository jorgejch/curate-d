# Proposal

## Why

The integration job runs the whole suite against the whole stack on every pull request to
`dev` or `main`, about ten minutes of runner time, even when the pull request changes
nothing the suite can observe: a doc, a unit test, a GitHub template, an OpenSpec change.
Those runs gate nothing and delay the merge; the suite should run only when a file it
reads, imports, builds or boots has changed, and the decision must fail closed: a broken
detection must fail the required check, never let the merge through unverified.

## What Changes

- The `Integration tests` job gains a detection step, right after checkout, that lists the
  pull request's changed files and compares them with the set of paths the integration
  suite depends on. Every later step (Tesseract, uv, the image build, the stack, the
  suite, the application boot, the schedule check, the evidence, the logs) runs only when
  the detection reported a match. When it reports none, the job runs no test, writes to
  the step summary which paths select the suite, and passes: the required check is green
  and a docs-only pull request merges without a suite run.
- Detection is part of the required job, so a failing detection step fails `Integration
  tests` and blocks the merge. A guard step fails the job if the detection produced
  neither `true` nor `false`.
- The dependency set is declared once, in the workflow, as directory prefixes and exact
  files: `src/`, `tests/integration/`, `tests/stubs/`, `tests/__init__.py`, the
  infrastructure the stack mounts or a test reads (`infrastructure/postgres/`,
  `infrastructure/rustfs/`, `infrastructure/staging-compatibility.md`), `migrations/`,
  `alembic.ini`, `compose.yaml`, `docker/Dockerfile`, `.dockerignore`, `Makefile`,
  `pyproject.toml`, `uv.lock`, `.python-version` and the workflow itself. `README.md`,
  `LICENSE` and `NOTICE` are copied into the image for packaging metadata only and are
  left out, as are `infrastructure/reset/`, `infrastructure/README.md`,
  `compose.lifecycle.yaml`, `tests/unit/`, `tests/local/`, `docs/`, `openspec/` and the
  other workflows.
- A unit test pins the declared set against what the repository actually wires in: every
  host path `compose.yaml` bind-mounts, every `COPY` source in `docker/Dockerfile` except
  the three packaging files, and every repository file an integration test reads by path.
  A new mount, copy or read that the set does not cover fails the unit suite. Another
  pins the gate's shape: detection is a step of the required job, every step after it is
  conditioned on its output, and the stack teardown still runs on every outcome.
- The headers of `integration.yml`, `checks.yml` and the `Makefile`, and the lines in
  `README.md`, `CONTRIBUTING.md` and `docs/staging.md` that say the suite runs on every
  pull request, say when it runs instead.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `release-pipeline`: the merge-gate requirement admits an integration job that ran no
  test when the pull request changes none of the suite's inputs; a new requirement states
  that the job itself detects the changed paths, that a detection failure fails the job,
  and that the declared set is pinned against the stack definition.

## Impact

- `.github/workflows/integration.yml`: a detection step and a guard, a condition on every
  later step, a skip notice in the step summary, a reworded header.
- `tests/unit/infrastructure/test_integration_workflow.py`: the pin tests for the gate's
  shape and the declared set.
- `openspec/specs/release-pipeline/spec.md` (on sync).
- Comments and docs: `.github/workflows/checks.yml`, `Makefile`, `README.md`,
  `CONTRIBUTING.md`, `docs/staging.md`.
- The ruleset is unchanged: `Integration tests` stays the required check's name, and it
  reports a result on every pull request.
- No runtime, image or deployment change.
