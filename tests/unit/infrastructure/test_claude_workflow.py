"""The Claude workflow's tools: an explicit allow-list, the checks it can run, read-only MCP."""

import json
import re
import shlex
from pathlib import Path
from typing import cast

import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "claude.yml"

type Document = dict[str, object]

# Never allowed: these reach past the working copy and the `gh` writes the job is meant for.
_FORBIDDEN_COMMANDS = ("gh api", "gh pr merge")

# The read tools of each MCP server, by name prefix. A tool outside these may write.
_READ_TOOLS = {
    "sonarqube": ("search_", "get_", "show_", "list_"),
    "logfire": ("query_", "project_list", "project_logfire_link", "issue_list", "issue_get"),
}


def _steps() -> list[Document]:
    """Return the steps of the workflow's `claude` job."""
    workflow = cast(Document, yaml.safe_load(WORKFLOW.read_text(encoding="utf-8")))
    return cast(list[Document], cast(Document, cast(Document, workflow["jobs"])["claude"])["steps"])


def _step(name: str) -> Document:
    """Return the step called `name`."""
    return next(s for s in _steps() if s.get("name") == name)


def _claude_args() -> list[str]:
    """Return `claude_args` split into arguments, as the action does.

    The action drops full-line `#` comments before splitting the rest shell-style.
    """
    raw = cast(str, cast(Document, _step("Run Claude Code")["with"])["claude_args"])
    lines = [line for line in raw.splitlines() if not line.strip().startswith("#")]
    return shlex.split("\n".join(lines))


def _flag_values(flag: str) -> list[str]:
    """Return every value given to `flag`, accumulated over repeated occurrences."""
    values: list[str] = []
    current: str | None = None
    for arg in _claude_args():
        if arg.startswith("--"):
            current = arg
        elif current == flag:
            # A flag's values run up to the next flag.
            values.append(arg)
    return values


def _allowed_tools() -> list[str]:
    """Return the allow-list: every `--allowedTools` value, split on commas."""
    return [t.strip() for v in _flag_values("--allowedTools") for t in v.split(",") if t.strip()]


def _is_forbidden(entry: str) -> bool:
    """Return whether `entry` allows unrestricted shell or covers a forbidden command.

    An entry covers a command when it names it, or a narrower form of it, or when its
    wildcards (`*` anywhere, a trailing `:*` prefix) match it.
    """
    match = re.fullmatch(r"Bash(?:\((.*)\))?", entry)
    if not match:
        return False
    rule = (match.group(1) or "").strip()
    if not rule:
        return True
    pattern = re.escape(rule.removesuffix(":*")).replace(r"\*", ".*")
    if rule.endswith(":*"):
        pattern += ".*"
    return any(
        rule.startswith(command)
        or re.fullmatch(pattern, command)
        or re.fullmatch(pattern, f"{command} x")
        for command in _FORBIDDEN_COMMANDS
    )


def _mcp_servers() -> dict[str, Document]:
    """Return the servers of the inline `--mcp-config` JSON."""
    (config,) = _flag_values("--mcp-config")
    return cast(dict[str, Document], json.loads(config)["mcpServers"])


def test_the_allow_list_has_no_blanket_entry() -> None:
    """Unrestricted shell, `gh api` and `gh pr merge` are never on the list."""
    tools = _allowed_tools()
    assert tools
    assert [t for t in tools if _is_forbidden(t)] == []


def test_the_forbidden_pattern_catches_each_blanket_form() -> None:
    """The guard itself: each never-allowed form matches, a scoped command does not."""
    for entry in (
        "Bash",
        "Bash()",
        "Bash(*)",
        "Bash(:*)",
        "Bash(gh api:*)",
        "Bash(gh api repos/x:*)",
        "Bash(gh pr merge:*)",
        "Bash(gh pr merge 1)",
        "Bash(gh:*)",
        "Bash(gh pr:*)",
        "Bash(gh *)",
        "Bash(gh pr m*)",
    ):
        assert _is_forbidden(entry), entry
    for entry in ("Bash(gh pr view:*)", "Bash(gh search:*)", "Bash(make test)", "WebFetch"):
        assert not _is_forbidden(entry), entry


def test_claude_args_keep_permission_checks() -> None:
    """No argument skips permission checks, which would admit tools off the list."""
    bypasses = ("dangerously-skip-permissions", "bypassPermissions")
    assert [a for a in _claude_args() if any(b in a for b in bypasses)] == []


def test_claude_can_run_the_checks() -> None:
    """Lint, type check and the unit suite are allowed, on the locked dependencies."""
    tools = _allowed_tools()
    for command in ("make lint", "make typecheck", "make test", "make format"):
        assert f"Bash({command})" in tools
    assert _step("Install dependencies")["run"] == "uv sync --locked --no-build"


def test_the_checkout_keeps_full_history() -> None:
    """`git log` and `git blame` need the history a depth-1 clone drops."""
    checkout = _step("Checkout repository")
    assert cast(Document, checkout["with"])["fetch-depth"] == 0


def test_every_mcp_tool_allowed_is_a_read_tool_of_a_configured_server() -> None:
    """An allowed MCP tool names a configured server and one of its read tools."""
    servers = _mcp_servers()
    mcp_tools = [t for t in _allowed_tools() if t.startswith("mcp__")]
    assert mcp_tools
    for tool in mcp_tools:
        _, server, name = tool.split("__", 2)
        assert server in servers, tool
        assert name.startswith(_READ_TOOLS[server]), tool


def test_the_sonarqube_server_is_read_only_and_pinned() -> None:
    """Read-only mode on, the token forwarded by name, the image pinned by digest."""
    server = _mcp_servers()["sonarqube"]
    env = cast(dict[str, str], server["env"])
    assert env["SONARQUBE_READ_ONLY"] == "true"
    assert env["SONARQUBE_TOKEN"] == "${SONAR_TOKEN}"
    args = cast(list[str], server["args"])
    assert args[args.index("SONARQUBE_READ_ONLY") - 1] == "-e"
    image = args[-1]
    assert re.fullmatch(r"sonarsource/sonarqube-mcp:[\w.]+@sha256:[0-9a-f]{64}", image)
    assert _step("Pull the SonarQube MCP image")["run"] == f"docker pull {image}"


def test_mcp_secrets_come_from_the_step_environment() -> None:
    """The JSON names the secrets; the Claude step's environment carries them."""
    env = cast(Document, _step("Run Claude Code")["env"])
    assert env["SONAR_TOKEN"] == "${{ secrets.SONAR_TOKEN }}"
    assert env["LOGFIRE_MCP_API_KEY"] == "${{ secrets.LOGFIRE_MCP_API_KEY }}"
    headers = cast(dict[str, str], _mcp_servers()["logfire"]["headers"])
    assert headers["Authorization"] == "Bearer ${LOGFIRE_MCP_API_KEY:-}"
