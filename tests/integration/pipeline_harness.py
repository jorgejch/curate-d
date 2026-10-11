"""A running pipeline for integration tests: DBOS, the ledger, a stub pCloud, staging.

DBOS runs in the test process against the real ledger database and dequeues every queue,
so a scan's hand-offs actually run. Provider access goes to `StubPCloud` and staging is
in memory: what these tests exercise is the workflows, the ledger transactions and the
queues between them. The staging contract against RustFS and OVH has its own suite.

Each harness uses its own pCloud account id and its own content, so tests never share a
source or content object, and it deletes what it created when it closes. Audit entries
are append-only and stay.
"""

import asyncio
from collections.abc import AsyncGenerator, Coroutine
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final
from uuid import uuid4

import httpx
from dbos import DBOS
from pydantic import SecretStr
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from curate_d.classification.remote import RemoteClassifier
from curate_d.config import Settings
from curate_d.domain import ContentObject, Provider, SourceObject
from curate_d.ledger.mapping import to_content_object, to_source_object
from curate_d.ledger.tables import (
    classification_holds,
    classifications,
    content_objects,
    placements,
    remote_spend,
    review_items,
    scans,
    source_objects,
)
from curate_d.placement.admission import TransferAdmission
from curate_d.placement.remotes import RcloneAccess, RemoteResolver
from curate_d.providers.access import PacedFiles, ProviderFiles
from curate_d.providers.crypto import CredentialCipher, generate_key
from curate_d.providers.errors import ConnectionRequiresReauthenticationError
from curate_d.providers.pacing import Backoff, Pacer, Quota
from curate_d.providers.pcloud.files import PCloudFiles
from curate_d.runtime import services
from curate_d.runtime.datasource import create_ledger_datasource
from curate_d.runtime.dbos_app import configure
from curate_d.runtime.queues import register_all_async
from curate_d.runtime.services import StagingAccess
from curate_d.staging.factories import StagingReaderBackend, StagingWriterBackend
from curate_d.staging.snapshots import LocalScratchBudget
from tests.stubs.pcloud import StubPCloud
from tests.stubs.staging import MemoryStaging

SETTLE_TIMEOUT_SECONDS: Final = 60.0


class StubAccess:
    """Provider access to one stub account, paced as production access is."""

    def __init__(self, pcloud: StubPCloud) -> None:
        """Serve `pcloud`'s account."""
        self.pcloud = pcloud
        self.blocked = False
        self._pacer = Pacer(Quota(requests=10_000, period_seconds=1.0))
        self._http: httpx.AsyncClient | None = None

    async def files(self, provider: Provider, account: str) -> ProviderFiles:
        """A client for the stub account."""
        if self.blocked:
            raise ConnectionRequiresReauthenticationError(provider, account, "connect", "flagged")
        if self._http is None:
            self._http = self.pcloud.client()
        return PacedFiles(
            PCloudFiles(
                self._http,
                api_host=self.pcloud.host,
                account=account,
                access_token=SecretStr(self.pcloud.access_token),
            ),
            self._pacer,
            Backoff(initial_seconds=0.01, max_attempts=3),
        )

    async def aclose(self) -> None:
        """Close the HTTP client."""
        if self._http is not None:
            await self._http.aclose()


@dataclass
class DirectStaging:
    """Staging whose backends live on the test's own loop."""

    memory: MemoryStaging

    @property
    def writer(self) -> StagingWriterBackend | None:
        """The shared in-memory writer."""
        return self.memory.writer

    @property
    def reader(self) -> StagingReaderBackend | None:
        """The shared in-memory reader."""
        return self.memory.reader

    async def run[T](self, work: Coroutine[None, None, T]) -> T:
        """Await `work` here: there is only one loop."""
        return await work


