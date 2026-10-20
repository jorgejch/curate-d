"""An app wired to in-memory backends, for the isolated endpoint tests."""

from collections.abc import AsyncGenerator, Iterator, Sequence
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from fastapi.testclient import TestClient
from pydantic import BaseModel, SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from curate_d.api.app import create_app
from curate_d.api.audit import History
from curate_d.api.backends import Backends
from curate_d.api.connections import StartedAuthorization
from curate_d.api.errors import NotFoundError, RejectedError
from curate_d.api.jobs import FailedUnit, Jobs, Progress, Spend
from curate_d.api.review import Entry, Promoted
from curate_d.api.throughput import LimitsChange, StageLimits, apply_change
from curate_d.config import Settings
from curate_d.configuration.store import ConfigurationVersion
from curate_d.domain import (
    SEED_POLICY,
    SEED_TAXONOMY,
    ConfigurationKind,
    ConnectionHealth,
    ConnectionState,
    ContentObjectId,
    Facts,
    Provider,
    RejectedValue,
    ReviewStatus,
)
from curate_d.rendering.templates import SEED_TEMPLATES
from curate_d.runtime.queues import QUEUE_SPECS, SPECS_BY_NAME, QueueName
from tests.unit.api.dry_run_fakes import FakeDryRuns as FakeDryRuns

_SEEDS: dict[ConfigurationKind, BaseModel] = {
    ConfigurationKind.TAXONOMY: SEED_TAXONOMY,
    ConfigurationKind.TEMPLATES: SEED_TEMPLATES,
    ConfigurationKind.POLICY: SEED_POLICY,
}


class StubEngine:
    """An engine that is never connected to."""

    async def dispose(self) -> None:
        """Nothing to release."""


@dataclass
class FakeConnections:
    """Connections held in memory; the nonce issued is always `nonce`."""

    connections: list[ConnectionHealth] = field(default_factory=list)
    nonce: str = "browser-nonce"
    failure: Exception | None = None

    async def health(self) -> Sequence[ConnectionHealth]:
        """The connections held."""
        return self.connections

    def begin(self, provider: Provider) -> StartedAuthorization:
        """A consent URL carrying the provider."""
        if self.failure is not None:
            raise self.failure
        return StartedAuthorization(
            redirect_url=f"https://consent.example/{provider.value}",
            browser_nonce=SecretStr(self.nonce),
        )

    async def complete(
        self,
        provider: Provider,
        *,
        code: str,
        state: str,
        hostname: str,
        browser_nonce: str | None,
    ) -> ConnectionHealth:
        """Store a healthy connection named after `code`, if the nonce matches."""
        if self.failure is not None:
            raise self.failure
        if browser_nonce != self.nonce:
            raise RejectedError("authorization was not completed by the browser that started it")
        health = ConnectionHealth(provider=provider, account=code, state=ConnectionState.HEALTHY)
        self.connections.append(health)
        return health


@dataclass
class FakeConfiguration:
    """Configuration versions in memory, each kind starting at its seed."""

    versions: dict[ConfigurationKind, list[tuple[BaseModel, str]]] = field(
        default_factory=lambda: {kind: [(seed, "seed")] for kind, seed in _SEEDS.items()}
    )
    rejection: str | None = None

    async def active(self, kind: ConfigurationKind) -> tuple[int, BaseModel]:
        """The newest version."""
        return len(self.versions[kind]), self.versions[kind][-1][0]

    async def activate(self, kind: ConfigurationKind, document: BaseModel, *, by: str) -> int:
        """Append a version, unless told to refuse."""
        if self.rejection is not None:
            raise RejectedError(self.rejection)
        self.versions[kind].append((document, by))
        return len(self.versions[kind])

    async def history(self, kind: ConfigurationKind) -> Sequence[ConfigurationVersion]:
        """Newest first."""
        count = len(self.versions[kind])
        return [
            ConfigurationVersion(
                kind=kind,
                version=number,
                active=number == count,
                created_at=datetime(2026, 9, 30, tzinfo=UTC),
                created_by=self.versions[kind][number - 1][1],
            )
            for number in range(count, 0, -1)
        ]


def idle_jobs() -> Jobs:
    """Nothing done, nothing spent."""
    return Jobs(
        scans=Progress(),
        fingerprinting=Progress(),
        classification=Progress(),
        placements=Progress(),
        queues={},
        spend=Spend(
            period="2026-09",
            spent_usd=Decimal(0),
            cap_usd=Decimal(10),
            calls=0,
            cap_reached=False,
        ),
    )


