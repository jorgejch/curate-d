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
- The branch is **up to date with its base** (`dev`, or `main` for a release PR). The
  ruleset requires it, so a PR that is only behind its base, with no conflict, is still
  blocked: bring the base in (see Commits for rebase or merge) and push.

Both check names are required by the ruleset and pinned by tests; never rename them. The
SonarQube scan runs inside `Checks`. Its quality gate is not the only signal: check the
PR's open SonarQube issues too (see SonarQube issues).

## Before every push

Run the commands of CI's `Checks` job, from the PR head, and push only when all pass. They
don't cover the `Integration tests` job: see below, and treat validation as complete only
once that PR check is green too.

```bash
uv sync --locked --no-build
make lint          # ruff check, ruff format --check, workflow_audit
make typecheck     # mypy --strict
make test          # unit suite, coverage floor
```

`make format` fixes formatting and autofixable lint. Run it, then re-run `make lint`.

For integration tests, run the files the fix touches when Docker is available. `make
test-stack` starts only what the suite talks to, migrated, as `integration.yml` does; never
`make stack-up`, whose workers apply the persisted schedules and run them against the test
database.

```bash
make test-stack
uv run pytest tests/integration/test_<area>.py --no-cov -m integration --staging-backend=rustfs
```

Without `--staging-backend=rustfs`, staging tests also select OVH, which needs credentials
a session usually lacks. When the stack can't come up in the session, say so in the reply
or PR comment instead of claiming integration coverage, and let the PR's `Integration
tests` run be the proof.

For a CI failure, reproduce it locally first with the failing job's exact command, then show
it passing.

## Commits

- Conventional Commits, lower-case imperative summary, a scope where one helps:
  `fix(recovery): re-read the status before the cancel`, `test(sweep): pin the cap`.
- Each commit is one logical change. The commit's type comes from what it changes, not from
  the fact that a review asked for it: a renamed variable is `refactor:`, an added test is
  `test:`.
- To bring a branch up to date with its base `<base>` (the PR's base branch), prefer a
  rebase: in a shallow clone (`git rev-parse --is-shallow-repository` prints `true`, as in
  the `claude.yml` Action's checkout) run `git fetch --unshallow` first, then `git fetch
  origin <base>`, `git rebase origin/<base>`, re-run the pre-push checks on the rebased head,
  and `git push --force-with-lease`. Rebase only a branch you created and no one else has
  pushed to. On a branch someone else pushed to, merge `origin/<base>` in instead: never
  rewrite their history.
- Gitar counts as someone else: on a `gitar-managed` pull request it commits its fixes to the
  branch. Pull before you push. Before any force-push, `git fetch origin <branch>` and check
  `git log --format='%h %an %s' origin/<base>..origin/<branch>`; a commit you did not write
  there, Gitar's or anyone's, means merge, not rebase. `--force-with-lease` does not protect a
  commit that a fetch already brought in.
- The `claude.yml` Action never merges, rebases or force-pushes: its own system prompt
  forbids it, whatever tools it is granted. As that Action, don't attempt the update: say
  on the PR that it is behind its base and leave it to a Claude Code session or the owner.
- Resolve a conflict in `uv.lock` by regenerating it, never by hand: `uv lock` can't parse a
  file with conflict markers, so first `git checkout origin/<base> -- uv.lock`, then run
  `uv lock` and stage the result.

## Review comments

- **Small, local asks** (a rename, a docstring, an added test, a one-function refactor)
  that stay within the PR's change: implement, validate, push, then reply on the thread
  naming the commit SHA and resolve it. Resolve a thread only once its fix is pushed. The
  PR's OpenSpec change covers them; on a simple-fix PR, only an ask that keeps it a simple
  fix (a typo, a one-line bug fix, a doc correction) is small. Anything else goes through
  OpenSpec: reply proposing a change instead of pushing.
- **Docstring and comment nits** follow `CLAUDE.md`'s Docstrings section. A nit citing it
  correctly is a plainly correct nit: carry it in the next push that already changes the
  PR's files.
- **An ask that changes specified behaviour** must also update the affected requirement in
  the PR's delta spec (`openspec/changes/<name>/specs/`), in the same push, and pass
  `openspec validate <name> --strict`. The change directory is committed with the PR; sync it
  into `openspec/specs/` and archive the change in this PR or in a named follow-up PR. If the PR has no OpenSpec
  change (a simple fix) and the ask grows it past one, reply proposing an OpenSpec change
  instead of pushing.
- **Stay within the files the PR's change names.** No drive-by reformatting, import
  reordering or cleanup of unrelated code, even when a reviewer points at it: reply that it
  belongs in its own change.
- **Security findings** stay out of public discussion (`SECURITY.md`): don't add details,
  reproductions or a proposed exploit path to the thread. Reply in one line that it goes to
  the owner privately, raise it with them outside the PR (in your session, or through
  [private vulnerability reporting](https://github.com/jorgejch/curate-d/security/advisories/new)),
  and leave the thread open for them.
- **Leave these threads open for the repo owner** and put the question to them:
  data-safety questions (can this release an original, skip verification or alter
  a placement?), anything that departs from the `dbos-python` skill, and any request to use
  a library's private internals.
- **Comments that mention `@claude`** start the Claude Code GitHub Action (`claude.yml`),
  which may push to the same branch. If you are that Action run, the comment is yours:
  handle it. Otherwise don't push a parallel fix for it. Wait for the Action's run to
  finish, fetch the branch, and then handle whatever is still open.

## SonarQube issues

A passing quality gate can still leave issues open on the PR's new code, so check the
issues themselves after every `Checks` run of a push, yours or anyone's. Project key and
organization are in `sonar-project.properties`.

- **With the SonarQube MCP server** (when the session has it), use it, not the web API:
  `search_sonar_issues_in_projects` with `projectKeys` and `pullRequest` (the PR number) and
  `issueStatuses` `OPEN` and `CONFIRMED`; `search_security_hotspots` with `pullRequest` and
  status `TO_REVIEW`; `get_project_quality_gate_status`; and `show_rule` for a rule you
  don't know.
- **Without it** (the `claude.yml` Action, for one), read the public web API:

  ```bash
  curl -s "https://sonarcloud.io/api/issues/search?componentKeys=<projectKey>&pullRequest=<n>&issueStatuses=OPEN,CONFIRMED"
  curl -s "https://sonarcloud.io/api/hotspots/search?projectKey=<projectKey>&pullRequest=<n>&status=TO_REVIEW"
  ```

- **Handle each issue** as a review-bot finding: verify it against the code and the rule.
  A valid one is a small, local ask (Review comments): fix, validate, push, and name the
  rule and commit in the PR or in the session. A finding you judge invalid: say why to the
  owner, and leave its status alone; never mark an issue accepted or a false positive, or
  a hotspot safe, yourself.
- **A security issue or hotspot** follows Review comments' security rule: no details on
  the PR, the owner hears it privately.
- **Confirm a fix** through the same lookup after the push's `Checks` run reports: the issue
  must no longer be open. The analysis runs only in CI; local analysis
  (`sonar analyze agentic`) is not available for this organization.

## Watching the PR

When you steward a PR in a session, subscribe to its activity and handle each new review
comment as it arrives, by the rules in Review comments. Do not wait to be asked again.

- **Subscribe** when the session offers PR activity events. Otherwise poll every 2 to 3
  minutes (`gh api repos/<owner>/<repo>/pulls/<n>/comments`, `.../pulls/<n>/reviews` and
  `.../issues/<n>/comments`, or `gh pr view <n> --json state,reviews,comments`).
- **A new review comment** is an inline comment, a review body or a PR comment that someone
  else wrote after the last one you handled. Your own replies and commits are not new. A
  bot comment (Gitar, SonarQube, Copilot) counts.
- **Handle each one** as Review comments says: fix, validate, push and reply, or reply
  proposing a change, or leave the thread open for the owner. A comment that is a
  question for the owner or a security finding ends the automatic handling of that thread
  only; keep watching the others.
- **Stop** at the first of these:
  - the PR is closed or merged;
  - 10 minutes pass with no new review comment. Start the 10 minutes when you finish
    handling the last comment (the push and the reply), not when it arrived. A new comment
    restarts the count.
- **On stop**, post nothing new. Tell the user in the session why you stopped, and list any
  thread you left open and why.
- Do not stop while a push you made is still waiting for its `Checks` or `Integration tests`
  run to report: a failed run is also work for you. The 10 minutes count only after both
  runs are green, or after you reported a failure you cannot fix under What never gets you
  to green here.
- **After each `Checks` run**, check the SonarQube issues (see SonarQube issues). An open
  issue is a new review comment for these rules.

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
