# Tasks

Streams A and B touch disjoint files and can start at once; group 3 waits on both.
Stream B's new tests fail until stream A's detection step and filter exist, so stream B
may be written first but closes green only after A lands.

## 1. Stream A: workflow (`.github/workflows/integration.yml`, `.github/workflows/checks.yml`, `Makefile`, `README.md`, `CONTRIBUTING.md`, `docs/staging.md`)

- [x] 1.1 Add a detection step (`id: changes`) to the `integration` job after "Refuse a
  CI-sized observation budget" and before "Install Tesseract", running
  `dorny/paths-filter` pinned by commit SHA (version in a comment) with one filter key,
  `suite`, listing exactly the design's declared paths as `dir/**` prefixes or exact
  files; verify with `actionlint .github/workflows/integration.yml` (or
  `uv run --locked --no-build python -c "import yaml; yaml.safe_load(open('.github/workflows/integration.yml'))"`
  if actionlint is not installed) and by reading the filter list against design.md's table
- [x] 1.2 Add a guard step after the detection that fails with `::error::` naming the
  detection when `steps.changes.outputs.suite` is neither `true` nor `false`, and a
  "Suite not selected" step, `if: steps.changes.outputs.suite == 'false'`, that appends to
  `GITHUB_STEP_SUMMARY` that the suite was not run and lists the declared paths; verify
  with the YAML load from 1.1
- [x] 1.3 Condition every step after the guard except "Remove the stack" on
  `steps.changes.outputs.suite == 'true'` (`always() && ...` on "Record staging
  evidence", `failure() && ...` on "Stack logs"), leaving the job's `name`, the step
  order, the budget guard's `if`, the teardown's `always()` and the `concurrency` block
  unchanged; verify with
  `uv run pytest tests/unit/infrastructure/test_integration_workflow.py --no-cov`
  (the existing tests must still pass)
- [x] 1.4 Reword the `integration.yml` header, the `checks.yml` header line about the
  integration suite, the `Makefile` header and `check` target comment, `README.md`'s
  `make check` line, `CONTRIBUTING.md`'s merge-gate line and `docs/staging.md`'s
  "runs on every pull request" sentence to say the suite runs when a pull request changes
  a path it depends on; verify with
  `grep -rn "every pull request" .github/workflows Makefile README.md CONTRIBUTING.md docs/staging.md`
  returning no line that still claims the suite runs on every pull request

## 2. Stream B: pin tests (`tests/unit/infrastructure/test_integration_workflow.py`)

- [x] 2.1 Add `test_the_gate_detects_changes_inside_the_required_job`: the workflow has
  only the `integration` job, its detection step (`id: changes`, a `dorny/paths-filter`
  `uses` pinned by a 40-hex SHA) comes after the budget guard and before "Install
  Tesseract", the guard step follows it and exits non-zero, and every step after the guard
  except "Remove the stack" has an `if` containing `steps.changes.outputs.suite == 'true'`
  (the skip notice: `== 'false'`), failing with the step's name; verify with
  `uv run pytest tests/unit/infrastructure/test_integration_workflow.py --no-cov`
- [x] 2.2 Add `test_declared_paths_are_prefixes_or_exact_files` and
  `test_the_declared_paths_are_the_expected_set`: parse the `suite` filter from the
  workflow YAML, assert every pattern is `dir/**` or a plain path (no other glob
  characters), and assert the set equals the literal list from proposal.md; verify with
  the same command
- [x] 2.3 Add `test_every_stack_mount_copy_and_test_read_is_declared`: collect
  `compose.yaml` `./` bind-mount sources, `docker/Dockerfile` `COPY` sources without
  `--from=` minus `README.md`, `LICENSE`, `NOTICE`, and the quoted repository paths
  `tests/integration/*.py` join onto `_ROOT` or `REPOSITORY_ROOT`, and assert a declared
  pattern covers each, failing with the uncovered path; verify with the same command, and
  by temporarily removing `infrastructure/rustfs/**` from the filter and seeing the test
  name `infrastructure/rustfs/initialize.sh`

## 3. Integration check

- [ ] 3.1 Open the pull request against `dev`; confirm `Integration tests` runs the suite
  and passes (the workflow file is a declared path), and record the run URL in the PR
  description. The skip path can't be shown before merge: the workflow triggers only on
  pull requests to `dev` or `main`, so a draft into the PR branch never starts it (see
  Workflow follow-up)
- [x] 3.2 Run `make lint typecheck test` and verify it passes with the coverage floor met
- [x] 3.3 Run `openspec validate scope-integration-tests-to-touched-paths --strict` and
  verify it passes

## Workflow follow-up

- After the pull request merges into `dev`, confirm on the next docs-only pull request to
  `dev` that it is mergeable with `Integration tests` passing in about a minute, every
  step after the guard skipped and the summary naming the declared paths.
- Sync the delta into `openspec/specs/release-pipeline/spec.md` (`openspec-sync-specs`).
- Archive the change (`openspec-archive-change`).
