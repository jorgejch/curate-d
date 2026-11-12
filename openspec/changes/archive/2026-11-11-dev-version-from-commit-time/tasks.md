# Tasks

Streams 1 and 2 touch disjoint files and can run at once; stream 3 waits on both.

## 1. CI and image

- [x] 1.1 In `.github/workflows/docker-publish.yml`, add a dev-only step that reads the commit's UTC committer time from git and fails unless it is 14 digits, and build the dev tag from it instead of `{{date}}`
- [x] 1.2 In `docker/Dockerfile`, pin the frontend by digest, split a `system` stage that writes the sorted package list, add a `build-environment` target holding it, and fail the build when the build revision's last part is not the list's hash
- [x] 1.3 In the workflow, build the `build-environment` target, hash the list, tag dev images `dev-<timestamp>-<sha>-<hash>` and with the commit alias, and push nothing when the commit alias or release tag is already published
- [x] 1.4 In `tests/unit/infrastructure/test_release_pipeline.py`, run the stamp, hash and publish-check scripts (the stamp in a throwaway, isolated copy of the repository; the publish check against a fake registry), and pin the step order, the Dockerfile's pins and its check

## 2. Application version

- [x] 2.1 In `src/curate_d/runtime/dbos_app.py`, return the hashed dev tag itself as the version, and refuse a tag whose hash is not the image's package list's
- [x] 2.2 In `tests/unit/runtime/test_dbos_app.py` and `test_release_pipeline.py`'s packaged-version test, expect the tag; check it is a Kubernetes label value and that a wrong or unreadable list is refused
- [x] 2.3 Update `ops/README.md`'s dev version paragraph and tag tables, and `docs/twelve-factor.md`'s V.2 and II.1 rows

## 3. Integration

- [x] 3.1 Sync the delta into `openspec/specs/release-pipeline/spec.md` and archive in the same pull request
- [ ] 3.2 Open the flux-ovh pull request that makes the dev ImagePolicy select `dev-<timestamp>-<sha>-<hash>`
