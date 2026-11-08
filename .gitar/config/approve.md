# curate.d auto-approve criteria

Repository criteria for Gitar's auto-approve. An approval here can arm GitHub's auto-merge, and a
merge into `dev` publishes the dev image the running system pulls, so approve only what could not
change what runs: a change a human reviewer would pass without reading it twice. curate.d moves
people's files between clouds and may release the originals; anything that could touch that path
waits for a human. The one exception is a narrow class of Dependabot updates (category 4 below),
whose compatibility the required checks alone judge. Project rules: `CLAUDE.md`,
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
  a human before it merges. The only exception is an eligible Dependabot update (category 4).
- **Carries a bot's commit.** Any commit in it was authored or committed by such an account, for
  instance `@claude` pushing to a human's pull request. A `Co-Authored-By` trailer is not an
  account: a commit a human authored and pushed stays eligible. The only exception is a category 4
  pull request's own commits, as that category defines them.
- **Identity unknown.** The author of the pull request, or of any commit in it, can't be
  established. Category 4 needs only the pull request's author: its **Commits** condition states
  the rule for commit data.
- **From a fork**, or into any base but `dev`. `main` takes release merges only.
- **Draft.**
- **Risk above low**, or no risk assessment available. Category 4 alone also accepts medium:
  a version change's impact is uncertain, which Gitar rates medium, and the required checks judge
  it. A high rating or no rating refuses category 4 too.
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
4. **Dependabot updates**: a version or a security update that meets every one of these
   conditions. The two kinds follow the same conditions.
   - **Author.** `dependabot[bot]` opened it, from a branch in this repository.
   - **Commits.** Gitar's review does not show commit authors, committers or messages, and that
     absence refuses nothing here. A commit the review does show as authored by another account,
     human or bot, refuses: the diff is then no longer Dependabot's alone.
   - **Update types.** The diff names the dependencies: every package whose `version` changes in
     `uv.lock` and every image whose `FROM` tag changes. Judge each one's update type from its old
     and new versions there. Compare the versions' first three numeric parts: the same major and
     minor is a patch, the same major and a higher minor is a minor, anything else is a major. A
     title or description that names a dependency or a version the diff contradicts refuses; one
     that is missing or truncated does not. Every dependency is one of:
     - a `uv` patch, direct or indirect;
     - a `uv` minor whose old and new versions both have a major of `1` or more. SemVer lets a
       `0.y` minor break anything, so a minor of a `0.x` package (`fastapi`, `uvicorn`, `httpx`,
       `ruff`, among others) is refused. A patch of a `0.x` package stays eligible;
     - a `docker` image patch.

     The ecosystem is the one in the branch name (`dependabot/uv/...`, `dependabot/docker/...`).
     A `github-actions` update of any type is refused: a new action release runs in CI with the
     repository's secrets as soon as it merges. A major, a `docker` minor and a version whose parts
     can't be compared all refuse.
   - **Not `dbos`.** The diff changes no `dbos` version. `CLAUDE.md` requires a `dbos` bump to
     refresh the vendored `.agents/skills/dbos-python` and its `skills-lock.json` pin in the same
     change, and Dependabot does neither.
   - **Not ignored.** No dependency's update is one of these, the `ignore` rules of
     `.github/dependabot.yml`, copied here because Gitar's review does not read that file:
     - `uv` `aioboto3`: minor.
     - `docker` `python`: minor and major.

     Judge this condition from this list alone, with the update type the **Update types**
     condition gives: a dependency or an update type the list does not name is not ignored, and
     an unread `dependabot.yml` refuses nothing. A unit test keeps the list equal to the file's
     rules. GitHub's documentation does not say that an `ignore` rule's `update-types` also holds
     back a security update, so this file holds the exclusion: such an update waits for a human.
   - **Version references only.** Every changed line is a version reference of a dependency the
     update changes:
     - `uv.lock`: those packages' versions, sources, sdists, wheels and hashes, and the
       root project's copy of each one's specifier (under `[package.metadata]`
       `requires-dist` or `[package.metadata.requires-dev]`) when it matches the
       `pyproject.toml` rewrite. A package added to or removed from the lock refuses; name it.
     - `pyproject.toml`: only those dependencies' version specifiers, rewritten when the new
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
review results and risk assessment, don't approve; commit data under category 4 is the one
exception, as its **Commits** condition states. Every fact a condition needs from a file the pull
request doesn't change is stated in this file, so judge such a condition from this file and never
withhold approval because the other file could not be read. Say which condition was not established and
what would establish it. A pull request that is close to eligible is not eligible.

