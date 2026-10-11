"""pCloud file metadata: folder listings, reported digests, and content download.

Listing is one folder at a time rather than `recursive=1`. A recursive listing of a large
library is one response of unbounded size, and a failure halfway through it confirms
nothing; per-folder listing gives the scan a frontier it can persist and resume from.

The version marker is the `hash` field of listing metadata: pCloud documents it as a
64-bit value that changes with the file's content. The digests (SHA-1 in both regions,
plus MD5 in the US or SHA-256 in the EU) are not in listings; `checksumfile` reports them
from pCloud's metadata without transferring the content.
"""

import re
from collections.abc import AsyncGenerator, AsyncIterator, Mapping
from contextlib import asynccontextmanager
from email.utils import parsedate_to_datetime
from typing import Final

import httpx
from pydantic import Field, SecretStr

from curate_d.domain import (
    DomainModel,
    HashAlgorithm,
    PendingFolder,
    Provider,
    StrongFingerprint,
)
from curate_d.ledger.change_detection import ListedFile
from curate_d.providers.errors import ProviderUnavailableError
from curate_d.providers.pcloud.api import JsonObject, call, classify_http, require_api_host

_OP_LIST: Final = "list folder"
_OP_CHECKSUM: Final = "read reported digests"
_OP_DOWNLOAD: Final = "download file"
_HEX: Final = re.compile(r"\A[0-9a-fA-F]+\Z")
_DIGEST_FIELDS: Final = (
    ("sha1", HashAlgorithm.SHA1),
    ("sha256", HashAlgorithm.SHA256),
    ("md5", HashAlgorithm.MD5),
)
_DOWNLOAD_CHUNK: Final = 1024 * 1024


class FolderListing(DomainModel):
    """One folder's direct contents."""

    folder_id: str = Field(min_length=1)
    path: str = Field(min_length=1)
    files: tuple[ListedFile, ...]
    folders: tuple[PendingFolder, ...]


def _join(parent: str, name: str) -> str:
    """Child path under `parent`."""
    return f"{parent.rstrip('/')}/{name}"


def _malformed(account: str, operation: str, what: str) -> ProviderUnavailableError:
    """A response that cannot be what pCloud sends for a successful call."""
    return ProviderUnavailableError(Provider.PCLOUD, account, operation, f"malformed {what}")


def _integer(item: Mapping[str, object], key: str, account: str, operation: str) -> int:
    """Read a non-boolean integer field."""
    value = item.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise _malformed(account, operation, key)
    return value


def _string(item: Mapping[str, object], key: str, account: str, operation: str) -> str:
    """Read a non-empty string field."""
    value = item.get(key)
    if not isinstance(value, str) or not value:
        raise _malformed(account, operation, key)
    return value


def parse_listing(body: JsonObject, *, parent_path: str, account: str) -> FolderListing:
    """Translate a `listfolder` response into listed files and pending subfolders.

    Raises:
        ProviderUnavailableError: the response lacks a field every listing carries.
    """
    metadata = body.get("metadata")
    if not isinstance(metadata, Mapping):
        raise _malformed(account, _OP_LIST, "metadata")
    contents = metadata.get("contents", [])
    if not isinstance(contents, list):
        raise _malformed(account, _OP_LIST, "contents")
    folder_id = str(_integer(metadata, "folderid", account, _OP_LIST))
    files: list[ListedFile] = []
    folders: list[PendingFolder] = []
    for item in contents:
        if not isinstance(item, Mapping):
            raise _malformed(account, _OP_LIST, "entry")
        name = _string(item, "name", account, _OP_LIST)
        path = _join(parent_path, name)
        if item.get("isfolder") is True:
            folders.append(
                PendingFolder(
                    folder_id=str(_integer(item, "folderid", account, _OP_LIST)), path=path
                )
            )
            continue
        modified_raw = _string(item, "modified", account, _OP_LIST)
        try:
            modified = parsedate_to_datetime(modified_raw)
        except (TypeError, ValueError):
            raise _malformed(account, _OP_LIST, "modified") from None
        if modified.tzinfo is None:
            raise _malformed(account, _OP_LIST, "modified timezone")
        marker = item.get("hash")
        files.append(
            ListedFile(
                native_id=str(_integer(item, "fileid", account, _OP_LIST)),
                path=path,
                size_bytes=_integer(item, "size", account, _OP_LIST),
                modified_at=modified,
                version_marker=(
                    str(marker)
                    if isinstance(marker, int) and not isinstance(marker, bool)
                    else None
                ),
            )
        )
    # Sorted so the order work is created in never depends on pCloud's response order.
    return FolderListing(
        folder_id=folder_id,
        path=parent_path,
        files=tuple(sorted(files, key=lambda f: f.native_id)),
        folders=tuple(sorted(folders, key=lambda f: f.folder_id)),
    )


