"""The rclone remote-control API, as placement uses it.

One `operations/copyfile` per placement — never `sync/copy` over a tree, which would fold
hundreds of files into one result and lose per-file verification, retry and audit. The
copy is started with `_async` for a job id, `_group` to scope its statistics to this
placement, and `_config` to override transfer settings for this call alone. Evidence is
collected afterwards with `operations/stat` (with hashes) on both sides, and
`operations/hashsumfile` with `download` when no common hash exists.

rclone's own post-copy check stays enabled: when sizes or the common hash differ it
deletes the destination copy and fails the job, and the error names both values, which
`corrupted_transfer` recovers for the ledger.
"""

import asyncio
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Final

import httpx
from pydantic import AwareDatetime

from curate_d.domain import DomainModel, HashAlgorithm, StrongFingerprint
from curate_d.placement.verification import CorruptTransfer

_HASH_NAMES: Final = {
    "sha1": HashAlgorithm.SHA1,
    "sha256": HashAlgorithm.SHA256,
    "md5": HashAlgorithm.MD5,
}
# rclone's messages when its post-copy check rejects a transfer (fs/operations), with or
# without each side's Fs named: "corrupted on transfer: sizes differ src(a:) 10 vs dst(b:) 11"
# and "corrupted on transfer: sha1 hashes differ src(a:) "x" vs dst(b:) "y"".
_SIDE: Final = r"(?:(?:src|dst)\([^)]*\) )?"
_CORRUPT_SIZES: Final = re.compile(
    rf"corrupted on transfer: sizes differ {_SIDE}(\d+) vs {_SIDE}(\d+)"
)
_DIGEST: Final = r'"?([0-9a-fA-F]+)"?'
_CORRUPT_HASHES: Final = re.compile(
    rf"corrupted on transfer: (\w+) hash(?:es)? differ {_SIDE}{_DIGEST} vs {_SIDE}{_DIGEST}"
)
_NO_SPACE: Final = re.compile(r"(?i)no space left on device|disk quota exceeded")
_NOT_FOUND: Final = re.compile(r"(?i)directory not found")


class RcloneError(RuntimeError):
    """An rclone call failed."""

    def __init__(self, method: str, message: str, *, status: int | None = None) -> None:
        """Name the call and what rclone said."""
        self.method = method
        self.status = status
        self.message = message
        super().__init__(f"rclone {method} failed: {message}")


class RcloneUnavailableError(RcloneError):
    """The control API could not be reached; the transfer never started."""


class ObjectInfo(DomainModel):
    """What `operations/stat` reports about one object."""

    size_bytes: int
    modified_at: AwareDatetime | None = None
    hashes: tuple[StrongFingerprint, ...] = ()


class JobStatus(DomainModel):
    """An async job's state."""

    finished: bool
    success: bool
    error: str = ""


def corrupted_transfer(error: str) -> CorruptTransfer | None:
    """The mismatch named in a job error from rclone's post-copy check, if that is what it is."""
    sizes = _CORRUPT_SIZES.search(error)
    if sizes is not None:
        return CorruptTransfer(what="size", source=sizes[1], destination=sizes[2])
    hashes = _CORRUPT_HASHES.search(error)
    if hashes is not None:
        return CorruptTransfer(
            what=hashes[1].lower(), source=hashes[2].lower(), destination=hashes[3].lower()
        )
    return None


def out_of_space(error: str) -> bool:
    """Whether a job failed because local scratch ran out."""
    return _NO_SPACE.search(error) is not None


def _object_info(item: Mapping[str, object]) -> ObjectInfo:
    """Translate an lsjson item."""
    size = item.get("Size")
    if not isinstance(size, int) or isinstance(size, bool):
        raise RcloneError("operations/stat", "item has no size")
    modified = item.get("ModTime")
    parsed: datetime | None = None
    if isinstance(modified, str):
        try:
            parsed = datetime.fromisoformat(modified)
        except ValueError:
            parsed = None
        if parsed is not None and parsed.tzinfo is None:
            parsed = None
    hashes: list[StrongFingerprint] = []
    raw = item.get("Hashes")
    if isinstance(raw, Mapping):
        for name, digest in raw.items():
            algorithm = _HASH_NAMES.get(str(name).lower())
            if algorithm is None or not isinstance(digest, str) or not digest:
                continue
            try:
                hashes.append(StrongFingerprint(algorithm=algorithm, digest=digest.lower()))
            except ValueError:
                continue
    return ObjectInfo(
        size_bytes=size,
        modified_at=parsed,
        hashes=tuple(sorted(hashes, key=lambda h: h.algorithm.value)),
    )


