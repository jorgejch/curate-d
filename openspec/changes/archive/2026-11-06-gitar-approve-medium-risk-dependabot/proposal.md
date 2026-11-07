# Proposal

## Why

Gitar has never auto-approved a Dependabot pull request under `.gitar/config/approve.md`, and it
cannot under the current text. Category 4 asks for two things Gitar does not have: a low risk
rating, when Gitar rates dependency bumps medium (#124 to #127, #134, #141), and commit metadata,
which its review does not show. #141, a one-line `astral-sh/uv` image patch with every required
check green, was withheld for both reasons, and #134 for the same two.

## What Changes

- Category 4 accepts a low or a medium risk rating. A high rating, or no rating, still refuses.
  Categories 1 to 3 stay at low.
- Category 4's commit condition becomes what the review shows: a commit by another account that
  the review shows refuses, and commit data the review does not show no longer refuses. The
  pull request's author stays required: `dependabot[bot]`.
- Category 4 stops depending on the `updated-dependencies` block, which Gitar can't read. The
  diff names the dependencies and gives their old and new versions. A title or description that
  disagrees with the diff refuses.
- `Reporting` and `Examples` in `approve.md` follow: an approval no longer states who authored
  every commit, since Gitar can't see that.
- `CONTRIBUTING.md`'s Gitar paragraph and `.github/dependabot.yml`'s header comment match.
- Every other category 4 limit stays: no `github-actions` update, no major, no `0.x` minor, no
  `docker` minor, no digest-only refresh, no `dbos`, nothing `dependabot.yml` ignores, version
  references only, no package added to or removed from `uv.lock`.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `release-pipeline`: the auto-approve policy admits category 4 at medium risk, and judges a
  Dependabot update from the pull request's author, its diff and the commits the review shows.

## Impact

- Files: `.gitar/config/approve.md`, `CONTRIBUTING.md`, `.github/dependabot.yml` (comment only),
  `openspec/specs/release-pipeline/spec.md` (at sync).
- Behaviour: an eligible Dependabot pull request that Gitar rates medium merges into `dev` on the
  required checks alone, and that merge publishes the dev image the running system pulls. This
  reaches `uv` patches, `uv` minors at `1.0` or above and `docker` patches, not only the image
  patches of #141.
- Depends on Gitar's dashboard, which this change can't read or set: organization criteria that
  also require low risk would still refuse, since both policies bind, and auto-merge must be on
  with a merge method the `dev` ruleset allows (merge or rebase, not squash).
- This pull request changes `.gitar/`, so Gitar refuses it and a human merges it.
