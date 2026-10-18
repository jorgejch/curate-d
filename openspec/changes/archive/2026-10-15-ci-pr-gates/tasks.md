# Tasks

Work streams A, B and C touch disjoint files and can start at once. Stream D waits for all
three. Every stream runs `make check` before its last commit; the workflow-shape tests are
the automated check on each YAML change, and `actionlint` (install locally, it is not in
the repo) is the syntax check.

## 1. Stream A: integration workflow (`.github/workflows/integration.yml`, `tests/unit/infrastructure/test_integration_workflow.py`, `tests/integration/test_staging_backend_checks.py`, `docs/staging.md`)

- [x] 1.1 Spike: add `integration.yml` per design D4 (PR to `dev`/`main` trigger, Tesseract
  install, image build with gha cache, staged `compose up`, migrations, serial
  `pytest tests/integration --no-cov -m integration --staging-backend=rustfs
  --junitxml=integration.xml -o junit_family=legacy`, `compose logs` on failure,
  `down --volumes` always), push the branch and open the PR. Verify: the job runs on the
  PR; record its wall-clock time and every failing test in the PR description. If
  `test_local_ocr.py` fails on Ubuntu's Tesseract, switch that file to the in-image run
  from D4 and re-verify.
- [x] 1.2 Fix or report what 1.1 found: a test failing only because of the CI environment is
  fixed in the workflow (env, ordering, waits); a test failing on its own is reported to the
  user before anything is deselected. Verify: the integration job is green on the PR.
  Found: `test_api_configuration.py`'s `restore` fixture restored nothing on a fresh
  database, leaking a test template into `test_dry_run_workflow.py`; fixed by seeding the
  defaults before the snapshot (user chose the fixture fix, 2026-10-15).
- [x] 1.3 Move the "Refuse a CI-sized observation budget" guard step into `integration.yml`
  ahead of the suite, and set `timeout-minutes` from 1.1's measurement plus headroom, and a
  per-PR concurrency group with cancel-in-progress. Verify: `actionlint` clean; the job
  still passes.
- [x] 1.4 Rewrite the evidence summary step to read `integration.xml`: contract-suite and
  backend-check counts by `classname` prefix, the OVH-PENDING statement, and the rows 30/31
  statement (skipped against the recorded observation, re-observed with
  `make lifecycle-check` on a pin bump). Verify: the step summary on the PR run shows all
  three sections with non-zero counts.
- [x] 1.5 Change `_RECORDED_LIFECYCLE_OBSERVATIONS["rustfs"]` to drop ", re-run in CI", and
  update `docs/staging.md`'s CI paragraph so it describes the full suite and the manual
  lifecycle re-check. Verify: `grep -rn "re-run in CI"` finds nothing; `make test` passes.
- [x] 1.6 Add `test_integration_workflow.py`: trigger is `pull_request` to `dev` and `main`
  only; job `name` is `Integration tests`; the pytest command targets `tests/integration`
  with `--no-cov`, `-m integration`, `--staging-backend=rustfs`, no `--cov-report`, no
  `--deselect`; the budget guard step exists and runs before the suite; a `down --volumes`
  step has `if: always()`. Verify: `uv run pytest tests/unit/infrastructure -q` green.

## 2. Stream B: checks workflow (`.github/workflows/checks.yml`, `.github/workflows/unit-tests.yml`, `sonar-project.properties`, `.gitignore`, `tests/unit/infrastructure/test_checks_workflow.py`)

- [x] 2.1 Add `sonar-project.properties` and the `/coverage.xml` ignore from
  `origin/ci/sonarqube` (fa970bb). Verify: `uv run pytest -n auto --cov-report=xml` writes
  `coverage.xml` and `git status --porcelain` stays clean.
- [x] 2.2 Add `checks.yml` per design D2/D3: `push: branches: ['**']`, `pull_request:
  branches: [dev, main]`; job `checks` (`name: Checks`) with lint, workflow audit, mypy,
  `pytest -n auto --cov-report=xml`, Sonar cache and scan (pins from fa970bb, fork-only
  skip, runs when `coverage.xml` exists), `if:` excluding dev/main pushes; job
  `coverage-report` with the complementary `if:`, unit suite and scan only; per-ref
  concurrency with cancel-in-progress; `permissions: contents: read`. Verify: `actionlint`
  clean; on the PR, `Checks` is green and `coverage-report` shows skipped; SonarQube Cloud
  shows the PR analysis with coverage.