class RcloneClient:
    """Calls to one rclone daemon's control API."""

    def __init__(self, http: httpx.AsyncClient, base_url: str) -> None:
        """Bind the daemon's address; the HTTP client is the caller's."""
        self._http = http
        self._base = base_url.rstrip("/")

    async def call(self, method: str, params: Mapping[str, object]) -> Mapping[str, object]:
        """POST one control call and return its JSON result.

        Raises:
            RcloneUnavailableError: the daemon could not be reached.
            RcloneError: the daemon answered with an error.
        """
        try:
            response = await self._http.post(f"{self._base}/{method}", json=dict(params))
        except httpx.TransportError as error:
            raise RcloneUnavailableError(method, type(error).__name__) from None
        try:
            body = response.json()
        except ValueError:
            raise RcloneError(method, "response is not JSON", status=response.status_code) from None
        if not isinstance(body, Mapping):
            raise RcloneError(method, "response is not an object", status=response.status_code)
        if response.status_code != httpx.codes.OK:
            raise RcloneError(method, str(body.get("error", body)), status=response.status_code)
        return body

    async def list_remotes(self) -> list[str]:
        """Configured remote names."""
        body = await self.call("config/listremotes", {})
        remotes = body.get("remotes") or []
        return [str(r) for r in remotes] if isinstance(remotes, list) else []

    async def ensure_remote(self, name: str, backend: str, parameters: Mapping[str, str]) -> None:
        """Create a remote, or update it when it exists with other parameters.

        Non-interactive: an OAuth backend given its token must not start its own
        authorization flow and wait for a browser that will never come.
        """
        options = {"nonInteractive": True, "obscure": False}
        if name in await self.list_remotes():
            await self.call(
                "config/update",
                {"name": name, "parameters": dict(parameters), "opt": options},
            )
        else:
            await self.call(
                "config/create",
                {"name": name, "type": backend, "parameters": dict(parameters), "opt": options},
            )

    async def start_copy(
        self,
        src_fs: str,
        src_remote: str,
        dst_fs: str,
        dst_remote: str,
        *,
        group: str,
        config: Mapping[str, object],
    ) -> int:
        """Start one file copy as an async job, returning its id."""
        body = await self.call(
            "operations/copyfile",
            {
                "srcFs": src_fs,
                "srcRemote": src_remote,
                "dstFs": dst_fs,
                "dstRemote": dst_remote,
                "_async": True,
                "_group": group,
                "_config": dict(config),
            },
        )
        job = body.get("jobid")
        if not isinstance(job, int):
            raise RcloneError("operations/copyfile", "no job id returned")
        return job

    async def job_status(self, job: int) -> JobStatus:
        """One job's state."""
        body = await self.call("job/status", {"jobid": job})
        return JobStatus(
            finished=bool(body.get("finished")),
            success=bool(body.get("success")),
            error=str(body.get("error") or ""),
        )

    async def stats(self, group: str) -> Mapping[str, object]:
        """Transfer statistics scoped to one group."""
        return await self.call("core/stats", {"group": group, "short": True})

    async def wait(self, job: int, *, poll_seconds: float = 0.5) -> JobStatus:
        """Poll a job until it finishes."""
        while True:
            status = await self.job_status(job)
            if status.finished:
                return status
            await asyncio.sleep(poll_seconds)

    async def stat(self, fs: str, remote: str) -> ObjectInfo | None:
        """Size, time and every hash the backend reports for one object; None if absent."""
        body = await self.call(
            "operations/stat",
            {"fs": fs, "remote": remote, "opt": {"showHash": True, "filesOnly": True}},
        )
        item = body.get("item")
        if item is None:
            return None
        if not isinstance(item, Mapping):
            raise RcloneError("operations/stat", "item is not an object")
        return _object_info(item)

    async def hashsum_file(
        self, fs: str, remote: str, algorithm: HashAlgorithm, *, download: bool
    ) -> StrongFingerprint:
        """A digest of one object; with `download`, computed over its bytes as read back."""
        body = await self.call(
            "operations/hashsumfile",
            {
                "fs": fs,
                "remote": remote,
                "hashType": algorithm.value,
                "download": download,
            },
        )
        digest = body.get("hash")
        if not isinstance(digest, str) or not digest:
            raise RcloneError("operations/hashsumfile", "no hash returned")
        return StrongFingerprint(algorithm=algorithm, digest=digest.lower())

    async def list_files(self, fs: str, directory: str) -> list[str]:
        """Every file under `directory`, relative to it; empty when it does not exist."""
        try:
            body = await self.call(
                "operations/list",
                {"fs": fs, "remote": directory, "opt": {"recurse": True, "filesOnly": True}},
            )
        except RcloneError as error:
            if _NOT_FOUND.search(error.message) and not isinstance(error, RcloneUnavailableError):
                return []
            raise
        items = body.get("list")
        if not isinstance(items, list):
            raise RcloneError("operations/list", "no list returned")
        # rclone reports each path from the root of `fs`, the listed directory included.
        prefix = f"{directory.strip('/')}/" if directory.strip("/") else ""
        return [
            path.removeprefix(prefix)
            for item in items
            if isinstance(item, Mapping) and "Path" in item
            for path in [str(item["Path"])]
        ]

    async def upload(self, fs: str, directory: str, name: str, content: bytes) -> None:
        """Write a small file through the daemon; for fixtures and smoke checks."""
        try:
            response = await self._http.post(
                f"{self._base}/operations/uploadfile",
                params={"fs": fs, "remote": directory},
                files={"file": (name, content)},
            )
        except httpx.TransportError as error:
            raise RcloneUnavailableError("operations/uploadfile", type(error).__name__) from None
        if response.status_code != httpx.codes.OK:
            raise RcloneError("operations/uploadfile", response.text, status=response.status_code)

    async def delete(self, fs: str, remote: str) -> None:
        """Delete one object."""
        await self.call("operations/deletefile", {"fs": fs, "remote": remote})
