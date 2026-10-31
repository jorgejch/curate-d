# Proposal

## Why

The `gitar-managed` label turns on Gitar's auto-apply for a pull request: Gitar commits fixes for
review findings and CI failures to its branch. Gitar's documentation says the label does nothing
else: "Auto-Approve and Auto-Merge have separate settings. The managed label only enables
automatic fixes" (docs.gitar.ai/labels). Today the label is on the wrong pull requests.

- Every Dependabot pull request carries it (`.github/dependabot.yml`, added in #129). There
  auto-apply has nothing to fix: `apply.md` excludes dependency pins and `docker/`. And any commit
  Gitar pushes is authored by Gitar, so `approve.md`'s category 4 refuses the update (the Commits
  condition), and Dependabot stops managing its branch.
- The owner's own pull requests do not carry it, so auto-apply on them needs a
  `gitar auto-apply:on` comment each time.

## What Changes

- `.github/dependabot.yml` no longer sets `labels: [gitar-managed]` on the `uv`, `github-actions`
  and `docker` entries. Dependabot falls back to its default labels (`dependencies` and the
  ecosystem label). Its header comment says why.
- A new GitHub Actions workflow labels a pull request `gitar-managed` when it is opened, if the
  repository owner opened it from a branch in this repository. A `Co-Authored-By` trailer from a
  bot (Claude) in its commits does not matter: the pull request's author decides.
- `CONTRIBUTING.md`'s Gitar paragraph says which pull requests get the label.
- `.gitar/config/apply.md` and `.gitar/config/approve.md` do not change. Neither refers to the
  label.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `release-pipeline`: the Dependabot requirement's security-update scenario no longer says that
  security updates carry the entry's labels; two requirements are added: Dependabot pull requests
  carry no `gitar-managed` label, and the owner's pull requests get it when they are opened.

## Impact

- `.github/dependabot.yml` (labels removed, header comment).
- `.github/workflows/label-owner-prs.yml` (new). It uses no third-party action and no secret, only
  `GITHUB_TOKEN` with `pull-requests: write`. It is not a required check.
- `CONTRIBUTING.md` (Gitar paragraph; a pull-before-push and force-push rule) and the steward
  skill's Commits section (the same rule).
- No tests: the label exists only for Gitar, an external service, and the owner's rule is not to
  pin such configuration in tests. Verification is `actionlint`, a YAML read and an observed pull
  request after the merge.
- Open Dependabot pull requests keep the label they already carry; only new ones change.
