# Proposal

## Why

`@claude` in GitHub edits code and pushes it blind. It can't run the checks it is expected to
pass, read the web, see why SonarCloud blocks a pull request or query the telemetry a bug report
points at, so it guesses and waits a CI round trip to find out. Separately, every pull request
waits on a human to press merge, even a typo fix in `docs/`. Gitar can approve and arm GitHub's
auto-merge, but with no repository criteria it would approve anything its review finds no
blocking issue in, including a bot's change to a workflow.

## What Changes

- The Claude workflow (`.github/workflows/claude.yml`) grants more tools, still by an explicit
  allow-list (anything unlisted is refused, as no one is there to approve it):
  - `WebSearch` and `WebFetch`.
  - `make lint`, `make typecheck`, `make test`, `make format` and `uv run pytest`, with the
    locked dependencies installed as the checks job installs them. The system prompt asks
    Claude to run the checks before pushing and say when one could not be run.
  - Read-only `gh` (`issue list`, `pr diff`, `pr checks`, `run view`, `search`) and git history
    (`log`, `blame`, `show`), with a full-history checkout.
  - The OpenSpec CLI's validation and planning commands, with the CLI installed at the version
    the repository uses.
  - Two MCP servers, read-only: SonarQube Cloud (the server's read-only mode, a pinned image,
    the issue, quality-gate, rule, measure, coverage, duplication and hotspot read tools) and
    Logfire (the hosted server, authenticated by an API key, the query, project and issue read
    tools). A server whose secret is unset is unavailable for the run; the run goes on.
  - Never allowed: unrestricted `Bash`, `gh api`, `gh pr merge`.
- Add `.gitar/config/approve.md`, beside `apply.md`: repository auto-approve
  criteria that admit only low-risk pull requests into `dev` (documentation prose, comment- and
  docstring-only code changes, added unit tests) and refuse everything else. A pull request
  opened by a bot or app account, or carrying a commit authored or committed by one (`@claude`,
  Dependabot), is never approved, so a bot's change always reaches a human before it merges. A
  pull request touching `.gitar/` is never approved, so the criteria can't authorize their own
  change.
- `CONTRIBUTING.md` points at the new file beside its `apply.md` paragraph.
- A unit test pins the Claude workflow's allow-list against the never-allowed entries and its
  MCP servers to read-only.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `release-pipeline`: adds requirements that the Claude workflow grants tools by an explicit
  allow-list that never includes unrestricted shell, `gh api` or merging; that it can run the
  repository's lint, type check and unit suite; that its MCP servers are read-only and a missing
  secret disables only that server; and that a repository auto-approve policy admits only
  low-risk pull requests into `dev`, never one written by a bot, and never one that edits the
  criteria.

## Non-goals

- Turning on Gitar's auto-approve or auto-merge, or adding `merge.md` or `risk.md`. Those are
  Gitar settings and separate files; this change writes the criteria approval is judged by.
- Requiring a human approval in the ruleset. `required_approving_review_count` stays 0; the
  auto-approve criteria are what keep a bot's pull request from merging unread, and a human
  merging by hand is unchanged.
- Signed commits from `@claude` (`use_commit_signing`). The ruleset requires signatures, so an
  unsigned `@claude` commit already can't merge; that is unchanged here.
- Running the integration suite from the Claude workflow. It needs the local stack, and
  `integration.yml` runs it on the pull request.

## Impact

- `.github/workflows/claude.yml`: checkout depth, three setup steps (uv and locked
  dependencies, Node and OpenSpec, the SonarQube MCP image pull), the step's environment, and
  `claude_args`.
- New: `.gitar/config/approve.md`, `tests/unit/infrastructure/test_claude_workflow.py`.
- `CONTRIBUTING.md`: the Gitar paragraph names the new file.
- Secrets: `SONAR_TOKEN` (existing) now also reaches the Claude job. `LOGFIRE_MCP_API_KEY` is
  new and must be created by hand: a Logfire API key with `project:read` (and `project:read_alert`
  for the issue tools) only. Until it exists the Logfire tools are unavailable.
- No application code, migration, or required check changes.