@dataclass
class FakeJobs:
    """Fixed counts and failures; records the limit asked for."""

    summary: Jobs = field(default_factory=idle_jobs)
    failed: list[FailedUnit] = field(default_factory=list)
    limits: list[int] = field(default_factory=list)

    async def jobs(self) -> Jobs:
        """The fixed counts."""
        return self.summary

    async def failures(self, *, limit: int) -> Sequence[FailedUnit]:
        """The fixed failures, up to `limit`."""
        self.limits.append(limit)
        return self.failed[:limit]


@dataclass
class FakeReview:
    """Review entries in memory; refuses everything with `refusal` when set."""

    entries: dict[UUID, Entry] = field(default_factory=dict)
    refusal: str | None = None
    decided_by: list[str] = field(default_factory=list)
    promoted: list[list[RejectedValue]] = field(default_factory=list)

    async def open_items(self, *, limit: int, offset: int) -> Sequence[Entry]:
        """Open entries, in insertion order."""
        open_ = [e for e in self.entries.values() if e.status is ReviewStatus.OPEN]
        return open_[offset : offset + limit]

    async def entry(self, item_id: UUID) -> Entry:
        """One entry."""
        if item_id not in self.entries:
            raise NotFoundError("review item", item_id)
        return self.entries[item_id]

    def _close(self, item_id: UUID, status: ReviewStatus, by: str, **changes: object) -> Entry:
        """Close an entry, unless refusing."""
        if self.refusal is not None:
            raise RejectedError(self.refusal)
        closed = self.entries[item_id].model_copy(update={"status": status, **changes})
        self.entries[item_id] = closed
        self.decided_by.append(by)
        return closed

    async def resolve(self, item_id: UUID, facts: Facts, *, by: str) -> Entry:
        """Close as resolved with `facts`."""
        await self.entry(item_id)
        return self._close(item_id, ReviewStatus.RESOLVED, by, facts=facts)

    async def promote(self, item_id: UUID, values: Sequence[RejectedValue], *, by: str) -> Promoted:
        """Taxonomy version 2; the entry stays open."""
        entry = await self.entry(item_id)
        if self.refusal is not None:
            raise RejectedError(self.refusal)
        self.decided_by.append(by)
        self.promoted.append(list(values))
        return Promoted(taxonomy_version=2, entry=entry)

    async def exclude(self, item_id: UUID, reason: str, *, by: str) -> Entry:
        """Close as excluded."""
        await self.entry(item_id)
        return self._close(item_id, ReviewStatus.EXCLUDED, by, resolution=f"excluded: {reason}")


@dataclass
class FakeAudit:
    """Histories by content object."""

    histories: dict[UUID, History] = field(default_factory=dict)

    async def history(self, content_object_id: ContentObjectId) -> History:
        """The history held, or not found."""
        if content_object_id not in self.histories:
            raise NotFoundError("content object", content_object_id)
        return self.histories[content_object_id]


@dataclass
class FakeThroughput:
    """Stage limits in memory, starting from the registered defaults."""

    stages: dict[QueueName, StageLimits] = field(
        default_factory=lambda: {
            spec.name: StageLimits(
                stage=spec.name,
                limit_driver=spec.limit_driver,
                partitioned=spec.partitioned,
                worker_concurrency=spec.worker_concurrency,
                partition_worker_concurrency=spec.partition_worker_concurrency,
            )
            for spec in QUEUE_SPECS
        }
    )

    async def limits(self) -> Sequence[StageLimits]:
        """Every stage."""
        return list(self.stages.values())

    async def change(self, stage: QueueName, change: LimitsChange) -> StageLimits:
        """Apply the change as the real backend checks it."""
        if stage not in self.stages:
            raise NotFoundError("registered queue", stage.value)
        self.stages[stage] = apply_change(SPECS_BY_NAME[stage], self.stages[stage], change)
        return self.stages[stage]


def backends() -> Backends:
    """Fresh fake backends."""
    return Backends(
        connections=FakeConnections(),
        configuration=FakeConfiguration(),
        jobs=FakeJobs(),
        review=FakeReview(),
        audit=FakeAudit(),
        dry_runs=FakeDryRuns(),
        throughput=FakeThroughput(),
    )


@contextmanager
def client(wired: Backends | None = None) -> Iterator[TestClient]:
    """A test client over `wired`, or fresh fakes."""
    chosen = wired if wired is not None else backends()

    @asynccontextmanager
    async def factory(_settings: Settings, _engine: AsyncEngine) -> AsyncGenerator[Backends, None]:
        """Hand out the chosen backends."""
        yield chosen

    app = create_app(
        Settings(),
        engine_factory=lambda _settings: StubEngine(),  # type: ignore[arg-type,return-value]
        backends_factory=factory,
    )
    with TestClient(app) as http:
        yield http
