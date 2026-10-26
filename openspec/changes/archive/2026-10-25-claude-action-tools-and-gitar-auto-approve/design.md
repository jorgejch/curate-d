# Design

## Context

The Claude workflow runs `anthropics/claude-code-action` (pinned `ae68417a`) on an `@claude`
mention by a user with write access. In that mode Claude may read, edit and commit files, run
read-only git and use the action's GitHub MCP tools; any other tool must be named in
`--allowedTools`, and with no one to approve, an unnamed one is refused. Before this change it
named five `gh` commands.

How the action handles `claude_args` (read in `base-action/src/parse-sdk-options.ts` at the
pinned SHA): full-line `#` comments are stripped; every `--allowedTools` occurrence accumulates
and each value splits on commas; `--mcp-config` values are merged with the action's own inline
JSON, and **a value that is a file path is dropped** when inline JSON is present, which in tag
mode it always is. The action passes its whole environment (minus the OIDC request variables)
to Claude.

Claude Code expands `${VAR}` in an inline `--mcp-config`, and a stdio server inherits Claude's
environment; an unset variable is left literal (or empty, with `${VAR:-}`) and the session goes
on. Verified locally on 2026-10-25 with a stub server that recorded its environment.

Gitar's auto-approve (documentation, "Auto-approve", "Auto-merge", "Repository configuration",
read 2026-10-25): it runs only after its review, and a blocking finding (Important or above)
always prevents approval. It then judges criteria against the pull request's metadata, changed
files, review results and the Risk Triage assessment, never CI status, and runs nothing. The
organization's criteria and `.gitar/config/approve.md` both apply. Approval can arm GitHub's
native auto-merge, which waits for the ruleset (required checks, code scanning, review threads,
signatures). The docs say `merge.md` is read from the default branch and say nothing for
`approve.md`. The ruleset on `dev` requires 0 approving reviews, so an approval adds no gate;
what it adds is arming auto-merge.

## Goals / Non-Goals

**Goals:**
- Claude in GitHub verifies its own change before pushing and can consult the web, SonarCloud
  and Logfire, without gaining a tool that writes beyond the repository's working copy and the
  `gh` writes it already had.
- Auto-approval only for changes that can't alter what runs, and never for a bot's.

**Non-Goals:**
- Containing a prompt-injected Claude completely. The job already holds a write token and the
  Claude OAuth token; this change bounds what new tools can do, it does not sandbox the job.

## Decisions

- **One `--allowedTools` flag per group, with a comment above each.** The action accumulates
  repeated flags, so the list reads as groups (web, `gh`, git, checks, OpenSpec, each MCP
  server) instead of one 1,500-character line. Exact entries (`Bash(make test)`) where the
  command takes no arguments; prefix entries (`Bash(gh pr diff:*)`) only for read commands.
- **OpenSpec by named subcommand, not `Bash(openspec:*)`.** `validate`, `list`, `show`,
  `status`, `instructions`, `context`, `new change` and `archive` touch only the working copy,
  which Claude edits anyway, and are what the repository's `openspec-*` skills run. A blanket
  entry would also admit any subcommand a later CLI version adds that reaches the network.
- **MCP config inline in `claude_args`, secrets by `${VAR}`.** A file path would be dropped by
  the action (Context). Writing the tokens into the JSON by a prior step would put them in the
  CLI's arguments; GitHub also refuses to pass a secret through a step output. The tokens are the
  step's environment, and the JSON names them. Alternative rejected: a root `.mcp.json`, which
  the action would load but which every local Claude Code session would also load, duplicating
  the user-scope servers.
- **SonarQube server: `sonarsource/sonarqube-mcp:1.30.0.4763` by digest, `SONARQUBE_READ_ONLY`,
  read toolsets only, image pulled in a step.** Read-only mode is the server's own guard; the
  allow-list names only `search_`, `get_`, `show_` and `list_` tools as a second one. The pull
  step keeps the first tool call inside Claude Code's MCP start-up timeout. `docker run -e NAME`
  forwards the token from the server's environment, keeping it out of the command line.
- **Logfire: the hosted server (`logfire-us`, where the project lives) with a bearer API key.**
  The local `logfire-mcp` package is deprecated upstream and the hosted one's browser OAuth
  can't run headless. The key is scoped to `project:read` (and `project:read_alert` for issues),
  so the server exposes no write tool to it; the allow-list repeats the restriction.
- **`SONAR_TOKEN` is reused.** It is the only Sonar credential the repository holds. Read-only
  mode and the allow-list bound the server; the token's own rights (a user token) are wider, see
  Risks.
- **Auto-approve by positive list, not by exclusion.** The criteria name the three eligible kinds
  of change and refuse everything else, so a path nobody thought of is refused, not admitted.
  Runbooks (`docs/recovery.md`, `docs/rewind.md`, `docs/reset.md`) are excluded from the
  documentation category: an operator follows them during an incident, and a wrong step there
  can lose data. Documents that instruct agents or reviewers (`CLAUDE.md`, `CONTRIBUTING.md`,
  `.gitar/`, `.claude/`, `.agents/`, `openspec/`) are excluded too.
- **Bots refused by identity, at pull-request and commit level.** `@claude` can push to a human's
  pull request, so refusing only bot-opened pull requests would let a bot commit ride a human's
  approval. Co-author trailers are not identities: a human's commit written with Claude Code
  locally stays eligible, as the human ran and pushed it.
- **`.gitar/` changes are refused outright.** Since the docs don't say which branch `approve.md`
  is read from, refusing any pull request that touches it closes the self-authorization path
  either way.
- **Requirements in `release-pipeline`**, as for `apply.md`: the capability holds the
  repository's tooling contracts.

## Risks / Trade-offs

- [A prompt-injected Claude (a fetched page) writes a test that reads its environment, which now
  holds `SONAR_TOKEN` and the Logfire key, and leaks it through a comment] → The job is started
  only by users with write access; the Logfire key is read-only by scope. `SONAR_TOKEN` is a user
  token with that user's rights: replacing it in the Claude step with a dedicated token from a
  read-only SonarCloud member would narrow this, and is a one-line change once such a member
  exists.
- [Running `make test` executes pull-request code in a job holding `contents: write`] → Same
  trust boundary as before: the code is the triggering writer's or Claude's. The checks job keeps
  `persist-credentials: false`; the Claude job can't, since it pushes.
- [Gitar misjudges a criterion and approves an ineligible pull request] → Auto-merge still waits
  for every required check, code scanning and resolved review threads, and Gitar revokes the
  approval when a later push brings a blocking finding. The positive list keeps the misjudgement
  to "is this file prose", not "is this logic safe".
- [The allow-list drifts from what the skills need] → A refused command is reported in the
  run's comment; the allow-list is extended by a reviewed change.

## Migration Plan

Merge into `dev`. The Claude workflow uses the new configuration on the next mention; the
Logfire tools appear once `LOGFIRE_MCP_API_KEY` is created. Gitar judges approvals by the
criteria once auto-approve is turned on in its settings, together with auto-merge (method:
merge commit or rebase, the two the ruleset allows). Rollback: revert the commit.

## Parallelism

Two work streams on disjoint files, both able to start at once:

- **A**: `.github/workflows/claude.yml` and `tests/unit/infrastructure/test_claude_workflow.py`.
- **B**: `.gitar/config/approve.md` and `CONTRIBUTING.md`.

No shared edit: `.gitar/**` is already in the Sonar exclusions, and no settings field or
migration is involved.
