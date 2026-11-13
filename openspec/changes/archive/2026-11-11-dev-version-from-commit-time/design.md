# Design

## Context

See `proposal.md`. The dev tag is set in `.github/workflows/docker-publish.yml` with
`docker/metadata-action`'s `{{date 'YYYYMMDDHHmmss'}}`, the time the action runs. The image is
built with the tag as `CURATE_D_BUILD_REVISION`, and `dbos_app.application_version` turns it into
the DBOS version. Nothing in curate.d orders version strings: DBOS's latest version is the one
registered last, and the sweep, promotion and schedule ownership compare versions only for
equality or membership.

Every input of the image but one is fixed by the commit: the base images and the uv image are
pinned by digest, Python packages install from `uv.lock` with `--locked --no-build`, and the
source is the commit. The exception is the system packages: `apt-get` resolves `tesseract-ocr`
(`TESSERACT_VERSION=5.5.*`), `libpq5` and their dependencies from the Debian archive when the
layer is built. The Dockerfile frontend (`# syntax=docker/dockerfile:1.19`) also floated; this
change pins it by digest.

## Goals / Non-Goals

**Goals:**
- A published image tag never changes contents (Twelve-Factor V.2), on dev and on a release.
- A dev tag names everything the image is built from: the commit and the system packages.
- One dev commit, one published image, so no two dev tags share a timestamp.
- Dev tags still sort, as text, in the order commits reached `dev`.
- A dev DBOS version is a valid Kubernetes label value and fits the version controller's
  `spec.version` pattern.

**Non-Goals:**
- Pinning the system packages (II.1). The hash names them; it does not fix them.
- Adopting the version controller; this only removes one of the gaps its plan lists.
- Release versions, which stay the packaged version.

## Decisions

