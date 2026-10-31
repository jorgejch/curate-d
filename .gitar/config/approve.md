# curate.d auto-approve criteria

Repository criteria for Gitar's auto-approve. An approval here can arm GitHub's auto-merge, and a
merge into `dev` publishes the dev image the running system pulls, so approve only what could not
change what runs: a change a human reviewer would pass without reading it twice. curate.d moves
people's files between clouds and may release the originals; anything that could touch that path
waits for a human. The one exception is a narrow class of Dependabot version updates (category 4
below), whose compatibility the required checks alone judge. Project rules: `CLAUDE.md`,
`CONTRIBUTING.md`, and the contracts in `openspec/specs/`.

## Reach of this file

- **Both policies bind.** A pull request is approved only if it satisfies the organization
  criteria and this file. This file only narrows; it never makes eligible what the organization
  criteria exclude.
- **It can't authorize its own change.** Any pull request that adds, edits or deletes anything
  under `.gitar/` is refused, whichever copy of these criteria was read.
- **It enables nothing.** Auto-approve and auto-merge are turned on in Gitar's settings, and
  auto-merge also needs GitHub's "Allow auto-merge". Merging this file turns neither on.
- **Approval leaves every gate in place.** The required checks (`Checks`, `Integration tests`),
  code scanning, resolved review threads and signed commits still hold the merge. These criteria
  never refer to CI status: the ruleset enforces it.
- **A blocking finding refuses.** A review finding of severity Important or above prevents
  approval, whatever this file says.

## Never approve

Any one of these refuses the pull request:

- **Written by a bot.** It was opened by a bot or app account: a login ending in `[bot]`, such as
  `claude[bot]` or `github-actions[bot]`, or any other automation account. A bot's change reaches
  a human before it merges. The only exception is an eligible Dependabot version update
  (category 4).
- **Carries a bot's commit.** Any commit in it was authored or committed by such an account, for
  instance `@claude` pushing to a human's pull request. A `Co-Authored-By` trailer is not an
  account: a commit a human authored and pushed stays eligible. The only exception is a category 4
  pull request's own commits, as that category defines them.
- **Identity unknown.** The author of the pull request, or of any commit in it, can't be
  established.
- **From a fork**, or into any base but `dev`. `main` takes release merges only.
- **Draft.**
- **Risk above low**, or no risk assessment available.
- **Too large**: more than 10 changed files or 200 changed lines. Category 4 is exempt: its diff
  rules bound it instead, since a grouped `uv.lock` update passes 200 lines in wheel hashes alone.
- **Deletes, renames or moves a file**, or adds or changes a binary file.
- **Any changed file outside the eligible changes below.**

## Eligible changes

Approve only when every changed file falls under one of categories 1 to 3, or the whole pull
request is category 4. Category 4 never combines with the others.

1. **Documentation prose**: `README.md` and files under `docs/` (including `docs/diagrams/`),
   where the change is to wording, spelling, links or formatting. Not the runbooks an operator
   follows during an incident: `docs/recovery.md`, `docs/rewind.md`, `docs/reset.md`. A wrong step
   there can lose data.
2. **Comments and docstrings only**, in `src/` or `tests/`: the diff changes no statement,
   expression, signature, default, decorator, import, type annotation, or string other than a
   docstring. A log or error message is code. Docstrings follow `CLAUDE.md#docstrings`.
3. **Added unit tests**: new test functions under `tests/unit/` that add assertions without
   changing, loosening, skipping or deleting an existing test, and without touching a fixture,
   `conftest.py`, a fake or test configuration. A new test can fail CI; it can't change what runs.
4. **Dependabot version updates**: a pull request that meets every one of these conditions.
   - **Author.** `dependabot[bot]` opened it, from a branch in this repository.
   - **Commits.** `dependabot[bot]` authored every commit. Each commit's committer is
     `dependabot[bot]` or `web-flow`, the account GitHub records for the signed commits it
     creates for Dependabot, rebases included. A commit authored by any other account, human or
     bot, refuses: the diff is then no longer Dependabot's alone.
   - **Version update.** It carries the `gitar-managed` label. `.github/dependabot.yml` gives that
     label to version updates only; a security update ignores that file and carries no label, so
     it is refused.
   - **Update types.** The last commit's message ends in an `updated-dependencies:` block, and
     every entry in it is one of:
     - a `uv` dependency with `update-type: version-update:semver-patch`;
     - a `uv` dependency with `update-type: version-update:semver-minor`, whose old and new
       versions both have a major of `1` or more. SemVer lets a `0.y` minor break anything, so a
       minor of a `0.x` package (`fastapi`, `uvicorn`, `httpx`, `ruff`, among others) is refused.
       A patch of a `0.x` package stays eligible;
     - a `docker` image with `update-type: version-update:semver-patch`.

     The ecosystem is the one in the branch name (`dependabot/uv/...`, `dependabot/docker/...`).
     A `github-actions` update of any type is refused: a new action release runs in CI with the
     repository's secrets as soon as it merges. A `semver-major` update, a `docker` minor, an
     entry with no `update-type`, and a block that is missing or can't be read all refuse.
   - **Not `dbos`.** No entry names `dbos`. `CLAUDE.md` requires a `dbos` bump to refresh the
     vendored `.agents/skills/dbos-python` and its `skills-lock.json` pin in the same change, and
     Dependabot does neither.
   - **Version references only.** Every changed line is a version reference of a dependency the
     block names:
     - `uv.lock`: the named packages' versions, sources, sdists, wheels and hashes. A package added
       to or removed from the lock refuses; name it.
     - `pyproject.toml`: only the named dependencies' version specifiers, rewritten when the new
       version leaves the declared range (`sqlalchemy~=2.1.1` to `~=2.2.0`). A comment, another
       key or another dependency refuses.
     - `docker/Dockerfile`: only the tag and `@sha256:` digest of `FROM` lines. A digest that
       changes under the same tag is a digest-only refresh with no version to judge, and refuses.

   No other file may change. For a `uv` minor, the required checks are the only test of
   compatibility: say so in the approval.

