# Tasks

Two work streams on disjoint files that can start at once (design.md, Parallelism): stream A is
group 1, stream B is group 2. Group 3 runs after both.

## 1. Claude workflow tools (stream A)

- [x] 1.1 Add `tests/unit/infrastructure/test_claude_workflow.py`: it parses `claude_args` as the action does (full-line comments stripped, every `--allowedTools` accumulated and split on commas, the inline `--mcp-config` JSON) and fails on unrestricted `Bash`, `gh api` or `gh pr merge`, on a non-read SonarQube or Logfire tool, on the SonarQube server losing read-only mode or its image digest, and on the dependency install no longer running `uv sync --locked --no-build`; verify it fails against the current workflow (no MCP servers, no install)
- [x] 1.2 In `.github/workflows/claude.yml`: full-history checkout; install uv and the locked dependencies as `checks.yml` does; install Node and `@fission-ai/openspec@1.14.1` with scripts disabled; pull the pinned SonarQube MCP image; pass `SONAR_TOKEN` and `LOGFIRE_MCP_API_KEY` to the Claude step's environment; group `--allowedTools` (web, `gh`, git, checks, OpenSpec, SonarQube, Logfire) and add the inline `--mcp-config`; ask Claude in the system prompt to run the checks before pushing. Verify `uv run pytest tests/unit/infrastructure/test_claude_workflow.py` passes and the YAML parses
- [x] 1.3 Verify the inline MCP config starts both servers' commands as written: `claude -p` locally with the workflow's `--mcp-config` value and stub secrets connects to the SonarQube container (a token error from SonarCloud counts as started) and leaves the run going when `LOGFIRE_MCP_API_KEY` is unset

## 2. Gitar auto-approve criteria (stream B)

- [x] 2.1 Write `.gitar/config/approve.md`: reach (both policies bind, enables nothing, required checks stay, blocking findings always refuse, any `.gitar/` change refused), the bot and fork refusals at pull-request and commit level, target and risk conditions, the positive list of eligible changes (documentation prose outside the runbooks, comment- and docstring-only code changes, added unit tests), size limits, behaviour on uncertainty, reporting, and allow/skip examples; checked against Gitar's "Auto-approve", "Auto-merge" and "Repository configuration" documentation. Verify every backticked repository path it names exists (`ls -d` from the repository root)
- [x] 2.2 Name `approve.md` in `CONTRIBUTING.md`'s Gitar paragraph; verify the link resolves (`ls .gitar/config/approve.md`)

## 3. Integration

- [x] 3.1 Verify `make check` passes and `openspec validate claude-action-tools-and-gitar-auto-approve --strict` passes

## Workflow follow-up

- Sync into `openspec/specs/release-pipeline/spec.md` and archive in the same pull request, with
  `openspec validate --all --strict` passing.
- After merge: create the `LOGFIRE_MCP_API_KEY` secret (a Logfire API key with `project:read` and
  `project:read_alert` only), mention `@claude` on an issue asking it to query Logfire and read the
  pull request's SonarCloud issues, and check both servers answered.
- Turning on Gitar's auto-approve and auto-merge (merge commit or rebase) is a settings change
  for the repository owner; then observe that a bot's pull request is refused with that reason.
