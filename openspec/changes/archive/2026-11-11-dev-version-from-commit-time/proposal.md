# Proposal

## Why

A dev image's tag, `dev-<UTC timestamp>-<sha>`, takes its timestamp from the moment the build
runs. Two builds of one commit therefore publish two images and run as two DBOS application
versions, and re-running the build of an older commit publishes a tag that sorts after newer
commits, so Flux's dev ImagePolicy rolls the dev environment back to the older code.

The dev DBOS version, `<package>+dev.<timestamp>.<sha>`, also can't be a Kubernetes label value
or a name: `+` is not allowed in either. A controller that manages workers per application
version (`jorgejch/dbos-version-controller`, see the adoption plan on the curate.d Notion page)
names its resources and labels its pods by version, and accepts only `^[a-z0-9][a-z0-9.-]{0,48}$`.

## What Changes

- The dev tag's timestamp is the commit's UTC committer time, read from git in CI, so every
  build of one commit gets the same tag. The shape `dev-<14-digit UTC timestamp>-<sha>` and the
  Flux ImagePolicy that sorts on it don't change.
- A dev image's DBOS application version is its tag, `dev-<timestamp>-<sha>`, instead of
  `<package>+dev.<timestamp>.<sha>`. A release image still runs as the bare packaged version.
- Tests, `ops/README.md` and the code's comments follow.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `release-pipeline`: the dev tag's timestamp is the commit's, and the tag is the dev image's
  DBOS application version.

## Impact

- Files: `.github/workflows/docker-publish.yml`, `src/curate_d/runtime/dbos_app.py`,
  `tests/unit/runtime/test_dbos_app.py`, `tests/unit/infrastructure/test_release_pipeline.py`,
  `ops/README.md`, `openspec/specs/release-pipeline/spec.md`.
- Behaviour: the first dev build after this merges runs as `dev-<timestamp>-<sha>`; the build
  before it ran as `0.1.0+dev.<timestamp>.<sha>`. Like any new dev build, it is a new DBOS
  version, so work the previous build left is moved by the recovery sweep as today.
- flux-ovh needs no change: the ImagePolicy pattern and ordering stay as they are.