Everything else needs a human, including, at least:

- Executable code anywhere: `src/`, `migrations/`, `tests/local/`, scripts.
- CI, build and infrastructure: `.github/`, `docker/`, `ops/`, `compose.yaml`,
  `compose.lifecycle.yaml`, `Makefile`, `alembic.ini`, `sonar-project.properties`.
- Dependencies: `pyproject.toml` and `uv.lock` outside category 4, `.agents/skills/dbos-python`
  and `skills-lock.json`. The `docker/Dockerfile` `FROM` lines are eligible only in category 4.
- Instructions to agents and reviewers: `CLAUDE.md` (and `AGENTS.md`, which links to it),
  `CONTRIBUTING.md`, `.claude/`, `.agents/`, `.gitar/`, `.github/pull_request_template.md`.
- Specifications and plans: anything under `openspec/`, even a typo. A spec is the contract code
  is judged against.
- Configuration and secrets: `.env*`, settings defaults.

## When uncertain

If any condition can't be established from the pull request's metadata, commits, changed files,
review results and risk assessment, don't approve. Say which condition was not established and
what would establish it. A pull request that is close to eligible is not eligible.

## Reporting

- On approval under categories 1 to 3, name the eligible category each changed file falls under,
  and state that the author and every commit's author and committer are human accounts.
- On approval under category 4, list each dependency with its old and new version and update
  type, and state that `dependabot[bot]` authored every commit and that no package was added to or
  removed from `uv.lock`. For a `uv` minor, state that no compatibility check other than the
  required checks applies.
- On refusal, name the first condition that refused it.
- Never claim a test, lint or check ran or passed; Gitar runs none. The ruleset decides merge
  readiness.

## Examples

Approve:

- A maintainer's pull request into `dev` fixes two typos and a broken link in `docs/staging.md`;
  the review has no blocking finding and the risk is low.
- A docstring in `src/curate_d/placement/verification.py` changes from `Returns the...` to
  `Return the...`, and nothing else in the file changes.
- A new test function in `tests/unit/` pins an edge case the spec names, using the existing
  fixtures unchanged.
- `dependabot[bot]` opens "bump the python-patches group with 4 updates", labelled
  `gitar-managed`. Its one commit, signed by `web-flow`, gives `semver-patch` for every entry, and
  only those four packages change in `uv.lock`. A patch of `fastapi` from `0.142.2` to `0.142.3`
  in that group is eligible too.
- `dependabot[bot]` bumps `sqlalchemy` from `2.1.1` to `2.2.0` as `semver-minor`, rewriting
  `~=2.1.1` to `~=2.2.0` in `pyproject.toml` and the package in `uv.lock`. Approve, and state
  that the required checks are the only compatibility test.
- `dependabot[bot]` bumps the `python` image from `3.12.15-slim-trixie` to
  `3.12.16-slim-trixie` as `semver-patch`, changing the tag and digest on each `FROM` line.

Refuse:

- `claude[bot]` opens a pull request that only edits `README.md`: written by a bot.
- A maintainer's docs pull request gains a commit from `claude[bot]` after a review comment asked
  `@claude` for a fix: carries a bot's commit.
- `dependabot[bot]` bumps an action from `v8.3.0` to `v8.3.1` in the
  `actions-patches-and-minors` group: a `github-actions` update.
- `dependabot[bot]` bumps `pydantic` from `2.13` to `3.0` as `semver-major`: a major update.
- `dependabot[bot]` bumps `fastapi` from `0.142.2` to `0.162.0` as `semver-minor`: a minor of a
  `0.x` package.
- `dependabot[bot]` bumps the `uv` image from `0.13.5` to `0.14.0`: a `docker` minor.
- `dependabot[bot]` changes a `FROM` digest under the same tag: a digest-only refresh.
- `dependabot[bot]` opens a security update, with no `gitar-managed` label: not a version update.
- A `python-patches` group includes a `dbos` patch: `dbos` needs its skill refreshed.
- A maintainer pushes a fix onto a Dependabot pull request: a commit not authored by
  `dependabot[bot]`.
- A `python-patches` update adds a new package to `uv.lock`: the lock gains a package.
- A docs fix also corrects a log message in `src/`: a log message is code.
- A step in `docs/recovery.md` is reworded: a runbook.
- A typo fix in `openspec/specs/`: a specification.
- An existing test's expected value changes, even with a new test beside it: changes an existing
  test.
- A pull request edits this file: changes `.gitar/`.
