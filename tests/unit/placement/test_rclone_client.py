"""The rclone control-API client against a scripted daemon."""

import json
from collections.abc import AsyncIterator, Callable, Mapping
from datetime import UTC, datetime

import httpx
import pytest

from curate_d.domain import HashAlgorithm, StrongFingerprint
from curate_d.placement.rclone import RcloneClient, RcloneError, RcloneUnavailableError

type Answer = Callable[[str, dict[str, object]], httpx.Response]


class Daemon:
    """Answers control calls from a function of the method and body; records them."""

    def __init__(self, answer: Answer) -> None:
        """Answer every call with `answer(method, body)`."""
        self.answer = answer
        self.calls: list[tuple[str, dict[str, object]]] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        """Record the call and answer it."""
        method = request.url.path.lstrip("/")
        is_json = request.headers.get("content-type") == "application/json"
        body = json.loads(request.content) if is_json else {}
        self.calls.append((method, body))
        return self.answer(method, body)


def _ok(**body: object) -> httpx.Response:
    """A successful answer."""
    return httpx.Response(200, json=body)


@pytest.fixture
def daemon() -> Daemon:
    """A daemon that answers every call with an empty success."""
    return Daemon(lambda method, body: _ok())


@pytest.fixture
async def client(daemon: Daemon) -> AsyncIterator[RcloneClient]:
    """A client talking to `daemon`."""
    async with httpx.AsyncClient(transport=httpx.MockTransport(daemon.handle)) as http:
        yield RcloneClient(http, "http://rclone:5572/")


async def test_an_error_answer_is_raised_with_rclone_message(
    client: RcloneClient, daemon: Daemon
) -> None:
    """The status and message are kept."""
    daemon.answer = lambda method, body: httpx.Response(500, json={"error": "boom"})
    with pytest.raises(RcloneError, match="rclone core/version failed: boom") as raised:
        await client.call("core/version", {})
    assert raised.value.status == 500


@pytest.mark.parametrize(
    "response",
    [httpx.Response(200, text="not json"), httpx.Response(200, json=["not", "an", "object"])],
)
async def test_a_malformed_answer_is_an_error(
    client: RcloneClient, daemon: Daemon, response: httpx.Response
) -> None:
    """Neither non-JSON nor a JSON array is a result."""
    daemon.answer = lambda method, body: response
    with pytest.raises(RcloneError):
        await client.call("core/version", {})


async def test_an_unreachable_daemon_is_its_own_error() -> None:
    """The transfer never started; the caller can tell."""

    def refuse(request: httpx.Request) -> httpx.Response:
        """Fail to connect."""
        raise httpx.ConnectError("refused", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(refuse)) as http:
        rclone = RcloneClient(http, "http://rclone:5572")
        with pytest.raises(RcloneUnavailableError):
            await rclone.call("core/version", {})
        with pytest.raises(RcloneUnavailableError):
            await rclone.upload("/", "dir", "f.txt", b"x")


async def test_a_new_remote_is_created_and_an_existing_one_updated(
    client: RcloneClient, daemon: Daemon
) -> None:
    """Non-interactively, with its parameters unobscured."""
    daemon.answer = lambda method, body: (
        _ok(remotes=["pcloud-1"]) if method == "config/listremotes" else _ok()
    )
    await client.ensure_remote("pcloud-2", "pcloud", {"hostname": "eapi.pcloud.com"})
    await client.ensure_remote("pcloud-1", "pcloud", {"hostname": "api.pcloud.com"})
    writes = [
        (m, b) for m, b in daemon.calls if m.startswith("config/") and m != "config/listremotes"
    ]
    assert writes == [
        (
            "config/create",
            {
                "name": "pcloud-2",
                "type": "pcloud",
                "parameters": {"hostname": "eapi.pcloud.com"},
                "opt": {"nonInteractive": True, "obscure": False},
            },
        ),
        (
            "config/update",
            {
                "name": "pcloud-1",
                "parameters": {"hostname": "api.pcloud.com"},
                "opt": {"nonInteractive": True, "obscure": False},
            },
        ),
    ]


async def test_a_copy_is_one_async_job_scoped_to_its_group(
    client: RcloneClient, daemon: Daemon
) -> None:
    """copyfile with _async, _group and a per-call _config."""
    daemon.answer = lambda method, body: _ok(jobid=42)
    job = await client.start_copy(
        "a:", "x.jpg", "b:", "y/x.jpg", group="g", config={"Transfers": 1}
    )
    assert job == 42
    assert daemon.calls == [
        (
            "operations/copyfile",
            {
                "srcFs": "a:",
                "srcRemote": "x.jpg",
                "dstFs": "b:",
                "dstRemote": "y/x.jpg",
                "_async": True,
                "_group": "g",
                "_config": {"Transfers": 1},
            },
        )
    ]


async def test_a_copy_without_a_job_id_is_an_error(client: RcloneClient) -> None:
    """Nothing to poll."""
    with pytest.raises(RcloneError, match="no job id"):
        await client.start_copy("a:", "x", "b:", "x", group="g", config={})