- **Stamp with the commit's committer time, read with `git log`.** Alternatives:
  `metadata-action`'s `{{commit_date}}` (it falls back to the current time when it can't read
  the commit date, which would bring back build-time tags without a sign); a zero-padded
  first-parent commit count (stable too, but it says nothing a person can read, and it changes
  if `dev`'s history is ever rewritten). The checkout already fetches full history, and the step
  fails unless it reads 14 digits.
- **Name the system packages with a hash of their list.** A `system` stage installs them and
  writes `/usr/share/curate-d/build-environment`: one `name:arch=version` line per installed
  package, sorted with `LC_ALL=C`. A `build-environment` target holds only that file. CI builds
  it first, exports it, and takes the first 12 hex digits of its SHA-256 as the tag's last part.
  The list covers every package in the image, so a changed base image package or a changed
  dependency of Tesseract changes the hash too. Twelve digits keep the longest tag the version
  check accepts (a 12-digit sha) at 44 characters, inside the controller's 49 and a label
  value's 63. Alternative: pin every package to an exact version or a `snapshot.debian.org` date,
  so the commit fixes the image (this closes II.1 as well). Not taken here: it is the II.1 fix,
  and security updates would then need a commit each.
- **The image checks its own tag.** The runtime stage reuses the `system` stage, normally from
  the builder's cache. When the cache misses and apt resolves other packages between the two
  builds, the image's own list no longer hashes to the tag. A `RUN` after
  `ARG CURATE_D_BUILD_REVISION` compares the two and fails the build, so nothing is pushed under
  a wrong tag. `application_version` repeats the check at import, so a tag set at run time
  (`CURATE_D_BUILD_REVISION` in a manifest) can't name other packages either, and a dev tag
  without a readable list fails closed.
- **The first published image of a commit wins.** Each dev image is also tagged with the commit
  alias `dev-<timestamp>-<sha>`. Before it builds, CI looks the alias up; if it exists, the run
  pushes nothing. Only the registry's "not found" counts as unpublished, and any other answer
  fails the run. Without this, a rebuild with other packages would publish a second hashed tag
  with the commit's timestamp, and Flux's ImagePolicy, which orders by timestamp, could pick
  either. A rebuild is a manual re-run. New system packages reach dev with a commit that
  changes the `system` stage's inputs (see the next decision). Alternative: let the newest rebuild win. That needs an order the tag can't carry
  without a build time, which brings back the problem this change removes.
- **System packages refresh when the `system` stage's inputs change.** The build cache keys
  the `apt-get` layer on the base image digest, the step's text and `TESSERACT_VERSION`. Any
  other commit, a `uv.lock` change included, reuses the cached layer, so its packages and
  hash stay the same. The refresh comes with Dependabot's weekly digest update of the base
  image: upstream rebuilds it for Debian security fixes, and a new digest makes the next
  build run `apt-get` again, with a new hash. An eviction of the cache entry has the same
  effect. Alternative: clear the cache, or bust it on a schedule. Not taken: that refreshes
  packages without a commit that says so, and Dependabot already brings the base image's
  fixes.
- **A release tag is published once.** The same lookup, on `vMAJOR.MINOR.PATCH`, makes a re-run
  of a release build push nothing. Without it a re-run overwrote the release (and moved the
  `vMAJOR.MINOR` tag) with whatever apt resolved that day. A release gets no hash: its DBOS
  version must be the packaged version, and a published release can't change, so new packages
  need a new patch release.
- **Use the hashed tag as the DBOS version.** One string names the image, the DBOS version and,
  later, the controller's version. Alternative: `<package>-dev.<timestamp>.<sha>`, which keeps
  the package version but is a second spelling of the same build. The package version of a dev
  build is one `git show <sha>:pyproject.toml` away.
- **Committer time orders `dev`.** GitHub stamps a merge commit, and each commit a rebase merge
  writes, with the merge time, so `dev`'s head always carries the newest stamp. A direct push
  carries the pusher's local commit time, which follows its parent's unless a clock is wrong.

## Twelve-Factor

- **V.1, distinct stages:** still conforms. The release identifier is now fixed by the build's
  inputs rather than by the build run.
- **V.2, immutable releases:** the build side conforms. Before, a re-run of a release build
  overwrote its tag, and a commit-time dev stamp alone would have let a rebuild republish a dev
  tag with other packages. Now a published tag is never pushed again, and a dev tag names its
  packages. The row stays a gap for its deployment side, unchanged: a change of config or of a
  secret restarts no process and gets no new release identifier.
- **II.1, deterministic build and run:** stays a gap, unchanged. `TESSERACT_VERSION` still
  floats; the hash makes that visible in the tag but does not fix it. The Dockerfile frontend,
  which also floated, is now pinned by digest.

## Risks / Trade-offs

- [A wrong clock on a direct push stamps a commit earlier than its parent] → Flux keeps the
  newer image; the next merge fixes the order. Changes reach `dev` by pull request.
- [A rebuild to take a security fix in a system package publishes nothing] → Intended: the
  commit keeps its image. Change the `system` stage's inputs to build with new packages:
  normally merge the pending Dependabot base image update; for an urgent fix, edit the
  `apt-get` step or `TESSERACT_VERSION`.
- [Two runs of one commit at the same moment both find the alias missing] → A concurrency
  group per ref and commit makes the second wait, so it finds the first's image published.
- [The `system` stage misses the builder's cache between the two builds] → The image build fails
  on the hash check and pushes nothing; a re-run lists the packages again.
- [The registry's "not found" wording changes] → The lookup fails the run rather than
  publishing; the step's message names the answer.
- [Dashboards or queries that matched `+dev`] → None in this repository; Logfire queries that
  filter `service.version` by prefix need `dev-` instead.

## Migration Plan

Merge into `dev`, and merge flux-ovh's ImagePolicy change. In either order: the old pattern
matches the commit alias, and the new one matches the hashed tag, both naming the same image.
The next dev build runs as `dev-<timestamp>-<sha>-<environment hash>`, and the recovery sweep
moves the previous build's leftover work onto it, as after any dev build.
