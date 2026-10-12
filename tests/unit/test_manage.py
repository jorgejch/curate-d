"""The operator CLI: what each command sends, what it prints, and how it exits."""

import io
import json
from collections.abc import Callable
from uuid import UUID

import httpx
import pytest

from curate_d import manage

REQUEST_ID = UUID("00000000-0000-4000-8000-000000000005")


class _Api:
    """A mock API recording requests and answering from a list of responses."""

    def __init__(self, *answers: tuple[int, object]) -> None:
        """Answer with `answers` in turn, repeating the last; 200 and `{}` when none."""
        self.answers = list(answers) or [(200, {})]
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        """Record `request` and return the next answer."""
        self.requests.append(request)
        code, body = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        return httpx.Response(code, json=body)

    def bodies(self) -> list[object]:
        """Return each request's JSON body, None for a bodiless one."""
        return [
            json.loads(request.content) if request.content else None for request in self.requests
        ]


def _run(
    api: _Api, *argv: str, sleep: Callable[[float], None] = lambda _s: None
) -> tuple[int, str]:
    """Run the CLI against `api`; return its exit status and output."""
    out = io.StringIO()
    status = manage.run(
        ["--api", "http://api.test", *argv],
        transport=httpx.MockTransport(api),
        out=out,
        sleep=sleep,
    )
    return status, out.getvalue()


@pytest.mark.parametrize(
    ("argv", "method", "path", "body"),
    [
        (["executors", "io-0"], "GET", "/executors/io-0", None),
        (["recover", "io-1"], "POST", "/executors/io-1/recover", {}),
        (["recover", "--all"], "POST", "/executors/recover", {}),
        (["cancel", "scan:p:1:/A:c"], "POST", "/workflows/scan%3Ap%3A1%3A%2FA%3Ac/cancel", {}),
        (["resume", "w", "--queue", "scan"], "POST", "/workflows/w/resume", {"queue_name": "scan"}),
        (
            ["fork", "w", "--start-step", "4", "--version", "v2", "--queue", "verify"],
            "POST",
            "/workflows/w/fork",
            {"start_step": 4, "application_version": "v2", "queue_name": "verify"},
        ),
        (["fork", "w", "--start-step", "1"], "POST", "/workflows/w/fork", {"start_step": 1}),
        (["trigger", "prune"], "POST", "/schedules/prune/trigger", {}),
    ],
)
def test_each_command_sends_its_request(
    argv: list[str], method: str, path: str, body: dict[str, object] | None
) -> None:
    """The path, method and body each subcommand maps to."""
    api = _Api()
    status, _ = _run(api, "--request-id", str(REQUEST_ID), *argv)
    assert status == manage.EXIT_OK
    (request,) = api.requests
    assert (request.method, request.url.raw_path.decode()) == (method, path)
    expected = None if body is None else {"request_id": str(REQUEST_ID), **body}
    assert api.bodies() == [expected]


def test_a_new_request_id_is_generated_and_printed_first() -> None:
    """Printed before the request, so a lost answer can be retried under it."""
    api = _Api()
    _, out = _run(api, "cancel", "w")
    first = out.splitlines()[0]
    assert first.startswith("request_id ")
    request_id = UUID(first.removeprefix("request_id "))
    assert api.bodies() == [{"request_id": str(request_id)}]


def test_a_given_request_id_is_reused() -> None:
    """A retry sends the same ID."""
    api = _Api()
    _, out = _run(api, "--request-id", str(REQUEST_ID), "trigger", "prune")
    assert out.splitlines()[0] == f"request_id {REQUEST_ID}"
    assert api.bodies() == [{"request_id": str(REQUEST_ID)}]


def test_reads_print_no_request_id() -> None:
    """Only writes carry one."""
    _, out = _run(_Api((200, {"executor_id": "io-0"})), "executors", "io-0")
    assert "request_id" not in out


@pytest.mark.parametrize(
    ("code", "status"),
    [(200, 0), (202, 0), (404, 4), (409, 3), (422, 3), (500, 2), (503, 2)],
)
def test_exit_status_follows_the_answer(code: int, status: int) -> None:
    """Scripts tell refused from missing from broken."""
    assert _run(_Api((code, {"detail": "x"})), "cancel", "w")[0] == status


def test_an_unreachable_api_exits_2() -> None:
    """A transport error is reported, not raised."""

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    out = io.StringIO()
    status = manage.run(
        ["--api", "http://api.test", "executors"], transport=httpx.MockTransport(refuse), out=out
    )
    assert status == manage.EXIT_ERROR
    assert "refused" in out.getvalue()


def test_recover_polls_a_running_recovery_until_its_report() -> None:
    """A 202 is followed by polls of the recovery's workflow."""
    running = {"workflow_id": "recover-executor:io-1:x", "executor_id": "io-1"}
    report = {**running, "reenqueued": ["a"]}
    api = _Api((202, running), (202, running), (200, report))
    waits: list[float] = []
    status, out = _run(api, "recover", "io-1", sleep=waits.append)
    assert status == manage.EXIT_OK
    assert [request.url.raw_path.decode() for request in api.requests] == [
        "/executors/io-1/recover",
        "/executors/recoveries/recover-executor%3Aio-1%3Ax",
        "/executors/recoveries/recover-executor%3Aio-1%3Ax",
    ]
    assert waits == [manage.POLL_SECONDS, manage.POLL_SECONDS]
    assert '"reenqueued"' in out


def test_recover_without_waiting_stops_at_the_202() -> None:
    """--no-wait leaves the polling to the operator."""
    api = _Api((202, {"workflow_id": "w", "executor_id": "io-1"}))
    status, out = _run(api, "recover", "io-1", "--no-wait")
    assert status == manage.EXIT_OK
    assert len(api.requests) == 1
    assert "recovery w running" in out


def test_the_operator_is_sent_where_no_proxy_names_one() -> None:
    """Inside the pod there is no proxy; the flag names who acted."""
    api = _Api()
    _run(api, "--operator", "ops@example", "cancel", "w")
    assert api.requests[0].headers["Tailscale-User-Login"] == "ops@example"


def test_the_executor_list_prints_a_table() -> None:
    """Stranded executors stand out; versions say which run."""
    view = {
        "executors": [
            {
                "executor_id": "io-1",
                "live": False,
                "version": None,
                "pending": [{"status": "PENDING", "count": 2}, {"status": "ENQUEUED", "count": 1}],
                "oldest_pending_age_seconds": 61.2,
                "stranded": True,
            }
        ],
        "versions": [{"version": "v2", "latest": True, "running": False}],
    }
    _, out = _run(_Api((200, view)), "executors")
    lines = out.splitlines()
    assert lines[0].startswith("EXECUTOR")
    assert lines[1].split() == ["io-1", "no", "-", "2", "61s", "STRANDED"]
    assert lines[2] == "version v2: latest"


def test_the_executor_list_prints_json_on_request() -> None:
    """--json prints the API's answer as is."""
    _, out = _run(_Api((200, {"executors": []})), "--json", "executors")
    assert json.loads(out) == {"executors": []}
