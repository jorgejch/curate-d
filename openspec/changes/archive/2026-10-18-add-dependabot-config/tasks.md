# Tasks

Stream 0 is done (the delta spec in this change). Streams A and B touch disjoint files
and can start at once; group 3 waits on both. Execution agents run on Opus 5.5.

## 1. Stream A: Dependabot config (`.github/dependabot.yml`, `tests/unit/infrastructure/test_dependabot_config.py`)

- [x] 1.1 Create `.github/dependabot.yml` per design D1: `version: 2`; `uv` at `/`,
  `github-actions` at `/`, `docker` at `/docker`; `target-branch: dev` and
  `schedule: weekly, day: friday, time: "16:00", timezone: America/Los_Angeles` on each;
  `versioning-strategy: increase` on `uv`; groups `python-patches` (patch),
  `actions-patches-and-minors` (patch, minor), `image-patches` (patch), each
  `applies-to: version-updates`, `patterns: ["*"]`; ignores `aioboto3`
  (`version-update:semver-minor`) and `python` (`semver-minor`, `semver-major`). Comment
  the two ignores and the digest behaviour (D2, D3) in "why" form; verify with
  `uv run --locked --no-build python -c "import yaml; yaml.safe_load(open('.github/dependabot.yml'))"`
- [x] 1.2 Create `tests/unit/infrastructure/test_dependabot_config.py` per design D5, one
  test per spec scenario family: ecosystems present and no `docker-compose`;
  `target-branch: dev` on every entry; weekly Friday 16:00 `America/Los_Angeles` on every entry;
  `uv` uses `increase`; each group's `applies-to` and exact `update-types`; `aioboto3`
  ignore is exactly `["version-update:semver-minor"]`; `python` ignore is exactly minor
  and major; no ignore lists `semver-patch`. Failure messages name the entry; verify with
  `uv run pytest tests/unit/infrastructure/test_dependabot_config.py --no-cov`
- [x] 1.3 Mutation check: temporarily drop `target-branch` from one entry and add `major`
  to one group, confirm the test fails naming them, revert; verify with the same pytest
  command red then green

## 2. Stream B: Dockerfile pins (`docker/Dockerfile`, `tests/unit/infrastructure/test_dockerfile_pins.py`)

- [x] 2.1 Resolve current digests from the registries, never from memory:
  `docker buildx imagetools inspect ghcr.io/astral-sh/uv:0.12.19` and
  `docker buildx imagetools inspect python:3.12.<patch>-slim-trixie`, using the
  manifest-list digest each prints. Pick the Python patch: 3.12.14
  (`infrastructure/staging-compatibility.md:36`) unless Docker Hub lists a newer 3.12
  patch for `-slim-trixie`, and note the chosen patch for the pull-request body; verify by
  re-running each inspect and comparing the digest
- [x] 2.2 Rewrite `docker/Dockerfile` per design D4: delete `ARG PYTHON_VERSION=3.12` and
  `ARG UV_VERSION=0.12.19` (lines 16-17); lines 20, 26 and 53 become
  `FROM ghcr.io/astral-sh/uv:0.12.19@sha256:<digest> AS uv`,
  `FROM python:3.12.<patch>-slim-trixie@sha256:<digest> AS builder` and the same image
  `AS runtime`; keep `ARG TESSERACT_VERSION` and every stage name; update the header
  comment if it mentions the build args; verify with `make image` building to completion
  and `docker run --rm curate-d:dev python --version` printing the chosen patch
- [x] 2.3 Create `tests/unit/infrastructure/test_dockerfile_pins.py` per design D5: every
  `FROM` image reference fullmatches `[a-z0-9./-]+:[\w][\w.-]*@sha256:[0-9a-f]{64}` with no
  `$`, and no `ARG PYTHON_VERSION`/`ARG UV_VERSION` line remains; failure message is the
  line; verify with `uv run pytest tests/unit/infrastructure/test_dockerfile_pins.py --no-cov`
- [x] 2.4 Mutation check: temporarily restore `FROM python:${PYTHON_VERSION}-slim-trixie`
  on one line, confirm the test fails naming it, revert; verify with the same pytest
  command red then green
- [x] 2.5 Confirm the existing Dockerfile readers still pass:
  `uv run pytest tests/unit/infrastructure/test_release_pipeline.py tests/unit/infrastructure/test_integration_workflow.py --no-cov`

## 3. Integration check

- [x] 3.1 Run `make check` (lint, the workflow audit, mypy, the unit suite) and verify it
  passes with the 80% floor met
- [x] 3.2 Run `make image` from a clean build cache (`docker builder prune -f` first, or
  `--no-cache`) and verify the image builds from the pinned digests alone; then
  `make stack-up` and verify `docker compose ps` shows api, worker-io and worker-cpu healthy,
  then `make stack-down`
- [x] 3.3 Run `openspec validate add-dependabot-config --strict` and verify it passes
- [x] 3.4 Open the pull request against `dev` with a body stating the chosen Python patch
  and both digests, and verify the integration job runs the whole suite (the Dockerfile is a
  declared path) and the checks job's SonarQube scan reports

## Workflow follow-up

- After merge, inspect Dependabot's first `uv` pull request: the rewritten
  `pyproject.toml` range must raise its floor, not widen (design Risks, #16112). If it
  widens, open a change switching the strategy.
- Sync the delta spec into `openspec/specs/release-pipeline/spec.md` and archive the
  change once the pull request has merged.
