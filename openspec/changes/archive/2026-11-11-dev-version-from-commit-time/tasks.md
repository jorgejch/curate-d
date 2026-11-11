# Tasks

Two streams touch disjoint files and can run at once; stream 3 waits on both.

## 1. CI

- [x] 1.1 In `.github/workflows/docker-publish.yml`, add a dev-only step that reads the commit's UTC committer time from git and fails unless it is 14 digits, and build the dev tag from it instead of `{{date}}`
- [x] 1.2 In `tests/unit/infrastructure/test_release_pipeline.py`, check the tag takes the step's output, the step reads the commit in UTC, and runs only on `dev`

## 2. Application version

- [x] 2.1 In `src/curate_d/runtime/dbos_app.py`, return the dev tag itself as the version, and update the comments and docstring
- [x] 2.2 In `tests/unit/runtime/test_dbos_app.py` and `test_release_pipeline.py`'s packaged-version test, expect the tag; check it is a Kubernetes label value
- [x] 2.3 Update `ops/README.md`'s dev version paragraph

## 3. Integration

- [x] 3.1 Sync the delta into `openspec/specs/release-pipeline/spec.md` and archive in the same pull request