async def test_waiting_polls_until_the_job_finishes(client: RcloneClient, daemon: Daemon) -> None:
    """Its error is reported with it."""
    answers = iter([_ok(finished=False), _ok(finished=True, success=False, error="bad")])
    daemon.answer = lambda method, body: next(answers)
    status = await client.wait(7, poll_seconds=0)
    assert (status.finished, status.success, status.error) == (True, False, "bad")


async def test_stat_reads_size_time_and_known_hashes(client: RcloneClient, daemon: Daemon) -> None:
    """Unknown or empty hashes are dropped; digests are lowercased."""
    item: Mapping[str, object] = {
        "Size": 10,
        "ModTime": "2026-09-01T10:00:00.5+00:00",
        "Hashes": {"SHA1": "A" * 40, "quickxor": "zz", "md5": "", "sha256": "b" * 64},
    }
    daemon.answer = lambda method, body: _ok(item=item)
    info = await client.stat("a:", "x.jpg")
    assert info is not None
    assert info.size_bytes == 10
    assert info.modified_at == datetime(2026, 9, 1, 10, 0, 0, 500000, tzinfo=UTC)
    assert info.hashes == (
        StrongFingerprint(algorithm=HashAlgorithm.SHA1, digest="a" * 40),
        StrongFingerprint(algorithm=HashAlgorithm.SHA256, digest="b" * 64),
    )
    assert daemon.calls[0][1]["opt"] == {"showHash": True, "filesOnly": True}


@pytest.mark.parametrize("modified", ["not a time", "2026-09-01T10:00:00"])
async def test_an_unusable_modification_time_is_left_out(
    client: RcloneClient, daemon: Daemon, modified: str
) -> None:
    """Unparseable or without a zone."""
    daemon.answer = lambda method, body: _ok(item={"Size": 1, "ModTime": modified})
    info = await client.stat("a:", "x")
    assert info is not None
    assert info.modified_at is None


async def test_a_missing_object_stats_as_none_and_a_sizeless_one_is_an_error(
    client: RcloneClient, daemon: Daemon
) -> None:
    """Absence is an answer; an item without a size is not one."""
    assert await client.stat("a:", "x") is None
    daemon.answer = lambda method, body: _ok(item={"Name": "x"})
    with pytest.raises(RcloneError, match="no size"):
        await client.stat("a:", "x")


async def test_a_read_back_digest_is_requested_with_download(
    client: RcloneClient, daemon: Daemon
) -> None:
    """hashsumfile names the algorithm; the digest comes back lowercased."""
    daemon.answer = lambda method, body: _ok(hash="C" * 32)
    digest = await client.hashsum_file("b:", "y", HashAlgorithm.MD5, download=True)
    assert digest == StrongFingerprint(algorithm=HashAlgorithm.MD5, digest="c" * 32)
    assert daemon.calls[0] == (
        "operations/hashsumfile",
        {"fs": "b:", "remote": "y", "hashType": "md5", "download": True},
    )
    daemon.answer = lambda method, body: _ok()
    with pytest.raises(RcloneError, match="no hash"):
        await client.hashsum_file("b:", "y", HashAlgorithm.MD5, download=True)


async def test_listing_recurses_and_a_missing_directory_is_empty(
    client: RcloneClient, daemon: Daemon
) -> None:
    """A destination that was never written holds nothing, which is an answer."""
    # rclone reports paths from the root of the fs, the listed directory included.
    daemon.answer = lambda method, body: _ok(list=[{"Path": "lib/a/x.jpg"}, {"Path": "lib/y.jpg"}])
    assert await client.list_files("b:", "lib") == ["a/x.jpg", "y.jpg"]
    assert await client.list_files("b:", "/lib/") == ["a/x.jpg", "y.jpg"]
    assert daemon.calls[0][1]["opt"] == {"recurse": True, "filesOnly": True}
    daemon.answer = lambda method, body: httpx.Response(
        404, json={"error": "error in ListJSON: directory not found"}
    )
    assert await client.list_files("b:", "lib") == []
    daemon.answer = lambda method, body: httpx.Response(500, json={"error": "token expired"})
    with pytest.raises(RcloneError, match="token expired"):
        await client.list_files("b:", "lib")


async def test_an_upload_error_carries_the_status(client: RcloneClient, daemon: Daemon) -> None:
    """For fixtures: a refused write is loud."""
    daemon.answer = lambda method, body: httpx.Response(403, text="denied")
    with pytest.raises(RcloneError, match="denied") as raised:
        await client.upload("/", "dir", "f.txt", b"x")
    assert raised.value.status == 403


async def test_remotes_are_listed_and_objects_deleted(client: RcloneClient, daemon: Daemon) -> None:
    """A malformed remote list reads as none."""
    daemon.answer = lambda method, body: _ok(remotes="nonsense")
    assert await client.list_remotes() == []
    await client.delete("b:", "y")
    assert daemon.calls[-1] == ("operations/deletefile", {"fs": "b:", "remote": "y"})
