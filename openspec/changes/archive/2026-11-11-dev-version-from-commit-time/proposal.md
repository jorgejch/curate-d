# Proposal

## Why

A dev image's tag, `dev-<UTC timestamp>-<sha>`, takes its timestamp from the moment the build
runs. Two builds of one commit therefore publish two images and run as two DBOS application
versions, and re-running the build of an older commit publishes a tag that sorts after newer
commits, so Flux's dev ImagePolicy rolls the dev environment back to the older code.

A stamp fixed by the commit alone would make a rebuild republish the same tag. The image is not
fixed by the commit alone: apt resolves the system packages when the image is built, and
`TESSERACT_VERSION=5.5.*` floats. A rebuild could then put other contents under a published
identifier, which Twelve-Factor V.2 (immutable releases) forbids.

The dev DBOS version, `<package>+dev.<timestamp>.<sha>`, also can't be a Kubernetes label value
or a name: `+` is not allowed in either. A controller that manages workers per application
version (`jorgejch/dbos-version-controller`, see the adoption plan on the curate.d Notion page)
names its resources and labels its pods by version, and accepts only `^[a-z0-9][a-z0-9.-]{0,48}$`.

## What Changes

- The dev tag becomes `dev-<UTC commit timestamp>-<sha>-<environment hash>`. The timestamp is
  the commit's committer time, read from git in CI. The environment hash is the first 12 hex
  digits of the SHA-256 of the image's sorted list of installed system packages, which a
  `build-environment` Dockerfile target exports before the tags are named.
- Each dev image is also tagged with its commit alias, `dev-<UTC commit timestamp>-<sha>`.
- A build whose dev commit alias, or whose release tag, is already published pushes nothing. A
  published tag never changes, and each dev commit has exactly one image.
- A dev image's DBOS application version is its hashed tag. The Docker build fails, and the
  application refuses to import, when the tag's hash is not the image's own package list's. A
  release image still runs as the bare packaged version.
- The Dockerfile frontend is pinned by digest, so the system packages are the only image input
  the commit does not fix.
- flux-ovh's dev ImagePolicy selects the hashed tags (a pull request in that repository).
- Tests, `ops/README.md`, `docs/twelve-factor.md` and the code's comments follow.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `release-pipeline`: the dev tag's timestamp is the commit's and its last part names the
  system packages; the hashed tag is the dev image's DBOS application version; a published
  image is never pushed again.

## Impact

- Files: `.github/workflows/docker-publish.yml`, `docker/Dockerfile`,
  `src/curate_d/runtime/dbos_app.py`, `tests/unit/runtime/test_dbos_app.py`,
  `tests/unit/infrastructure/test_release_pipeline.py`, `ops/README.md`,
  `docs/twelve-factor.md`, `openspec/specs/release-pipeline/spec.md`.
- Behaviour: the first dev build after this merges runs as
  `dev-<timestamp>-<sha>-<environment hash>`; the build before it ran as
  `0.1.0+dev.<timestamp>.<sha>`. Like any new dev build, it is a new DBOS version, so work the
  previous build left is moved by the recovery sweep as today.
- flux-ovh: the dev ImagePolicy's pattern must accept the hashed tag. Until it does, it matches
  the commit alias, which points at the same image, so the merge order does not break dev.
