"""What the control plane's endpoints read and change, one seam per area.

Each area's router depends on a protocol, not on the ledger: production wires the
ledger-backed implementations, and the isolated tests wire fakes, so an endpoint's
validation and error mapping are tested without a database.
"""

from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import cast

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncEngine

from curate_d.api.audit import AuditBackend, LedgerAudit
from curate_d.api.configuration import ConfigurationBackend, LedgerConfiguration
from curate_d.api.connections import ConnectionsBackend, ledger_connections
from curate_d.api.dry_runs import DryRunsBackend, LedgerDryRuns
from curate_d.api.jobs import JobsBackend, LedgerJobs
from curate_d.api.reclassification import through
from curate_d.api.review import LedgerReview, ReviewBackend
from curate_d.api.throughput import DbosThroughput, ThroughputBackend
from curate_d.config import Settings
from curate_d.runtime import dbos_app


@dataclass(frozen=True)
class Backends:
    """One backend per control-plane area."""

    connections: ConnectionsBackend
    configuration: ConfigurationBackend
    jobs: JobsBackend
    review: ReviewBackend
    audit: AuditBackend
    dry_runs: DryRunsBackend
    throughput: ThroughputBackend


type BackendsFactory = Callable[[Settings, AsyncEngine], AbstractAsyncContextManager[Backends]]


def of(request: Request) -> Backends:
    """The backends of the app serving `request`."""
    return cast(Backends, request.app.state.backends)


def connections(request: Request) -> ConnectionsBackend:
    """The connections backend."""
    return of(request).connections


def configuration(request: Request) -> ConfigurationBackend:
    """The configuration backend."""
    return of(request).configuration


def jobs(request: Request) -> JobsBackend:
    """The observability backend."""
    return of(request).jobs


def review(request: Request) -> ReviewBackend:
    """The review queue backend."""
    return of(request).review


def audit(request: Request) -> AuditBackend:
    """The audit backend."""
    return of(request).audit


def dry_runs(request: Request) -> DryRunsBackend:
    """The dry-run backend."""
    return of(request).dry_runs


def throughput(request: Request) -> ThroughputBackend:
    """The throughput backend."""
    return of(request).throughput


@asynccontextmanager
async def ledger_backends(
    settings: Settings, engine: AsyncEngine
) -> AsyncGenerator[Backends, None]:
    """The backends production uses, over the ledger and the workflow system database."""
    async with AsyncExitStack() as stack:
        workflows = dbos_app.client(settings)
        stack.callback(workflows.destroy)
        yield Backends(
            connections=await stack.enter_async_context(ledger_connections(settings, engine)),
            configuration=LedgerConfiguration(engine, reclassify=through(workflows)),
            jobs=LedgerJobs(settings, engine, workflows),
            review=LedgerReview(engine, reclassify=through(workflows)),
            audit=LedgerAudit(engine),
            dry_runs=LedgerDryRuns(engine, workflows),
            throughput=DbosThroughput(workflows),
        )
