"""`python -m curate_d.manage`: the executor operations, over the control plane API.

For `kubectl exec` into the API pod and for the runbook (docs/executors.md). Every write
sends a request ID, printed before the request goes out, so a command whose answer was
lost can be repeated with `--request-id` and lands once.

    python -m curate_d.manage executors
    python -m curate_d.manage recover worker-io-1
    python -m curate_d.manage fork <workflow> --start-step 4

Exit status: 0 done, 3 refused or conflicting, 4 not found, 2 anything else.
"""

import argparse
import json
import os
import sys
import time
from collections.abc import Callable, Sequence
from typing import Final, TextIO
from urllib.parse import quote
from uuid import UUID, uuid4

import httpx

from curate_d.api.configuration import OPERATOR_HEADER

API_URL_ENV: Final = "CURATE_D_API_URL"
OPERATOR_ENV: Final = "CURATE_D_OPERATOR"
DEFAULT_API_URL: Final = "http://localhost:8000"

EXIT_OK: Final = 0
EXIT_ERROR: Final = 2
EXIT_CONFLICT: Final = 3
EXIT_NOT_FOUND: Final = 4

POLL_SECONDS: Final = 2.0
TIMEOUT_SECONDS: Final = 120.0

_WRITES: Final = frozenset({"recover", "cancel", "resume", "fork", "trigger"})


def _shared_options(*, subcommand: bool) -> argparse.ArgumentParser:
    """Return the options every command takes, before or after its name.

    Args:
        subcommand: Build the copy a subcommand parses. Its options have no default, so
            one left out after the command name keeps the value given before it.
    """
    shared = argparse.ArgumentParser(add_help=False)
    defaults: dict[str, object] = {
        "api": os.environ.get(API_URL_ENV, DEFAULT_API_URL),
        "request_id": None,
        "operator": os.environ.get(OPERATOR_ENV),
        "json": False,
    }
    if subcommand:
        defaults = dict.fromkeys(defaults, argparse.SUPPRESS)
    shared.add_argument(
        "--api",
        default=defaults["api"],
        help=f"Control plane URL (${API_URL_ENV}, default {DEFAULT_API_URL}).",
    )
    shared.add_argument(
        "--request-id",
        type=UUID,
        default=defaults["request_id"],
        help="Repeat an earlier command under its request ID; a new one when omitted.",
    )
    shared.add_argument(
        "--operator",
        default=defaults["operator"],
        help=f"Who is acting, for the API's logs (${OPERATOR_ENV}); where no proxy names you.",
    )
    shared.add_argument(
        "--json", action="store_true", default=defaults["json"], help="Print `executors` as JSON."
    )
    return shared


def parser() -> argparse.ArgumentParser:
    """Return the command-line parser."""
    root = argparse.ArgumentParser(
        prog="python -m curate_d.manage",
        description="Operate curate.d's DBOS executors.",
        parents=[_shared_options(subcommand=False)],
    )
    commands = root.add_subparsers(dest="command", required=True)
    shared = _shared_options(subcommand=True)

    executors = commands.add_parser(
        "executors", help="List executors, or show one.", parents=[shared]
    )
    executors.add_argument("executor_id", nargs="?")

    recover = commands.add_parser(
        "recover", help="Recover a vanished executor's work.", parents=[shared]
    )
    target = recover.add_mutually_exclusive_group(required=True)
    target.add_argument("executor_id", nargs="?")
    target.add_argument("--all", action="store_true", help="Every executor's stranded work.")
    recover.add_argument(
        "--no-wait", action="store_true", help="Do not poll a recovery still running."
    )

    for name, text in (("cancel", "Cancel a workflow."), ("resume", "Resume a workflow.")):
        command = commands.add_parser(name, help=text, parents=[shared])
        command.add_argument("workflow_id")
    commands.choices["resume"].add_argument("--queue", help="Queue to resume onto.")

    fork = commands.add_parser("fork", help="Fork a workflow from a step.", parents=[shared])
    fork.add_argument("workflow_id")
    fork.add_argument("--start-step", type=int, required=True)
    fork.add_argument("--version", help="Application version; the latest when omitted.")
    fork.add_argument("--queue", help="Queue to fork onto; the workflow's own when omitted.")

    trigger = commands.add_parser(
        "trigger", help="Run a schedule's workflow now.", parents=[shared]
    )
    trigger.add_argument("schedule_name")
    return root


def _path(value: str) -> str:
    """Return `value` quoted for one URL path segment."""
    return quote(value, safe="")


