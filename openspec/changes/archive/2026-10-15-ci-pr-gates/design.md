# Design

## Context

See proposal.md for motivation. The facts that shape the approach:

- Three workflows exist. `docker-publish.yml` holds a `check` job (lint, mypy, unit suite,
  then four staging integration files and `make lifecycle-check` against a Compose
  PostgreSQL and RustFS) and `build-and-push`, which `needs: check`. It triggers on PRs
  to `main` and pushes to `main`, `dev` and semver tags. `unit-tests.yml` runs the unit
  suite on pushes to every branch but `main` and `dev`. `claude.yml` is unrelated.
- The GitHub ruleset `default` (id 24870763) covers `refs/heads/dev` and `refs/heads/main`.
  It requires the status contexts `check` and `Unit tests and SonarQube analysis`, with
  `strict_required_status_checks_policy: false`. The second context was introduced by the
  closed PR #34 and nothing on `dev` reports it, so every PR to `dev` currently waits on
  it. Its `code_scanning` rule requires results from two tools: CodeQL (alerts: errors;
  security: high or higher) and, since 2026-10-15 as a trial, SonarCloud (alerts: errors
  and warnings; security: high or higher). The ruleset also carries a `code_coverage` rule
  at 80% and a `code_quality` rule; nothing in the repository uploads coverage to GitHub.
- PR #34 (`ci/sonarqube`, closed, branch still on the remote at fa970bb) holds a working
  `sonarqube.yml`, `sonar-project.properties` and the `coverage.xml` ignore. Its runs
  passed, so `SONAR_TOKEN` is configured and the Sonar project key is right.
- `tests/integration` has 47 test files. Only the staging subset has ever run in CI. The
  suite isolates itself from a running stack by giving each session its own DBOS
  application version (`tests/integration/conftest.py`), shares one database, and relies
  on advisory locks, so it runs serially. `test_local_ocr.py` calls the host's Tesseract
  through `pytesseract`; `test_rclone.py` and the transfer harness need the rclone daemon
  on 5572; `test_end_to_end.py` runs the API in-process.
- SonarQube Cloud keeps pull-request analyses apart from branch analyses: a PR scan never
  changes `dev`'s or `main`'s measures.
- Rows 30/31 (RustFS lifecycle expiry and stale-upload reclamation) are recorded in
  `_RECORDED_LIFECYCLE_OBSERVATIONS` (2026-09-26) and in `infrastructure/
  staging-compatibility.md`. Without a budget they skip, citing the record. The pin-bump
  checklists in `docs/staging.md` and `staging-compatibility.md` call `make
  lifecycle-check`.

## Goals / Non-Goals

**Goals:**

- One place decides whether a change may merge: the PR's two required checks.
- `build-and-push` does nothing but build, on the two events that need an image.
- SonarQube Cloud shows unit coverage for every branch and for every merge into `dev`
  and `main`.
- The full integration suite runs in CI against the stack `make stack-up` brings up.
- Each workflow file has one purpose and no job is shared across purposes.

**Non-Goals:**

- Requiring SonarQube's quality gate as a status check, or deciding the code-scanning
  trial (D9). This change keeps the ruleset's SonarCloud code-scanning rule because it is
  already there; whether it stays is a later decision. The 80% overall floor is enforced
  by `--cov-fail-under` in the required checks job.
- Running the integration suite on feature-branch pushes, or in parallel. The suite shares
  one database.
- Running the OVH backend in CI. OVH verification stays PENDING, as today.
- A merge queue. Strict up-to-date checks give the same guarantee at this contributor
  count.
- Changing what `make check` or `make test-integration` run.

## Decisions

### D1. Three workflow files, one purpose each

`checks.yml` (lint, type check, unit suite, SonarQube), `integration.yml` (full suite
against the stack, PRs only), `docker-publish.yml` (build and push only).
`unit-tests.yml` is deleted; `checks.yml` is its successor with a wider trigger.

*Alternative:* one `ci.yml` with job-level conditions. Rejected: it was tried in PR #33 and
reverted, and a single file makes the per-event behaviour a matrix of `if:` expressions
that the shape tests then have to reverse-engineer.

