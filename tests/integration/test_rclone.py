"""The rclone sidecar's control API, against the local stack's daemon.

Copies run between two directories of the daemon's own local filesystem, which reports
every hash, so the full evidence path runs without a cloud account. The pCloud remote is
configured and listed but not exercised: that needs a real token.
"""

import contextlib
import hashlib
import json
import os
from collections.abc import AsyncIterator
from uuid import uuid4

import httpx
import pytest

from curate_d.domain import HashAlgorithm, VerificationLevel
from curate_d.placement.rclone import RcloneClient, RcloneError
from curate_d.placement.transfer import TransferSpec, copy_and_collect
from curate_d.placement.verification import Verdict, assess

pytestmark = pytest.mark.integration

_URL = os.environ.get("CURATE_D_TEST_RCLONE_URL", "http://127.0.0.1:5572")
# Where the compose daemon keeps its config; the file must be encrypted at rest.
_CONFIG_FILE = "/config/rclone/rclone.conf"


@pytest.fixture
async def rclone() -> AsyncIterator[RcloneClient]:
    """A client for the local daemon."""
    async with httpx.AsyncClient(timeout=30) as http:
        yield RcloneClient(http, _URL)


@pytest.fixture
async def workspace(rclone: RcloneClient) -> AsyncIterator[str]:
    """A private directory in the daemon's filesystem, removed afterwards."""
    root = f"tmp/curate-test-{uuid4().hex[:12]}"
    try:
        yield root
    finally:
        with contextlib.suppress(RcloneError):
            await rclone.call("operations/purge", {"fs": "/", "remote": root})


async def test_remote_tokens_are_stored_encrypted(rclone: RcloneClient) -> None:
    """A configured remote's token never reaches the daemon's config file in plain text."""
    name = f"curate-encrypted-{uuid4().hex[:8]}"
    token = f"plaintext-probe-{uuid4().hex}"
    try:
        await rclone.ensure_remote(
            name,
            "pcloud",
            {
                "token": json.dumps({"access_token": token, "token_type": "bearer"}),
                "hostname": "eapi.pcloud.com",
            },
        )
        body = await rclone.call(
            "core/command",
            {"command": "cat", "arg": [_CONFIG_FILE], "returnType": "COMBINED_OUTPUT"},
        )
        stored = str(body["result"])
        assert stored.startswith("# Encrypted rclone configuration File")
        assert token not in stored
        assert name not in stored
    finally:
        with contextlib.suppress(RcloneError):
            await rclone.call("config/delete", {"name": name})


async def test_the_daemon_answers_and_lists_a_configured_pcloud_remote(
    rclone: RcloneClient,
) -> None:
    """The sidecar is reachable, and a pCloud remote configured on it is listed."""
    version = await rclone.call("core/version", {})
    assert str(version["version"]).startswith("v1.75")
    name = f"curate-smoke-{uuid4().hex[:8]}"
    try:
        await rclone.ensure_remote(
            name,
            "pcloud",
            {
                "token": '{"access_token":"smoke","token_type":"bearer"}',
                "hostname": "eapi.pcloud.com",
            },
        )
        assert name in await rclone.list_remotes()
        await rclone.ensure_remote(name, "pcloud", {"hostname": "api.pcloud.com"})
        config = await rclone.call("config/get", {"name": name})
        assert config["hostname"] == "api.pcloud.com"
    finally:
        await rclone.call("config/delete", {"name": name})
    assert name not in await rclone.list_remotes()


async def test_a_copy_records_both_sides_digests_with_their_algorithms(
    rclone: RcloneClient, workspace: str
) -> None:
    """Copy one fixture, report progress, and leave evidence for a common-hash match."""
    content = os.urandom(3 * 1024 * 1024)
    await rclone.upload("/", f"{workspace}/source/Photos", "a.jpg", content)
    progress: list[tuple[int, int]] = []

    async def record(done: int, total: int) -> None:
        """Keep each progress report."""
        progress.append((done, total))

    evidence = await copy_and_collect(
        rclone,
        TransferSpec(
            source_fs="/",
            source_path=f"/{workspace}/source/Photos/a.jpg",
            destination_fs="/",
            destination_path=f"/{workspace}/destination/2026/a.jpg",
            size_bytes=len(content),
            group=f"test-{uuid4().hex[:8]}",
        ),
        on_progress=record,
        poll_seconds=0.01,
    )

    assert evidence.source.size_bytes == evidence.destination.size_bytes == len(content)
    sha1 = hashlib.sha1(content).hexdigest()
    assert evidence.source.reported_as(HashAlgorithm.SHA1) is not None
    assert evidence.destination.reported_as(HashAlgorithm.SHA1).digest == sha1  # type: ignore[union-attr]
    assert evidence.destination.reported_as(HashAlgorithm.MD5) is not None
    assessment = assess(evidence)
    assert (assessment.verdict, assessment.level) == (
        Verdict.VERIFIED,
        VerificationLevel.STRONG_COMMON_HASH,
    )
    assert all(total == len(content) for _, total in progress)


async def test_a_read_back_digest_is_computed_over_the_copy(
    rclone: RcloneClient, workspace: str
) -> None:
    """hashsumfile with download reads the destination's bytes and hashes them."""
    content = b"read me back"
    await rclone.upload("/", f"{workspace}/dst", "f.txt", content)
    computed = await rclone.hashsum_file(
        "/", f"{workspace}/dst/f.txt", HashAlgorithm.SHA256, download=True
    )
    assert computed.digest == hashlib.sha256(content).hexdigest()


async def test_a_missing_object_stats_as_none(rclone: RcloneClient, workspace: str) -> None:
    """Absence is an answer, not an error."""
    assert await rclone.stat("/", f"{workspace}/nothing-here") is None