- [x] 2.3 Delete `unit-tests.yml`. Verify: the feature-branch push runs `Checks` and no
  `Unit Tests` workflow; SonarQube Cloud lists the feature branch with coverage.
- [x] 2.4 Add `test_checks_workflow.py`: both trigger lists; `checks` named `Checks`; the
  two jobs' `if:` conditions are complementary and name `refs/heads/dev` and
  `refs/heads/main`; the unit command carries `--cov-report=xml`; `coverage-report` has no
  ruff or mypy step; the scan's `if:` compares the head repo to `github.repository`.
  Verify: `uv run pytest tests/unit/infrastructure -q` green.

## 3. Stream C: build-only publish workflow (`.github/workflows/docker-publish.yml`, `tests/unit/infrastructure/test_release_pipeline.py`, `Makefile`)

- [x] 3.1 Reduce `docker-publish.yml` to `build-and-push`: triggers `push` to `dev` and the
  semver tag pattern only; move the two tag-guard steps to the top of the job with
  `fetch-depth: 0`; delete the `check` job, its staging steps and summary; drop `needs:`;
  rewrite the header comment for the new split. Verify: `actionlint` clean; the workflow
  does not run on the PR.
- [x] 3.2 Update `test_release_pipeline.py`: remove the "gates merges" tests; add tests that
  there is no `pull_request` trigger, the push branches are exactly `['dev']`, the only job
  is `build-and-push`, and both guard steps are present with their `startsWith(github.ref,
  'refs/tags/')` conditions; keep the tag-shape and emitted-tag tests. Verify:
  `uv run pytest tests/unit/infrastructure -q` green.
- [x] 3.3 Correct the `Makefile` header comment (CI no longer runs `lifecycle-check`; it
  runs `lint`, `typecheck`, `test` and the full `test-integration`). Verify: `make help`
  output unchanged; `grep -n lifecycle-check Makefile` shows only the target and its
  variables.

## 4. Stream D: docs, ruleset, rollout (after A, B and C)

- [x] 4.1 Update `README.md` (the `make lifecycle-check` and `make check` lines' CI notes,
  the development section) and `CONTRIBUTING.md` (the "Run `make check`" bullet and the
  integration-tests bullet) to the D2 matrix: PRs to `dev`/`main` run the checks and the
  full integration suite as required checks; feature pushes run the checks; merges refresh
  SonarQube. Verify: `grep -n "staging integration" README.md CONTRIBUTING.md` finds no
  stale claim.
- [x] 4.2 With the PR's `Checks` and `Integration tests` green: save the current ruleset
  (`gh api repos/jorgejch/curate-d/rulesets/24870763 > ruleset-before.json`, outside the
  repo), check Rules → Insights for `code_coverage` evaluations, then, with the user's
  confirmation, PUT the ruleset with required contexts `Checks` and `Integration tests`
  (integration_id 15368), `strict_required_status_checks_policy: true`, the
  `code_scanning` rule unchanged with both CodeQL and SonarCloud (the trial of design D9),
  and without `code_coverage` unless it is fed. Verify:
  `gh api .../rulesets/24870763 --jq '.rules[]|select(.type=="required_status_checks" or .type=="code_scanning")'`
  shows the two contexts, `strict: true` and both tools; the PR's merge box lists `Checks`,
  `Integration tests` and the SonarCloud code-scanning result as required.
- [x] 4.3 Final verification: `make check` green locally; on the PR, `Checks` and
  `Integration tests` green, `coverage-report` and `Build and Publish Container` not run;
  SonarQube Cloud shows the PR analysis. Record the integration runtime in the PR
  description.

## Workflow follow-up

- Merge the PR. Verify on `dev`: `build-and-push` pushes one `dev-<timestamp>-<sha>` image,
  `coverage-report` runs and SonarQube Cloud's `dev` branch shows the merge commit's
  coverage, and no `Checks` or `Integration tests` job ran on the push.
- Rebase open PRs on `dev` (strict mode).
- Sync the `release-pipeline` spec and archive the change.