## Reporting

- On approval under categories 1 to 3, name the eligible category each changed file falls under,
  and state that the author and every commit's author and committer are human accounts.
- On approval under category 4, list each dependency with its old and new version and the update
  type those versions give, state the risk rating, and state that no package was added to or
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
- `dependabot[bot]` opens "bump the python-patches group with 4 updates". The `uv.lock` diff moves
  each of the four by a patch and changes no other package, and Gitar rates it medium. A patch of
  `fastapi` from `0.142.2` to `0.142.3` in that group is eligible too.
- `dependabot[bot]` bumps `sqlalchemy` from `2.1.1` to `2.2.0`, rewriting
  `~=2.1.1` to `~=2.2.0` in `pyproject.toml` and in `uv.lock`'s `requires-dist`, and the package
  in `uv.lock`. Approve, and state that the required checks are the only compatibility test.
- `dependabot[bot]` bumps the `python` image from `3.12.15-slim-trixie` to
  `3.12.16-slim-trixie`, changing the tag and digest on each `FROM` line.
- `dependabot[bot]` bumps the `astral-sh/uv` image from `0.13.6` to `0.13.7`, changing one `FROM`
  line's tag and digest. Gitar rates it medium, and its review shows no commit data. The ignored
  list above doesn't name `astral-sh/uv`, so it is not ignored, whether or not
  `.github/dependabot.yml` could be read.
- `dependabot[bot]` bumps `aioboto3` from `15.5.0` to `15.5.1` in `python-patches`: the list
  ignores only `aioboto3` minors, so a patch is not ignored.
- `dependabot[bot]` opens a security update that moves `pypdf` from `6.21.0` to `6.21.1` and the
  indirect `certifi` from `2026.10.5` to `2026.10.19`. The `uv.lock` versions make both patches.

Refuse:

- `claude[bot]` opens a pull request that only edits `README.md`: written by a bot.
- A maintainer's docs pull request gains a commit from `claude[bot]` after a review comment asked
  `@claude` for a fix: carries a bot's commit.
- `dependabot[bot]` bumps an action from `v8.3.0` to `v8.3.1` in the
  `actions-patches-and-minors` group: a `github-actions` update.
- `dependabot[bot]` bumps `pydantic` from `2.13` to `3.0`: a major update.
- `dependabot[bot]` bumps `fastapi` from `0.142.2` to `0.162.0`: a minor of a `0.x` package.
- An otherwise eligible Dependabot update that Gitar rates high: risk above medium.
- A maintainer's docs pull request that Gitar rates medium: only category 4 accepts medium.
- A Dependabot pull request's description says `pypdf` moves to `6.21.1`, and the diff moves it to
  `6.22.0`: the description contradicts the diff.
- `dependabot[bot]` bumps the `uv` image from `0.13.5` to `0.14.0`: a `docker` minor.
- `dependabot[bot]` changes a `FROM` digest under the same tag: a digest-only refresh.
- `dependabot[bot]` opens a security update that moves `pillow` from `12.3.0` to `13.0.0`: the
  `uv.lock` versions make it a major.
- A `python-patches` group includes a `dbos` patch: `dbos` needs its skill refreshed.
- `dependabot[bot]` opens a security update that moves `aioboto3` from `15.5.0` to `15.6.0`:
  the ignored list names `aioboto3` minors, which wait for re-verification against RustFS and OVH.
- The review shows a maintainer's commit on a Dependabot pull request: a commit not authored by
  `dependabot[bot]`.
- A `python-patches` update adds a new package to `uv.lock`: the lock gains a package.
- A docs fix also corrects a log message in `src/`: a log message is code.
- A step in `docs/recovery.md` is reworded: a runbook.
- A typo fix in `openspec/specs/`: a specification.
- An existing test's expected value changes, even with a new test beside it: changes an existing
  test.
- A pull request edits this file: changes `.gitar/`.
