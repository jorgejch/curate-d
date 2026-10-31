# Proposal

## Why

Dependabot opens version-update pull requests against `dev` every Friday, and each one waits for a
human even when it is a patch or minor bump that the required checks and the integration suite
already judge. `.gitar/config/approve.md` refuses every bot-written pull request, so Gitar's auto-approve,
and the auto-merge it arms, never reaches them. The lowest-risk updates can merge on the checks
alone, and a human then reviews only the updates that can change behaviour.

## What Changes

- `.gitar/config/approve.md` gains a fourth eligible category, **Dependabot version updates**,
  limited to:
  - `uv` patch updates (the `python-patches` group) and `uv` minor updates (one pull request
    each) of packages at `1.0` or above, with the required checks as the only compatibility test;
  - `docker` patch updates of a `FROM` image's tag (the `image-patches` group).
- Such a pull request is eligible only when Dependabot opened it and wrote every commit, it carries
  the `gitar-managed` label that only version updates get, its commit metadata names an allowed
  update type for every dependency, and its diff changes only the version references of the named
  dependencies.
- Still refused, so a human reviews them: `uv` majors, `uv` minors of a `0.x` package (SemVer
  lets those break), `docker` minors and majors, every
  `github-actions` update (a new action release runs in CI with the repository's secrets, and
  `.github/` stays human-only), digest-only image refreshes, security updates, any update of `dbos` (whose bump must
  refresh the vendored skill in the same change), a lock file that adds or removes a package, and
  any commit on the pull request that is not Dependabot's.
- The bot refusal stays for every other bot, and for a Dependabot pull request outside the new
  category. The other gates stay: a low risk assessment, no blocking finding, no `.gitar/` change,
  the required checks.
- `CONTRIBUTING.md`'s Gitar paragraph names the new category.
- `.gitar/config/apply.md` does not change: it governs auto-applied review fixes, not approval or
  merging.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `release-pipeline`: the auto-approve policy admits a fourth kind of change (eligible Dependabot
  version updates); the bot-written refusal gains that one exception and is renamed to say so;
  two new requirements state which Dependabot updates are eligible and which diff they may carry.

## Impact

- Files: `.gitar/config/approve.md`, `CONTRIBUTING.md`, `openspec/specs/release-pipeline/spec.md`
  (at sync).
- Behaviour: once Gitar's auto-approve and auto-merge are on, an eligible Dependabot pull request
  merges into `dev` after the required checks pass, and that merge publishes the dev image the
  running system pulls. No code, workflow or Dependabot configuration changes.
- Depends on the organization's Gitar criteria: if they exclude dependency changes from approval,
  this repository file cannot widen them, and nothing is approved until that policy changes.