@dataclass
class Pipeline:
    """A launched pipeline and what it is wired to."""

    settings: Settings
    engine: AsyncEngine
    pcloud: StubPCloud
    access: StubAccess
    staging: MemoryStaging
    account: str
    executor_id: str
    listened: tuple[str, ...] | None = None
    period: str = ""
    terminated: bool = False

    def terminate(self) -> None:
        """Stop like a killed worker when the block exits: no settling, no cleanup.

        Whatever was running stays PENDING under this executor; a pipeline resuming this
        one recovers it.
        """
        self.terminated = True

    async def sources(self) -> list[SourceObject]:
        """Every source object recorded for this pipeline's account, by path."""
        async with self.engine.connect() as connection:
            rows = await connection.execute(
                select(source_objects)
                .where(source_objects.c.account == self.account)
                .order_by(source_objects.c.path)
            )
            return [to_source_object(row) for row in rows.mappings()]

    async def contents(self) -> list[ContentObject]:
        """Content objects this account's sources resolve to."""
        ids = {s.content_object_id for s in await self.sources() if s.content_object_id}
        if not ids:
            return []
        async with self.engine.connect() as connection:
            rows = await connection.execute(
                select(content_objects).where(content_objects.c.id.in_(ids))
            )
            return [to_content_object(row) for row in rows.mappings()]

    async def workflows(self, prefix: str) -> list[tuple[str, str]]:
        """(workflow id, status) of this run's workflows whose ids start with `prefix`."""
        async with self.engine.connect() as connection:
            rows = await connection.execute(
                text(
                    "SELECT workflow_uuid, status FROM dbos.workflow_status "
                    "WHERE application_version = :v AND workflow_uuid LIKE :p "
                    "ORDER BY workflow_uuid"
                ),
                {"v": self.settings_version, "p": f"{prefix}%"},
            )
            return [(row[0], row[1]) for row in rows]

    async def settle(self) -> None:
        """Wait until no workflow this process would run is still queued or running.

        Only this pipeline's executor's workflows count: DBOS records the enqueuing
        executor on a queued workflow and the running one on a started workflow, and a
        workflow left behind by an earlier, crashed run belongs to an executor that is
        gone and would otherwise hold every later test up to the timeout.
        """
        deadline = asyncio.get_running_loop().time() + SETTLE_TIMEOUT_SECONDS
        listened = list(self.listened) if self.listened is not None else None
        while True:
            async with self.engine.connect() as connection:
                pending = await connection.scalar(
                    text(
                        "SELECT count(*) FROM dbos.workflow_status "
                        "WHERE status IN ('ENQUEUED', 'PENDING') AND application_version = :v "
                        "AND executor_id = :executor "
                        "AND (CAST(:queues AS text[]) IS NULL OR queue_name = ANY(:queues) "
                        "OR queue_name IS NULL)"
                    ),
                    {"v": self.settings_version, "queues": listened, "executor": self.executor_id},
                )
            if not pending:
                return
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError(f"{pending} workflows still pending after the timeout")
            await asyncio.sleep(0.1)

    @property
    def settings_version(self) -> str:
        """The application version this test run's workflows carry."""
        from curate_d.runtime import dbos_app

        return dbos_app.APPLICATION_VERSION


