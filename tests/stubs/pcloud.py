"""An in-memory pCloud, served through httpx's MockTransport.

Implements the endpoints the application calls — `listfolder`, `checksumfile`,
`getfilelink` and the content host, `oauth2_token` — over a tree built in the test. It
counts every request by endpoint, which is how a test proves that a scan read no content,
and it can be told to fail a given call a given number of times.
"""

import hashlib
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import format_datetime
from itertools import count

import httpx

CONTENT_HOST = "c1.pcloud.test"


@dataclass
class StubFile:
    """A file in the stub tree."""

    file_id: int
    parent_id: int
    name: str
    content: bytes
    modified: datetime
    revision: int = 1
    held_marker: int | None = None

    @property
    def marker(self) -> int:
        """pCloud's `hash`: changes whenever the content does, unless held."""
        if self.held_marker is not None:
            return self.held_marker
        return int.from_bytes(hashlib.sha256(self.content).digest()[:7]) + self.revision


@dataclass
class StubFolder:
    """A folder in the stub tree."""

    folder_id: int
    parent_id: int | None
    name: str


@dataclass
class Failure:
    """Answer the next `times` matching calls with `body` (or `status`) instead."""

    times: int
    status: int = 200
    body: dict[str, object] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)


class StubPCloud:
    """One pCloud account, in the EU region unless told otherwise."""

    def __init__(self, *, region: str = "eu", uid: int = 4242) -> None:
        """Start with an empty root folder."""
        self.region = region
        self.uid = uid
        self.host = "eapi.pcloud.com" if region == "eu" else "api.pcloud.com"
        self.folders: dict[int, StubFolder] = {0: StubFolder(0, None, "")}
        self.files: dict[int, StubFile] = {}
        self.requests: Counter[str] = Counter()
        self.failures: dict[tuple[str, str], Failure] = {}
        self._ids = count(1000)
        self.access_token = "stub-access-token"

    # Building the tree.

    def folder(self, path: str) -> int:
        """Create every folder along `path`, returning the last one's id."""
        parent = 0
        for name in [part for part in path.split("/") if part]:
            existing = next(
                (f for f in self.folders.values() if f.parent_id == parent and f.name == name),
                None,
            )
            if existing is None:
                existing = StubFolder(next(self._ids), parent, name)
                self.folders[existing.folder_id] = existing
            parent = existing.folder_id
        return parent

    def file(self, path: str, content: bytes, *, modified: datetime | None = None) -> StubFile:
        """Create a file at `path`, creating its folders."""
        parent_path, _, name = path.rpartition("/")
        stub = StubFile(
            file_id=next(self._ids),
            parent_id=self.folder(parent_path),
            name=name,
            content=content,
            modified=modified or datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
        )
        self.files[stub.file_id] = stub
        return stub

    def modify(self, stub: StubFile, content: bytes) -> None:
        """Change a file's content, advancing its marker as pCloud would."""
        stub.content = content
        stub.revision += 1

    def change_silently(self, stub: StubFile, content: bytes) -> None:
        """Change content while the marker stays put: what a full rescan exists to catch."""
        stub.held_marker = stub.marker
        stub.content = content

    def rename(self, stub: StubFile, name: str) -> None:
        """Rename a file in place; its id is unchanged."""
        stub.name = name

    def path_of(self, folder_id: int) -> str:
        """The full path of a folder."""
        parts: list[str] = []
        current: int | None = folder_id
        while current:
            folder = self.folders[current]
            parts.append(folder.name)
            current = folder.parent_id
        return "/" + "/".join(reversed(parts))

    def remove_folder(self, path: str) -> None:
        """Delete the folder at `path` with everything under it."""
        top = self.folder_id_at(path)
        assert top is not None, f"no folder at {path}"
        doomed = {top}
        while True:
            below = {f.folder_id for f in self.folders.values() if f.parent_id in doomed}
            if below <= doomed:
                break
            doomed |= below
        for folder_id in doomed:
            self.folders.pop(folder_id, None)
        self.files = {k: f for k, f in self.files.items() if f.parent_id not in doomed}

    def fail(self, endpoint: str, key: str, failure: Failure) -> None:
        """Make the next calls to `endpoint` for `key` (folder or file id) fail."""
        self.failures[(endpoint, key)] = failure

    # Serving.

    def transport(self) -> httpx.MockTransport:
        """A transport answering as this account's API and content hosts."""
        return httpx.MockTransport(self.handle)

    def client(self) -> httpx.AsyncClient:
        """An HTTP client wired to this stub."""
        return httpx.AsyncClient(transport=self.transport())

    def handle(self, request: httpx.Request) -> httpx.Response:
        """Route one request."""
        endpoint = request.url.path.lstrip("/")
        if request.url.host == CONTENT_HOST:
            self.requests["content"] += 1
            return httpx.Response(200, content=self.files[int(endpoint.split("/")[-1])].content)
        self.requests[endpoint] += 1
        params = request.url.params
        key = params.get("folderid") or params.get("fileid") or params.get("path") or ""
        failure = self.failures.get((endpoint, key))
        if failure is not None and failure.times > 0:
            failure.times -= 1
            return httpx.Response(failure.status, json=failure.body, headers=failure.headers)
        if endpoint == "oauth2_token":
            return httpx.Response(
                200, json={"result": 0, "access_token": self.access_token, "uid": self.uid}
            )
        if request.headers.get("authorization") != f"Bearer {self.access_token}":
            return httpx.Response(200, json={"result": 2094, "error": "Invalid access_token."})
        if endpoint == "listfolder":
            return self._listfolder(params)
        if endpoint == "checksumfile":
            return self._checksum(int(params["fileid"]))
        if endpoint == "getfilelink":
            return httpx.Response(
                200,
                json={
                    "result": 0,
                    "hosts": [CONTENT_HOST],
                    "path": f"/dl/{params['fileid']}",
                },
            )
        return httpx.Response(200, json={"result": 2000, "error": f"unknown {endpoint}"})

    def _listfolder(self, params: httpx.QueryParams) -> httpx.Response:
        """Answer `listfolder` by folder id or path."""
        if "path" in params:
            folder_id = self.folder_id_at(params["path"])
            if folder_id is None:
                return httpx.Response(200, json={"result": 2005, "error": "Directory not found."})
        else:
            folder_id = int(params["folderid"])
            if folder_id not in self.folders:
                return httpx.Response(
                    200, json={"result": 2005, "error": "Directory does not exist."}
                )
        contents: list[dict[str, object]] = [
            {"isfolder": True, "folderid": f.folder_id, "name": f.name}
            for f in self.folders.values()
            if f.parent_id == folder_id
        ]
        if params.get("nofiles") != "1":
            contents += [
                {
                    "isfolder": False,
                    "fileid": f.file_id,
                    "name": f.name,
                    "size": len(f.content),
                    "modified": format_datetime(f.modified),
                    "hash": f.marker,
                }
                for f in self.files.values()
                if f.parent_id == folder_id
            ]
        return httpx.Response(
            200,
            json={
                "result": 0,
                "metadata": {"folderid": folder_id, "isfolder": True, "contents": contents},
            },
        )

    def folder_id_at(self, path: str) -> int | None:
        """The id of the folder at `path`, if it exists."""
        parent = 0
        for name in [part for part in path.split("/") if part]:
            found = next(
                (f for f in self.folders.values() if f.parent_id == parent and f.name == name),
                None,
            )
            if found is None:
                return None
            parent = found.folder_id
        return parent

    def _checksum(self, file_id: int) -> httpx.Response:
        """Answer `checksumfile` with this region's digests."""
        content = self.files[file_id].content
        body: dict[str, object] = {"result": 0, "sha1": hashlib.sha1(content).hexdigest()}
        if self.region == "eu":
            body["sha256"] = hashlib.sha256(content).hexdigest()
        else:
            body["md5"] = hashlib.md5(content).hexdigest()
        return httpx.Response(200, json=body)
