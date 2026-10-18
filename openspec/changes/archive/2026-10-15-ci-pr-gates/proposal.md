# Proposal

## Why

`dev` and `main` are protected and only pull requests reach them, but the merge gate is
split and incomplete: nothing gates a PR to `dev`, the integration suite that does run
(on PRs to `main` and on pushes to `dev`/`main`) is the staging subset, not
`tests/integration`, and SonarQube Cloud receives no coverage at all. A failure on the
`dev` push blocks the dev image after the merge instead of the merge itself. The gate
belongs in the PR, and `build-and-push` should do nothing but build.

## What Changes

- **Pull requests to `dev` and `main` are the only merge gate.** Two required checks:
  lint, type check and the unit suite with its 80% coverage floor, and the full
  `tests/integration` suite against the whole Compose stack (PostgreSQL, RustFS, rclone,
  API and both worker profiles). The ruleset requires both and requires the branch to be
  up to date with its base before merging. The ruleset's code-scanning rule also requires
  SonarCloud results with no warning-level alert, a trial started 2026-10-15; the ruleset
  edit keeps it.
- **`build-and-push` runs only on a push to `dev` (dev image) and on a semver tag whose
  commit is on `main` (prod image).** The `check` job, its staging steps and its evidence
  summary leave `docker-publish.yml`. The two tag guards (tag on `main`, tag equals the
  packaged version) stay in front of the prod build.
- **Every push to a feature branch runs lint, type check and the unit suite, and uploads
  the unit coverage to SonarQube Cloud as a branch analysis.** It gates nothing.
- **A push to `dev` or `main` runs the unit suite and a SonarQube branch analysis**, so
  the long-lived branches' coverage in SonarQube Cloud follows each merge. It gates
  nothing and `build-and-push` does not wait on it.
- **The full integration suite runs in CI for the first time.** `CONTRIBUTING.md` already
  says it does. It runs with the RustFS backend only; OVH verification stays PENDING.
- **`make lifecycle-check` leaves CI.** Rows 30/31 (lifecycle expiry and stale-upload
  reclamation on RustFS) were observed and recorded on 2026-09-26; the target stays as
  the pin-bump re-check tool. The guard that refuses a CI-sized observation budget stays.
- `sonar-project.properties` is added; `coverage.xml` is git-ignored.
- The ruleset's `code_coverage` rule (80%) is removed unless it is found to be fed: the
  coverage floor is enforced once, by the unit-test step, and reported once, to SonarQube.
- `unit-tests.yml` is replaced by the checks workflow. **BREAKING** for the ruleset: the
  required check contexts change from `check` and `Unit tests and SonarQube analysis` to
  the new job names, in the same rollout. The code-scanning rule is untouched.

## Capabilities

### New Capabilities

- `release-pipeline`: what CI runs on each kind of Git event, what gates a merge into a
  protected branch, and when and from where a container image is published.

### Modified Capabilities

None.

## Impact

- `.github/workflows/docker-publish.yml` (build-only), `.github/workflows/unit-tests.yml`
  (removed), new `.github/workflows/checks.yml` and `.github/workflows/integration.yml`.
- `sonar-project.properties` (new), `.gitignore`.
- `tests/unit/infrastructure/test_release_pipeline.py` and new workflow-shape tests.
- `tests/integration/test_staging_backend_checks.py`: the recorded RustFS observation text
  no longer claims a CI re-run.
- `README.md`, `CONTRIBUTING.md`, `Makefile` header, `docs/staging.md`: CI description.
- GitHub ruleset `default` on `dev` and `main`: required check contexts, strict
  up-to-date policy, `code_coverage` rule. Edited by hand, outside the repository.
- CI runtime per PR commit grows by the full integration suite plus an image build; the
  first task measures it.