def parse_digests(body: JsonObject, *, account: str) -> tuple[StrongFingerprint, ...]:
    """Translate a `checksumfile` response into digests with their algorithms.

    Raises:
        ProviderUnavailableError: a reported digest is not hex of its algorithm's length.
    """
    digests: list[StrongFingerprint] = []
    for field, algorithm in _DIGEST_FIELDS:
        value = body.get(field)
        if value is None:
            continue
        if not isinstance(value, str) or not _HEX.fullmatch(value):
            raise _malformed(account, _OP_CHECKSUM, field)
        try:
            digests.append(StrongFingerprint(algorithm=algorithm, digest=value.lower()))
        except ValueError:
            raise _malformed(account, _OP_CHECKSUM, field) from None
    return tuple(digests)


class PCloudFiles:
    """File metadata and content for one authorized pCloud account."""

    def __init__(
        self, http: httpx.AsyncClient, *, api_host: str, account: str, access_token: SecretStr
    ) -> None:
        """Bind an account's regional host and token.

        Raises:
            ProviderRequestError: `api_host` is not a pCloud API host.
        """
        self._http = http
        self._host = require_api_host(api_host, account=account, operation="connect")
        self._account = account
        self._token = access_token

    def _headers(self) -> dict[str, str]:
        """The bearer header, built per call so the token is never stored in a client."""
        return {"Authorization": f"Bearer {self._token.get_secret_value()}"}

    async def list_folder(self, folder: PendingFolder) -> FolderListing:
        """List one folder's direct contents.

        Raises:
            ProviderError: pCloud refused or could not answer; see `api.raise_for_result`.
        """
        body = await call(
            self._http,
            "GET",
            f"https://{self._host}/listfolder",
            account=self._account,
            operation=_OP_LIST,
            params={"folderid": folder.folder_id},
            headers=self._headers(),
        )
        return parse_listing(body, parent_path=folder.path, account=self._account)

    async def resolve_folder(self, path: str) -> PendingFolder:
        """Find the folder a configured path names, without listing its files.

        Raises:
            ProviderError: the path does not exist or pCloud could not answer.
        """
        body = await call(
            self._http,
            "GET",
            f"https://{self._host}/listfolder",
            account=self._account,
            operation="resolve folder",
            params={"path": path, "nofiles": "1", "noshares": "1"},
            headers=self._headers(),
        )
        metadata = body.get("metadata")
        if not isinstance(metadata, Mapping):
            raise _malformed(self._account, "resolve folder", "metadata")
        return PendingFolder(
            folder_id=str(_integer(metadata, "folderid", self._account, "resolve folder")),
            path=path,
        )

    async def reported_digests(self, native_id: str) -> tuple[StrongFingerprint, ...]:
        """The digests pCloud reports for a file, read from its metadata.

        Raises:
            ProviderError: pCloud refused or could not answer.
        """
        body = await call(
            self._http,
            "GET",
            f"https://{self._host}/checksumfile",
            account=self._account,
            operation=_OP_CHECKSUM,
            params={"fileid": native_id},
            headers=self._headers(),
        )
        return parse_digests(body, account=self._account)

    @asynccontextmanager
    async def open_content(self, native_id: str) -> AsyncGenerator[AsyncIterator[bytes], None]:
        """Stream a file's content, in chunks, for staging.

        pCloud hands out a short-lived download link on one of its content hosts; the
        link, not the token, authorizes the transfer.

        Raises:
            ProviderError: the link could not be obtained or the download failed.
        """
        body = await call(
            self._http,
            "GET",
            f"https://{self._host}/getfilelink",
            account=self._account,
            operation=_OP_DOWNLOAD,
            params={"fileid": native_id, "forcedownload": "1"},
            headers=self._headers(),
        )
        hosts = body.get("hosts")
        path = body.get("path")
        if not isinstance(hosts, list) or not hosts or not isinstance(path, str):
            raise _malformed(self._account, _OP_DOWNLOAD, "file link")
        host = hosts[0]
        if not isinstance(host, str) or not host:
            raise _malformed(self._account, _OP_DOWNLOAD, "file link host")
        try:
            async with self._http.stream("GET", f"https://{host}{path}") as response:
                classify_http(response, account=self._account, operation=_OP_DOWNLOAD)
                yield response.aiter_bytes(_DOWNLOAD_CHUNK)
        except httpx.TransportError as error:
            raise ProviderUnavailableError(
                Provider.PCLOUD,
                self._account,
                _OP_DOWNLOAD,
                f"transport failure: {type(error).__name__}",
            ) from None