*Alternative:* keep the checks and integration jobs in `docker-publish.yml` and widen its
`pull_request` trigger to `dev`. Rejected: `build-and-push` would stay coupled to a test
job through `needs:`, and the file's name would describe a third of what it does.

### D2. Trigger matrix

```
event                   checks.yml            integration.yml   docker-publish.yml
---------------------   -------------------   ---------------   ------------------
push feature branch     checks                -                 -
pull_request dev|main   checks  (REQUIRED)    integration       -
                                              (REQUIRED)
push dev                coverage-report       -                 build-and-push
push main               coverage-report       -                 -
push tag vX.Y.Z         -                     -                 guards + build-and-push
```

`checks.yml` triggers on `push: branches: ['**']` and `pull_request: branches: [dev,
main]`. A branch filter alone excludes tags. Two jobs with complementary `if:` conditions
on `github.event_name == 'push' && (ref == refs/heads/dev || ref == refs/heads/main)`:
`checks` runs everywhere else, `coverage-report` only there. Two jobs rather than
step-level conditions in one job, so the required check has one name and one meaning and
the dev/main variant shows as a distinct, skipped job on PRs.

A feature branch with an open PR runs `checks` twice per commit (push and PR). Accepted:
the unit suite is a few minutes, and the push run is what gives SonarQube the branch
analysis.

### D3. The `checks` job

The steps of today's `check` job up to and including the unit suite: `ruff check`, `ruff
format --check`, the workflow database-access audit, `mypy`, `pytest -n auto
--cov-report=xml`. Then the SonarQube scan (`SonarSource/sonarqube-scan-action`, pinned by
commit), which runs when `coverage.xml` exists even if the unit step failed the floor, so
the dashboard shows the coverage that failed it; the job stays red. Fork PRs have no
`SONAR_TOKEN`, so the scan is skipped only when `github.event.pull_request.head.repo.
full_name != github.repository`; any other run without the token fails rather than
skipping. `fetch-depth: 0` for SCM blame. Concurrency group per ref, cancel in progress.

`coverage-report` is the same checkout, install, `pytest -n auto --cov-report=xml` and
scan, without lint and mypy. It is the only way the long-lived branches' measures move in
SonarQube Cloud.

`sonar-project.properties`, the `coverage.xml` ignore and the action pins are taken from
`origin/ci/sonarqube` (fa970bb).

### D4. The `integration` job

```
apt-get install tesseract-ocr
build curate-d:dev          docker/build-push-action, push: false, load: true, gha cache
compose up -d --wait postgres rustfs
compose run --rm rustfs-init
alembic upgrade head
compose up -d --wait        rclone, api, worker-io, worker-cpu
refuse CURATE_D_TEST_STAGING_LIFECYCLE_TIMEOUT_SECONDS
pytest tests/integration --no-cov -m integration --staging-backend=rustfs
       --junitxml=integration.xml -o junit_family=legacy
