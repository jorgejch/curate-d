---
name: steward
description: How to drive a curate.d pull request to mergeable when CI results, review comments, review threads or merge conflicts arrive on it. Use when acting on a PR activity event, a PR check-in, or a request to fix review comments or CI on a curate.d pull request.
---

# Stewarding a curate.d pull request

Repo conventions for acting on PR events. Everything in [`CLAUDE.md`](../../../CLAUDE.md) and
[`CONTRIBUTING.md`](../../../CONTRIBUTING.md) still applies to the code you push; this file
adds only how to handle the PR itself.

## The merge gate

A PR into `dev` (or `main`, for a release) merges when all of these hold:

- **`Checks`** is green: ruff lint and format check, the workflow database-access audit,
  `mypy --strict`, and the unit suite with its 80% coverage floor (`checks.yml`).
- **`Integration tests`** is green: the full integration suite against the whole local
  stack (`integration.yml`).
- The branch is **up to date with its base**. The ruleset requires it, so a PR that is only
  behind `dev`, with no conflict, is still blocked: bring `origin/dev` in (see Commits for
  rebase or merge) and push.

Both check names are required by the ruleset and pinned by tests; never rename them. The
SonarQube scan runs inside `Checks`. Treat its quality-gate findings as review-bot
findings to verify, not as orders.

## Before every push

Run the same commands CI runs, from the PR head, and push only when all pass:

```bash
uv sync --locked
make lint          # ruff check, ruff format --check, workflow_audit
make typecheck     # mypy --strict
make test          # unit suite, coverage floor
```

`make format` fixes formatting and autofixable lint. Run it, then re-run `make lint`.

For integration tests, run the files the fix touches when Docker and the stack are available
(`make stack-up`, then `uv run pytest tests/integration/test_<area>.py --no-cov -m
integration`). When the stack can't come up in the session, say so in the reply or PR
comment instead of claiming integration coverage, and let the PR's `Integration tests` run
be the proof.

For a CI failure, reproduce it locally first with the failing job's exact command, then show
it passing.

## Commits

- Conventional Commits, lower-case imperative summary, a scope where one helps:
  `fix(recovery): re-read the status before the cancel`, `test(sweep): pin the cap`.
- Each commit is one logical change. The commit's type comes from what it changes, not from
  the fact that a review asked for it: a renamed variable is `refactor:`, an added test is
  `test:`.
- To bring a branch up to date with `dev`, prefer a rebase: `git fetch origin dev`, `git
  rebase origin/dev`, re-run the pre-push checks on the rebased head, then `git push
  --force-with-lease`. Rebase only a branch you created and no one else has pushed to. On a
  branch someone else pushed to, merge `origin/dev` in instead: never rewrite their history.
- Resolve a conflict in `uv.lock` by regenerating it with `uv lock`, never by hand.

## Review comments

- **Small, local asks** (a rename, a docstring, an added test, a one-function refactor):
  implement, validate, push, then reply on the thread naming the commit SHA and resolve it.
  Resolve a thread only once its fix is pushed.
- **Docstring and comment nits** follow `CLAUDE.md`'s Docstrings section. A nit citing it
  correctly is a plainly correct nit: carry it in the next push that already changes the
  PR's files.
- **An ask that changes specified behaviour** must also update the change's delta specs under
  `openspec/changes/<name>/specs/`, in the same push. If the PR has no OpenSpec change (a
  simple fix) and the ask grows it past one, reply proposing an OpenSpec change instead of
  pushing.
- **Stay within the files the PR's change names.** No drive-by reformatting, import
  reordering or cleanup of unrelated code, even when a reviewer points at it: reply that it
  belongs in its own change.
- **Leave these threads open for the repo owner** and put the question to them: security
  findings, data-safety questions (can this release an original, skip verification or alter
  a placement?), anything that departs from the `dbos-python` skill, and any request to use
  a library's private internals.
- **Comments that mention `@claude`** start the Claude Code GitHub Action (`claude.yml`),
  which may push to the same branch. Don't push a parallel fix for such a comment. Wait for
  the Action's run to finish, fetch the branch, and then handle whatever is still open.

## What never gets you to green here

Each of these is a reason to stop and report, never a fix:

- loosening, deleting or skipping an assertion, or lowering the coverage floor in
  `pyproject.toml`;
- adding an exception to ruff `banned-api` (`TID251`), `test_no_library_internals.py`,
  `test_isolation.py` or the `workflow_audit` to get a check past a violation;
- moving a ledger write inside a workflow out of the ledger datasource
  (`runtime/datasource.py`);
- replacing a deterministic workflow ID with a random one to dodge a collision;
- renaming the `Checks` or `Integration tests` jobs or changing their triggers.

## DBOS and data safety

Before fixing anything that touches a workflow, step, queue, schedule, datasource or
`DBOSClient` call, load the `dbos-python` skill and re-read the DBOS section of `CLAUDE.md`.
A fix that adds a departure records it there in the same push.

When a fix touches transfers, placements, retries or the release of originals, update the
PR description's **Data safety** section in the same round, saying what keeps the change
safe on a crash, retry and replay.

## The PR description

The description follows `.github/pull_request_template.md`. When a push changes what was
verified, update **How it was verified** with the commands run and their outcomes, and tick
only the checklist items that are true.