@asynccontextmanager
async def running_pipeline(
    settings: Settings,
    engine: AsyncEngine,
    scratch: Path,
    *,
    listen: tuple[str, ...] | None = None,
    staging_bytes: int = 10 * 1024 * 1024,
    remote: RemoteClassifier | None = None,
    transfers: tuple[RcloneAccess, RemoteResolver, TransferAdmission] | None = None,
    staging: StagingAccess | None = None,
    resume: Pipeline | None = None,
    **overrides: object,
) -> AsyncGenerator[Pipeline, None]:
    """Launch DBOS with the stub provider and in-memory staging installed.

    Args:
        settings: Base settings; `overrides` replace fields of it.
        engine: Ledger engine for assertions and cleanup.
        scratch: Directory for fetch scratch and snapshots.
        listen: Queues this process dequeues; all when None. Work enqueued elsewhere is
            cancelled when the pipeline closes, so no later test picks it up.
        staging_bytes: Capacity of the in-memory staging.
        remote: The remote classification tier; the tier is disabled when None.
        transfers: The rclone daemon, remote resolver and admission placement uses;
            this process cannot transfer when None.
        staging: Staging to install, such as the local RustFS engine's; the in-memory
            staging when None.
        resume: A terminated pipeline to restart: the same account, stub provider and
            executor, so DBOS recovers what it left running.
        overrides: Settings fields to replace.

    The clock runs in a year of its own, so the spend period this pipeline charges is
    one no other run or deployment shares, and can be deleted afterwards.
    """
    uid = int(resume.account) if resume is not None else int(uuid4().int % 10**9)
    pcloud = resume.pcloud if resume is not None else StubPCloud(uid=uid)
    access = StubAccess(pcloud)
    memory = (
        resume.staging
        if resume is not None
        else MemoryStaging(scratch / "snapshots", max_bytes=staging_bytes)
    )
    year = 3000 + uid % 6000
    started = datetime.now(UTC)
    epoch = datetime(year, 6, 1, tzinfo=UTC)
    configured = settings.model_copy(
        update={
            "openrouter_model": "stub/model" if remote is not None else None,
            "staging_local_scratch_root": scratch / "fetch",
            "provider_retry_seconds": 0.01,
            "staging_capacity_retry_seconds": 0.01,
            **overrides,
        }
    )
    executor_id = resume.executor_id if resume is not None else f"pipeline-{uid}"
    DBOS.destroy()
    configure(configured, executor_id=executor_id)
    datasource = await create_ledger_datasource(configured)
    services.install(
        services.Services(
            settings=configured,
            datasource=datasource,
            cipher=CredentialCipher(SecretStr(generate_key())),
            provider_access=access,
            staging=staging if staging is not None else DirectStaging(memory),
            scratch_budget=LocalScratchBudget(64 * 1024 * 1024),
            remote_classifier=remote,
            rclone=transfers[0] if transfers else None,
            remotes=transfers[1] if transfers else None,
            admission=transfers[2] if transfers else None,
            clock=lambda: epoch + (datetime.now(UTC) - started),
        )
    )
    if listen is not None:
        DBOS.listen_queues(list(listen))
    DBOS.launch()
    await register_all_async()
    pipeline = Pipeline(
        settings=configured,
        engine=engine,
        pcloud=pcloud,
        access=access,
        staging=memory,
        account=str(uid),
        executor_id=executor_id,
        listened=listen,
        period=f"{year}-06",
    )
    try:
        yield pipeline
    finally:
        if pipeline.terminated:
            DBOS.destroy()
            asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor())
            services.uninstall()
            await datasource.engine.dispose()
            await access.aclose()
        else:
            try:
                await pipeline.settle()
                for workflow in await DBOS.list_workflows_async(
                    status=["ENQUEUED"], app_version=pipeline.settings_version
                ):
                    await DBOS.cancel_workflow_async(workflow.workflow_id)
            finally:
                DBOS.destroy()
                # DBOS made its own thread pool the loop's default executor and has just shut
                # it down; a fresh one lets the same test start the pipeline again, which is
                # how a worker restart is tested.
                asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor())
                services.uninstall()
                # DBOS does not own the datasource's pool; left open, each test keeps a
                # connection and a long run exhausts the server's limit.
                await datasource.engine.dispose()
                await access.aclose()
                await _clean(engine, str(uid), f"{year}-06")


async def _clean(engine: AsyncEngine, account: str, period: str) -> None:
    """Delete what the account's run created, and the content only it referenced."""
    async with engine.begin() as connection:
        content_ids = [
            row[0]
            for row in await connection.execute(
                select(source_objects.c.content_object_id).where(
                    source_objects.c.account == account,
                    source_objects.c.content_object_id.is_not(None),
                )
            )
        ]
        await connection.execute(delete(remote_spend).where(remote_spend.c.period == period))
        if content_ids:
            for table in (placements, classifications, review_items, classification_holds):
                await connection.execute(
                    delete(table).where(table.c.content_object_id.in_(content_ids))
                )
        await connection.execute(delete(scans).where(scans.c.account == account))
        await connection.execute(delete(source_objects).where(source_objects.c.account == account))
        if content_ids:
            await connection.execute(
                delete(content_objects).where(
                    content_objects.c.id.in_(content_ids),
                    ~content_objects.c.id.in_(
                        select(source_objects.c.content_object_id).where(
                            source_objects.c.content_object_id.is_not(None)
                        )
                    ),
                )
            )