def _request(
    arguments: argparse.Namespace, request_id: UUID
) -> tuple[str, str, dict[str, object] | None]:
    """Return the method, path and body for the parsed command."""
    body: dict[str, object] = {"request_id": str(request_id)}
    match arguments.command:
        case "executors":
            path = "/executors"
            if arguments.executor_id:
                path += f"/{_path(arguments.executor_id)}"
            return "GET", path, None
        case "recover":
            if arguments.all:
                return "POST", "/executors/recover", body
            return "POST", f"/executors/{_path(arguments.executor_id)}/recover", body
        case "cancel":
            return "POST", f"/workflows/{_path(arguments.workflow_id)}/cancel", body
        case "resume":
            if arguments.queue:
                body["queue_name"] = arguments.queue
            return "POST", f"/workflows/{_path(arguments.workflow_id)}/resume", body
        case "fork":
            body["start_step"] = arguments.start_step
            if arguments.version:
                body["application_version"] = arguments.version
            if arguments.queue:
                body["queue_name"] = arguments.queue
            return "POST", f"/workflows/{_path(arguments.workflow_id)}/fork", body
        case "trigger":
            return "POST", f"/schedules/{_path(arguments.schedule_name)}/trigger", body
    raise ValueError(f"unknown command {arguments.command!r}")


def _exit_status(code: int) -> int:
    """Return the exit status for an HTTP status."""
    if code < 300:
        return EXIT_OK
    if code == 404:
        return EXIT_NOT_FOUND
    if code in {409, 422}:
        return EXIT_CONFLICT
    return EXIT_ERROR


def executors_table(view: dict[str, object]) -> str:
    """Return `GET /executors`'s answer as a compact table."""
    lines = [f"{'EXECUTOR':<24} {'LIVE':<5} {'VERSION':<28} {'PENDING':>7} {'OLDEST':>8}  STATE"]
    executors = view.get("executors")
    for executor in executors if isinstance(executors, list) else []:
        pending = sum(work["count"] for work in executor["pending"] if work["status"] == "PENDING")
        age = executor["oldest_pending_age_seconds"]
        lines.append(
            f"{executor['executor_id']:<24} {'yes' if executor['live'] else 'no':<5} "
            f"{executor['version'] or '-':<28} {pending:>7} "
            f"{'-' if age is None else f'{age:.0f}s':>8}  "
            f"{'STRANDED' if executor['stranded'] else ''}"
        )
    versions = view.get("versions")
    for version in versions if isinstance(versions, list) else []:
        marks = [
            mark
            for mark, on in (("latest", version["latest"]), ("running", version["running"]))
            if on
        ]
        lines.append(f"version {version['version']}: {', '.join(marks) or 'idle'}")
    return "\n".join(lines)


def run(
    argv: Sequence[str],
    *,
    transport: httpx.BaseTransport | None = None,
    out: TextIO = sys.stdout,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Run one command and return the exit status.

    Args:
        argv: The arguments after the program name.
        transport: The HTTP transport; the network when None. Substituted by tests.
        out: Where results are printed.
        sleep: Waits between polls of a running recovery.
    """
    arguments = parser().parse_args(argv)
    request_id = arguments.request_id or uuid4()
    if arguments.command in _WRITES:
        print(f"request_id {request_id}", file=out, flush=True)
    headers = {OPERATOR_HEADER: arguments.operator} if arguments.operator else {}
    method, path, body = _request(arguments, request_id)
    with httpx.Client(
        base_url=arguments.api, transport=transport, timeout=TIMEOUT_SECONDS, headers=headers
    ) as http:
        try:
            response = http.request(method, path, json=body)
            if arguments.command == "recover" and response.status_code == 202:
                workflow_id = response.json()["workflow_id"]
                print(f"recovery {workflow_id} running", file=out, flush=True)
                while response.status_code == 202 and not arguments.no_wait:
                    sleep(POLL_SECONDS)
                    response = http.get(f"/executors/recoveries/{_path(workflow_id)}")
        except httpx.HTTPError as error:
            print(f"error: {arguments.api}{path}: {error}", file=out)
            return EXIT_ERROR
    try:
        answer = response.json()
    except ValueError:
        answer = {"detail": response.text}
    if arguments.command == "executors" and not arguments.executor_id and not arguments.json:
        print(executors_table(answer) if response.is_success else json.dumps(answer), file=out)
    else:
        print(json.dumps(answer, indent=2), file=out)
    return _exit_status(response.status_code)


def main() -> int:
    """Run the command named on the command line."""
    return run(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
