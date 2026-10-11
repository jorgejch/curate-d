"""Transfers for integration tests, through the local stack's rclone daemon.

Provider accounts are directories of the daemon's own filesystem: every source account is
one, the destination account `DESTINATION_ACCOUNT` another, and an account listed as
offline cannot be reached at all. The daemon's local backend reports every hash, so an
honest copy verifies on the common-hash rung. `ScriptedAccess` passes calls through to
the daemon, counting copies and injecting failures when told to.
"""

import asyncio
import contextlib
import os
from collections.abc import AsyncGenerator, Collection, Mapping
from pathlib import Path
from typing import Final
from uuid import uuid4

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine

from curate_d.classification.remote import RemoteClassifier
from curate_d.config import Settings
from curate_d.domain import Provider
from curate_d.placement.admission import TransferAdmission
from curate_d.placement.rclone import JobStatus, RcloneClient, RcloneError
from curate_d.placement.remotes import RcloneAccess
from curate_d.providers.errors import ProviderRequestError
from curate_d.runtime.services import StagingAccess
from tests.integration.pipeline_harness import Pipeline, running_pipeline

RCLONE_URL: Final = os.environ.get("CURATE_D_TEST_RCLONE_URL", "http://127.0.0.1:5572")
DESTINATION_ACCOUNT: Final = "destination"
_NO_SPACE: Final = -1


class WorkspaceRemotes:
    """Accounts as directories of one daemon workspace."""

    def __init__(self, workspace: str, *, offline: Collection[str] = ()) -> None:
        """Resolve into `workspace`; the `offline` accounts cannot be reached."""
        self.workspace = workspace
        self.offline = set(offline)

    async def fs_for(self, provider: Provider, account: str) -> str:
        """The directory standing in for the account.

        Raises:
            ProviderRequestError: the account is offline.
        """
        if account in self.offline:
            raise ProviderRequestError(provider, account, "configure remote", "unreachable")
        side = "destination" if account == DESTINATION_ACCOUNT else "source"
        return f"/{self.workspace}/{side}"


class ScriptedAccess(RcloneAccess):
    """The daemon, with copies counted and failures or pauses injected on request."""

    def __init__(self) -> None:
        """Pass every call through until told otherwise."""
        super().__init__(RCLONE_URL)
        self.copies = 0
        self.out_of_space = 0
        self.corrupt: str | None = None
        self.paused = asyncio.Event()
        self.crash: asyncio.Event | None = None

    def client(self) -> RcloneClient:
        """A client that consults this script."""
        inner = super().client()
        return _ScriptedClient(inner, self)


class _ScriptedClient(RcloneClient):
    """One loop's client, scripted by its `ScriptedAccess`."""

    def __init__(self, inner: RcloneClient, script: ScriptedAccess) -> None:
        """Wrap `inner`'s HTTP client."""
        super().__init__(inner._http, inner._base)
        self.script = script

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
        """Count the copy; crash the process, fail the copy, or start it."""
        self.script.copies += 1
        if self.script.crash is not None:
            self.script.paused.set()
            await self.script.crash.wait()
            # The process stops here: the step never completes and nothing is recorded.
            raise asyncio.CancelledError
        if self.script.out_of_space:
            self.script.out_of_space -= 1
            return _NO_SPACE
        return await super().start_copy(
            src_fs, src_remote, dst_fs, dst_remote, group=group, config=config
        )

    async def job_status(self, job: int) -> JobStatus:
        """A scripted failure, rclone's corruption report, or the real job's state."""
        if job == _NO_SPACE:
            return JobStatus(
                finished=True,
                success=False,
                error="write /scratch/.partial: no space left on device",
            )
        status = await super().job_status(job)
        if self.script.corrupt is not None and status.finished:
            return JobStatus(finished=True, success=False, error=self.script.corrupt)
        return status


@contextlib.asynccontextmanager
async def rclone_workspace() -> AsyncGenerator[tuple[RcloneClient, str], None]:
    """A client for the local daemon and a private directory on it, removed afterwards."""
    workspace = f"tmp/curate-placement-{uuid4().hex[:12]}"
    async with httpx.AsyncClient(timeout=30) as http:
        rclone = RcloneClient(http, RCLONE_URL)
        try:
            yield rclone, workspace
        finally:
            with contextlib.suppress(RcloneError):
                await rclone.call("operations/purge", {"fs": "/", "remote": workspace})


@contextlib.asynccontextmanager
async def transfer_pipeline(
    settings: Settings,
    engine: AsyncEngine,
    scratch: Path,
    remotes: WorkspaceRemotes,
    access: ScriptedAccess,
    *,
    listen: tuple[str, ...] | None = None,
    remote: RemoteClassifier | None = None,
    staging: StagingAccess | None = None,
    resume: Pipeline | None = None,
    **overrides: object,
) -> AsyncGenerator[Pipeline, None]:
    """A pipeline that transfers through `access` between `remotes`' directories.

    `remote`, `staging` and `resume` are as for `running_pipeline`; `overrides` replace
    settings fields.
    """
    async with running_pipeline(
        settings.model_copy(update=overrides),
        engine,
        scratch,
        listen=listen,
        remote=remote,
        staging=staging,
        resume=resume,
        transfers=(
            access,
            remotes,
            TransferAdmission(64 * 1024 * 1024, large_threshold=16 * 1024 * 1024),
        ),
        transfer_retry_seconds=0.01,
    ) as pipeline:
        yield pipeline