evidence summary            always()
compose logs                failure()
compose down --volumes      always()
```

- **Image built once, before `compose up`.** The Compose services carry `image:
  curate-d:dev` and `build:`; with the image already loaded, `up` does not rebuild. The
  build uses the GitHub Actions cache like the publish job, so a PR verifies the Dockerfile
  the dev image will be built from.
- **Migrate before the application services start.** Today's CI migrates against a bare
  PostgreSQL. Starting the API and workers afterwards matches `make stack-up` followed by
  `make migrate` as developers run it, and keeps DBOS from launching against a schema
  Alembic is about to touch.
- **Tesseract from Ubuntu's apt** (5.3.x on `ubuntu-24.04`), against the Dockerfile's 5.5
  on Debian trixie. `test_local_ocr.py` reads a synthetic high-contrast scan, so the
  version gap should not change the result; the spike (task 1.1) confirms. If it does not
  pass, the fallback is to run that one file inside the built image (`docker compose run
  --rm --no-deps worker-cpu python -m pytest tests/integration/test_local_ocr.py ...`),
  which keeps "no file deselected" true.
- **`--staging-backend=rustfs`** as a flag, not `CURATE_D_TEST_STAGING_BACKENDS`, so the
  selection is in the command the summary quotes. The report header already prints "OVH
  verification PENDING".
- **Serial run.** No `-n`: the suite shares one database and tests advisory locks across
  independent connections.
- **Rows 30/31 skip** without a budget, citing the recorded observation. The
  `_RECORDED_LIFECYCLE_OBSERVATIONS["rustfs"]` text drops ", re-run in CI". The guard that
  fails on a job-wide `CURATE_D_TEST_STAGING_LIFECYCLE_TIMEOUT_SECONDS` moves here
  unchanged: with the file no longer deselected, a budget would run the two tests on the
  real clock and record an unobserved reclamation as passed.
- **Evidence summary** reads one JUnit file. It reports the contract suite and the backend
  checks by `classname` prefix (`tests.integration.test_staging_contract`,
  `...test_staging_backend_checks`), states that OVH verification is PENDING, and states
  that rows 30/31 were skipped against the recorded observation and are re-observed with
  `make lifecycle-check` on a pin bump. The lifecycle section and its XML go.
- **`timeout-minutes`** set from the spike's measurement with headroom; concurrency group
  per PR, cancel in progress.
- No secrets: fork PRs run the full job.

### D5. `docker-publish.yml` is build-only

Triggers: `push` to `dev` and to `v[0-9]+.[0-9]+.[0-9]+`. The `pull_request` and `main`
triggers and the `check` job go. The two tag guards (ancestor of `main`, tag equals
`pyproject.toml` version) become the first steps of `build-and-push`, with `fetch-depth:
0` on its checkout. The job keeps `if: github.event_name == 'push'` as belt and braces;
the shape tests assert the trigger list and the guards.

A push to `main` without a tag runs nothing here. `coverage-report` in `checks.yml` is what
runs on it.

### D6. Ruleset changes, in rollout order

Required status checks become the new job names: `Checks` and `Integration tests` (job
`name:` is the context GitHub reports). `strict_required_status_checks_policy: true`. The
`code_scanning` rule keeps both tools, CodeQL and SonarCloud (D9).
The `code_coverage` rule is removed unless Rules → Insights shows it has evaluated with
data; nothing in the repository feeds it, and a second 80% gate with a different
measurement would either be inert or disagree with pytest's. `code_quality`, CodeQL and the
PR rule are untouched.

The edit is a `gh api --method PUT /repos/jorgejch/curate-d/rulesets/24870763` with the
full rules array, run by hand after the change's PR reports the new contexts green and
before it merges, with the user's confirmation. Between the edit and the merge, no other
PR to `dev` can merge (its head doesn't run `integration.yml` yet); after the merge, open
PRs need a rebase on `dev` for strict mode anyway.

### D7. Shape tests pin the contract

`tests/unit/infrastructure/` gets `test_checks_workflow.py` and
`test_integration_workflow.py` next to `test_release_pipeline.py`, each parsing its YAML
the way the existing file does. They pin: the required job names, the trigger matrix of
D2, the complementary `if:` conditions, `--no-cov` and no `--cov-report` on the
integration command, no `--deselect`, `--staging-backend=rustfs`, the fork-only scan skip,
the budget guard, and that `docker-publish.yml` has no `pull_request` trigger and no job
other than `build-and-push`. The unit suite is what the ruleset trusts, so a rename fails
there first.

### D9. SonarQube results gate merges through code scanning, on trial

On 2026-10-15 the user added SonarCloud to the ruleset's `code_scanning` rule, as a trial:
a PR cannot merge until SonarCloud has uploaded code-scanning results for its head commit
and those results hold no alert at `warning` or above and no security alert at `high` or
above. This is Sonar's issues as GitHub code-scanning alerts (the `SonarCloud` check run
from the `github-advanced-security` app on PR #34). It is not the Sonar quality gate: the
gate's conditions on new-code coverage, duplication and hotspots are reported by the
`SonarCloud Code Analysis` check run, which stays informational.

What this means for the rest of the design:

- Results exist only when the `checks` job's scan step ran. Fork PRs skip the scan (no
  `SONAR_TOKEN`), so a fork PR cannot merge while the rule lists SonarCloud, short of an
  admin bypass. Acceptable for a trial on a repository with one contributor.
- `errors_and_warnings` is strict: one Sonar "major" or "minor" issue in the PR blocks it.
  That is the point of the trial; it is also why the trial can end.
- The scan runs even when the unit step fails the floor (D3), so results are present on a
  PR that reached the unit step.
- The ruleset PUT in task 4.2 sends the full rules array and must carry the
  `code_scanning` rule with both tools, or it silently ends the trial.
- The 80% unit-coverage floor is still enforced only by `--cov-fail-under` in `Checks`.

Trial exit: when the user decides. Ending it is a ruleset edit, not a code change.

### D10. Test code for SonarQube comes from pytest's `testpaths`

SonarQube Cloud's analysis scope ignores `sonar.sources`, `sonar.tests` and their
inclusions (the scanner warns that they "have no effect"), analyses the whole repository,
and takes test code from the first pytest `testpaths` it finds, `pyproject.toml` first.
With `testpaths = ["tests/unit"]`, everything else under `tests/` was production code to
SonarQube: the PR's six changed lines in two integration test files counted as uncovered
new code, and S4790 fired on the OVH request signature in
`test_staging_backend_checks.py` (SHA-1, required by OVH's API) as a new vulnerability.
Once test paths are detected, the Python plugin's file-name test heuristic is switched off,
so nothing else marked those files as tests.

`testpaths` becomes `["tests"]`, and the unit-only run is named by path where it runs:
`make test`, `make coverage` and both `checks.yml` unit steps run `pytest -n auto
tests/unit`. They collect the same 1494 tests as before, and coverage is unchanged. A bare
`pytest` now collects the integration suite too. `sonar-project.properties` drops
`sonar.sources` and `sonar.tests`. No `sonar.exclusions`: every file stays analysed, the
test files as test code (the user's choice, 2026-10-15, over excluding them).

### D8. `make lifecycle-check` stays as a tool

Nothing in the Makefile or `compose.lifecycle.yaml` changes. The Makefile's header comment
and the README line that say CI runs it are corrected; the pin-bump checklists already
describe the manual run.

## Risks / Trade-offs

- [Full suite runtime is unknown; a slow suite makes every PR slow] → task 1.1 measures it
  on the runner before anything is required. `timeout-minutes` and per-PR cancellation
  bound the cost. If the measurement is unacceptable, that is a decision to bring back, not
  a reason to deselect files.
- [A flaky integration test blocks merges] → the first run shows which; a flaky test is a
  bug in the test, fixed or quarantined by name in a follow-up change, not deselected in
  the workflow.
- [Strict up-to-date checks mean "Update branch" plus a full re-run after every merge to
  `dev`] → accepted; it is the condition under which "the gate is in the PR" holds.
- [Tesseract version differs from the image's] → spike; fallback in D4.
- [Compose startup order: API or workers come up before migrations] → D4 migrates between
  the data services and the application services.
- [Two 80% gates disagree] → the ruleset `code_coverage` rule is removed (D6).
- [Ruleset edit lands before the new contexts exist, or the merge lands before the edit]
  → D6's order; the PR's own run proves the contexts before the edit.
- [Dropping `unit-tests.yml` and the `check` job removes the post-merge test on `dev`] →
  that is the design: strict mode guarantees the merged tree was tested as merged.
- [Image build on every PR commit] → gha cache; the build is the Dockerfile's only check
  before it publishes.

## Migration Plan

1. Open the change's PR against `dev`. `checks.yml` and `integration.yml` run from the PR
   head and report `Checks` and `Integration tests`.
2. Iterate until the integration job is green; record its runtime in the PR.
3. Edit the ruleset (D6). The PR now shows the two new contexts as required.
4. Merge. `docker-publish.yml` builds the dev image; `coverage-report` scans `dev`.
5. Rebase open PRs on `dev`.

Rollback: revert the merge commit and restore the previous ruleset rules array (saved in
step 3 before the PUT).

## Open Questions

- Full-suite wall-clock time on `ubuntu-24.04`: answered by task 1.1; sets
  `timeout-minutes`.
- Whether the ruleset's `code_coverage` rule has ever evaluated: answered when editing the
  ruleset (D6); either answer removes it unless it is fed.
