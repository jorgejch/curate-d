# Design

## Context

See `proposal.md`. The dev tag is set in `.github/workflows/docker-publish.yml` with
`docker/metadata-action`'s `{{date 'YYYYMMDDHHmmss'}}`, the time the action runs. The image is
built with the tag as `CURATE_D_BUILD_REVISION`, and `dbos_app.application_version` turns it into
the DBOS version. Nothing in curate.d orders version strings: DBOS's latest version is the one
registered last, and the sweep, promotion and schedule ownership compare versions only for
equality or membership.

## Goals / Non-Goals

**Goals:**
- One commit, one dev tag, one DBOS version, however many times it is built.
- Dev tags still sort, as text, in the order commits reached `dev`.
- A dev DBOS version is a valid Kubernetes label value and fits the version controller's
  `spec.version` pattern.

**Non-Goals:**
- Adopting the version controller; this only removes one of the gaps its plan lists.
- Release versions, which stay the packaged version.

## Decisions

- **Stamp with the commit's committer time, read with `git log`.** Alternatives:
  `metadata-action`'s `{{commit_date}}` (it falls back to the current time when it can't read
  the commit date, which would bring back build-time tags without a sign); a zero-padded
  first-parent commit count (stable too, but it says nothing a person can read, and it changes
  if `dev`'s history is ever rewritten). The checkout already fetches full history, and the step
  fails unless it reads 14 digits.
- **Use the tag as the DBOS version.** One string names the image, the DBOS version and, later,
  the controller's version. Alternative: `<package>-dev.<timestamp>.<sha>`, which keeps the
  package version but is a second spelling of the same build. The package version of a dev
  build is one `git show <sha>:pyproject.toml` away.
- **Committer time orders `dev`.** GitHub stamps a merge commit, and each commit a rebase merge
  writes, with the merge time, so `dev`'s head always carries the newest stamp. A direct push
  carries the pusher's local commit time, which follows its parent's unless a clock is wrong.

## Twelve-Factor

- **V.1, distinct stages:** still conforms. The release identifier is now fixed by the commit
  rather than by the build run.
- **II.1, deterministic build and run:** stays a gap, and it now has one more consequence: a
  rebuild of a commit republishes the same tag, and with `TESSERACT_VERSION=5.5.*` floating the
  rebuilt image can differ from the first. Rebuilds of a dev commit are rare, and the row's fix
  (pinning Tesseract) closes this too.

## Risks / Trade-offs

- [A wrong clock on a direct push stamps a commit earlier than its parent] → Flux keeps the
  newer image; the next merge fixes the order. Changes reach `dev` by pull request.
- [A rebuild overwrites a tag with a different image (II.1)] → Same code, same version; nodes
  that cached the first image keep it until it is pulled again.
- [Dashboards or queries that matched `+dev`] → None in this repository; Logfire queries that
  filter `service.version` by prefix need `dev-` instead.

## Migration Plan

Merge into `dev`. The next dev build runs as `dev-<timestamp>-<sha>`, and the recovery sweep
moves the previous build's leftover work onto it, as after any dev build.
