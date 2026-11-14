# Proposal

## Why

The first Dependabot run under the current auto-approve criteria (2026-11-13) armed auto-merge
once in three eligible-looking pull requests. Gitar approved #163 (two `docker` image patches) and
withheld #164 and #165, which the owner then merged by hand. Both verdicts read "Rules: No rules
evaluated", but that line is Gitar's separate Rules feature, which this repository doesn't use,
and #163 shows it too. The withheld reasons, from Gitar's verbose comments:

- #164 (`anthropics/claude-code-action` `1.0.268` to `1.0.271`, one pinned commit in
  `.github/workflows/claude.yml`): "the criteria refuse any github-actions update of any type".
- #165 (`python-patches`: `fastapi`, `ruff`, `hypothesis`, `pyrefly`, all patches): "the fastapi
  0.162.4 lock entry adds an `anyio` dependency edge, which is not a version reference of an
  updated package, so the 'Version references only' condition of category 4 is not met." `anyio`
  was already locked; the lock gained no package.

The owner's own pull requests that touched no code fared no better. #156 (`.gitignore`, medium
risk) and #161 (`.editorconfig`, low risk, 364 lines) fell outside every category. #158 (`README.md`,
low risk) carried a fix commit from Gitar itself and a commit from GitHub's web editor, whose
committer is GitHub's `web-flow` account; Gitar's compact verdict gave no reason, so which of the two
refused it is inferred. Gitar rates most non-trivial pull requests medium (#149, #153, #156, #160),
so the low-risk-only rule for categories 1 to 3 refuses ordinary prose. The owner wants the
criteria looser where an approval still can't change what runs.

## What Changes

- **Lock-file dependency edges.** In category 4, an updated package's own `dependencies`
  (and `optional-dependencies`, `dev-dependencies`) list in `uv.lock` may change, as long as every
  name it lists is a package locked before and after. Adding or removing a package still refuses.
- **GitHub Actions patches and minors.** Category 4 admits a `github-actions` patch or minor when
  every changed line is a `uses:` line whose action stays the same and whose ref moves from one
  full 40-character commit SHA to another, with at most the trailing version comment changing to
  the new version. A tag ref (`@v7`), a major, a `0.x` minor or any other workflow change still
  refuses.
- **Cooldown for actions.** `.github/dependabot.yml` sets a seven-day `cooldown` on the
  `github-actions` entry, so a new action release waits a week before Dependabot proposes it and,
  now, before it can merge unread. A unit test pins it.
- **Medium risk for categories 1 to 3.** Documentation prose, comment- and docstring-only changes
  and added unit tests are approved at low or medium risk. High or no rating still refuses.
- **Larger size cap for categories 1 to 3**: 20 changed files or 500 changed lines, up from 10
  and 200.
- **New category: editor and Git hygiene**: `.editorconfig` and `.gitignore`. Neither reaches the
  image, CI or the running system. `.dockerignore` and `.gitattributes` stay human.
- **Web edits are the human's.** A commit a human authored in GitHub's web editor has GitHub's
  `web-flow` account as committer; that commit counts as the human's. Every other bot or app
  commit, Gitar's own fix commits included, still refuses.

Unchanged and still human-only: `CLAUDE.md`/`AGENTS.md`, `CONTRIBUTING.md`, `.claude/`,
`.agents/`, `.gitar/`, anything under `openspec/`, executable code, workflow changes other than an
action's pinned commit, majors, `0.x` minors of `uv` packages and actions, `docker` minors,
`dbos`, ignored updates, digest-only refreshes, the incident runbooks, high or missing risk, and
any pull request a bot other than Dependabot opened. An OpenSpec-archive category was considered
and left out: archives now land in the change's own pull request, so it would never apply.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `release-pipeline`: the auto-approve requirements admit medium risk and the hygiene files for
  human changes, `github-actions` patches and minors and lock-file dependency edges for Dependabot,
  web-editor commits as human; a new requirement holds `github-actions` version updates to a
  cooldown.

## Impact

- Files: `.gitar/config/approve.md`, `.github/dependabot.yml`, `CONTRIBUTING.md`,
  `tests/unit/infrastructure/test_dependabot_config.py`, `openspec/specs/release-pipeline/spec.md`.
- Behaviour: Dependabot pull requests shaped like #164 and #165, and human docs, comment, test and
  hygiene pull requests at medium risk, can be auto-approved, which arms auto-merge with Gitar's
  dashboard settings as they are. The required checks, code scanning, resolved threads and signed
  commits still gate every merge. No effect on placements or originals.
- Trade-off: an action patch or minor now runs in CI with the repository's secrets without a human
  reading it first. The pin to a commit SHA, the cooldown, and refusing tag refs and majors bound
  that; the owner decides whether it's worth it.
- This pull request changes `.gitar/`, so Gitar refuses it and a human merges it.
