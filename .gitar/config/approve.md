# curate.d auto-approve criteria

Repository criteria for Gitar's auto-approve. An approval here can arm GitHub's auto-merge, and a
merge into `dev` publishes the dev image the running system pulls, so approve only what could not
change what runs: a change a human reviewer would pass without reading it twice. curate.d moves
people's files between clouds and may release the originals; anything that could touch that path
waits for a human. Project rules: `CLAUDE.md`, `CONTRIBUTING.md`, and the contracts in
`openspec/specs/`.

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
  `claude[bot]`, `dependabot[bot]` or `github-actions[bot]`, or any other automation account. A
  bot's change reaches a human before it merges.
- **Carries a bot's commit.** Any commit in it was authored or committed by such an account, for
  instance `@claude` pushing to a human's pull request. A `Co-Authored-By` trailer is not an
  account: a commit a human authored and pushed stays eligible.
- **Identity unknown.** The author of the pull request, or of any commit in it, can't be
  established.
- **From a fork**, or into any base but `dev`. `main` takes release merges only.
- **Draft.**
- **Risk above low**, or no risk assessment available.
- **Too large**: more than 10 changed files or 200 changed lines.
- **Deletes, renames or moves a file**, or adds or changes a binary file.
- **Any changed file outside the eligible changes below.**

## Eligible changes

Approve only when every changed file is one of these.

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

Everything else needs a human, including, at least:

- Executable code anywhere: `src/`, `migrations/`, `tests/local/`, scripts.
- CI, build and infrastructure: `.github/`, `docker/`, `infrastructure/`, `compose.yaml`,
  `compose.lifecycle.yaml`, `Makefile`, `alembic.ini`, `sonar-project.properties`.
- Dependencies: `pyproject.toml`, `uv.lock`, `.agents/skills/dbos-python` and `skills-lock.json`.
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

- On approval, name the eligible category each changed file falls under, and state that the
  author and every commit's author and committer are human accounts.
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

Refuse:

- `claude[bot]` opens a pull request that only edits `README.md`: written by a bot.
- A maintainer's docs pull request gains a commit from `claude[bot]` after a review comment asked
  `@claude` for a fix: carries a bot's commit.
- `dependabot[bot]` bumps a patch version: written by a bot, and a dependency.
- A docs fix also corrects a log message in `src/`: a log message is code.
- A step in `docs/recovery.md` is reworded: a runbook.
- A typo fix in `openspec/specs/`: a specification.
- An existing test's expected value changes, even with a new test beside it: changes an existing
  test.
- A pull request edits this file: changes `.gitar/`.
