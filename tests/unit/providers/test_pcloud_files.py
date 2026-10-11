"""pCloud listings, reported digests and content against the stub account."""

import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from pydantic import SecretStr

from curate_d.domain import HashAlgorithm, PendingFolder
from curate_d.providers.errors import (
    CredentialsRejectedError,
    ProviderRequestError,
    ProviderUnavailableError,
)
from curate_d.providers.pcloud.files import PCloudFiles, parse_digests, parse_listing
from tests.stubs.pcloud import Failure, StubPCloud


@pytest.fixture
def pcloud() -> StubPCloud:
    """An EU account holding a small nested tree."""
    stub = StubPCloud()
    stub.file("/Photos/2026/beach.jpg", b"jpeg-bytes")
    stub.file("/Photos/notes.txt", b"hello")
    stub.folder("/Photos/empty")
    return stub


@pytest.fixture
async def files(pcloud: StubPCloud) -> AsyncIterator[PCloudFiles]:
    """A client for the stub account."""
    async with pcloud.client() as http:
        yield PCloudFiles(
            http,
            api_host=pcloud.host,
            account=str(pcloud.uid),
            access_token=SecretStr(pcloud.access_token),
        )


async def test_a_folder_lists_its_files_and_subfolders_with_full_paths(
    files: PCloudFiles, pcloud: StubPCloud
) -> None:
    """Files carry id, path, size, time and marker; subfolders become pending folders."""
    photos = await files.resolve_folder("/Photos")
    listing = await files.list_folder(photos)

    assert [f.path for f in listing.files] == ["/Photos/notes.txt"]
    [notes] = listing.files
    assert notes.size_bytes == 5
    assert notes.modified_at == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert notes.version_marker is not None
    assert sorted(f.path for f in listing.folders) == ["/Photos/2026", "/Photos/empty"]


async def test_listing_reads_no_content(files: PCloudFiles, pcloud: StubPCloud) -> None:
    """Enumeration is metadata only."""
    root = await files.resolve_folder("/")
    await files.list_folder(root)
    assert pcloud.requests["content"] == 0
    assert pcloud.requests["getfilelink"] == 0


async def test_reported_digests_carry_their_algorithms(
    files: PCloudFiles, pcloud: StubPCloud
) -> None:
    """An EU account reports SHA-1 and SHA-256, from metadata."""
    stub = next(iter(pcloud.files.values()))
    digests = await files.reported_digests(str(stub.file_id))
    assert {d.algorithm for d in digests} == {HashAlgorithm.SHA1, HashAlgorithm.SHA256}
    assert pcloud.requests["content"] == 0


async def test_a_us_account_reports_md5_instead_of_sha256() -> None:
    """The region decides the second digest."""
    stub = StubPCloud(region="us")
    file = stub.file("/a.bin", b"x")
    async with stub.client() as http:
        client = PCloudFiles(
            http, api_host=stub.host, account="1", access_token=SecretStr(stub.access_token)
        )
        digests = await client.reported_digests(str(file.file_id))
    assert {d.algorithm for d in digests} == {HashAlgorithm.SHA1, HashAlgorithm.MD5}


async def test_content_streams_the_files_bytes(files: PCloudFiles, pcloud: StubPCloud) -> None:
    """Download goes through the link pCloud issues."""
    stub = next(f for f in pcloud.files.values() if f.name == "beach.jpg")
    async with files.open_content(str(stub.file_id)) as chunks:
        content = b"".join([chunk async for chunk in chunks])
    assert content == b"jpeg-bytes"


async def test_a_revoked_token_is_a_credential_rejection(pcloud: StubPCloud) -> None:
    """Calls with a token pCloud no longer accepts need a person."""
    async with pcloud.client() as http:
        client = PCloudFiles(
            http, api_host=pcloud.host, account="1", access_token=SecretStr("revoked")
        )
        with pytest.raises(CredentialsRejectedError):
            await client.list_folder(PendingFolder(folder_id="0", path="/"))


async def test_a_failing_folder_reports_a_transient_error(
    files: PCloudFiles, pcloud: StubPCloud
) -> None:
    """A server error on one folder is surfaced for the scan to record."""
    pcloud.fail("listfolder", "0", Failure(times=1, status=503))
    with pytest.raises(ProviderUnavailableError):
        await files.list_folder(PendingFolder(folder_id="0", path="/"))


async def test_a_missing_root_is_a_request_error(files: PCloudFiles) -> None:
    """A configured path that does not exist is the configuration's fault."""
    with pytest.raises(ProviderRequestError, match="2005"):
        await files.resolve_folder("/nope")


def test_a_host_outside_pcloud_is_refused() -> None:
    """The client never sends the token anywhere but pCloud."""
    with pytest.raises(ProviderRequestError):
        PCloudFiles(
            None,  # type: ignore[arg-type]
            api_host="evil.example",
            account="1",
            access_token=SecretStr("t"),
        )


@pytest.mark.parametrize(
    "body",
    [
        {"result": 0},
        {"result": 0, "metadata": {"folderid": 0, "contents": "x"}},
        {"result": 0, "metadata": {"contents": []}},
        {"result": 0, "metadata": {"folderid": 0, "contents": [1]}},
        {"result": 0, "metadata": {"folderid": 0, "contents": [{"name": "a", "fileid": 1}]}},
        {
            "result": 0,
            "metadata": {
                "folderid": 0,
                "contents": [{"name": "a", "fileid": 1, "size": 1, "modified": "not a date"}],
            },
        },
        {
            "result": 0,
            "metadata": {
                "folderid": 0,
                "contents": [
                    {"name": "a", "fileid": 1, "size": 1, "modified": "Fri, 02 Jan 2026 03:04:05"}
                ],
            },
        },
    ],
)
def test_a_malformed_listing_is_not_trusted(body: dict[str, object]) -> None:
    """A listing missing what every listing carries is treated as a bad answer."""
    with pytest.raises(ProviderUnavailableError, match="malformed"):
        parse_listing(body, parent_path="/", account="1")


def test_a_file_without_a_marker_is_listed_without_one() -> None:
    """A missing marker leaves change detection to size and time."""
    listing = parse_listing(
        {
            "result": 0,
            "metadata": {
                "folderid": 0,
                "contents": [
                    {
                        "name": "a",
                        "fileid": 1,
                        "size": 1,
                        "modified": "Fri, 02 Jan 2026 03:04:05 +0000",
                    }
                ],
            },
        },
        parent_path="/",
        account="1",
    )
    assert listing.files[0].version_marker is None
    assert listing.files[0].path == "/a"


@pytest.mark.parametrize(
    "body", [{"sha1": "xyz"}, {"sha1": 5}, {"sha256": "ab"}, {"md5": "0" * 31}]
)
def test_a_malformed_digest_is_not_trusted(body: dict[str, object]) -> None:
    """A digest of the wrong shape is refused rather than recorded."""
    with pytest.raises(ProviderUnavailableError, match="malformed"):
        parse_digests(body, account="1")


def test_uppercase_digests_are_normalised() -> None:
    """Recorded digests are lowercase hex whatever case pCloud used."""
    sha1 = hashlib.sha1(b"x").hexdigest().upper()
    [digest] = parse_digests({"sha1": sha1}, account="1")
    assert digest.digest == sha1.lower()
