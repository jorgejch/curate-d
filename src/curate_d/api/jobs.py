"""What the system is doing: work by state for each stage, and why units failed.

Each stage's states are reported as recorded, and folded into pending, in progress,
completed and failed by a fixed mapping per stage, so the four numbers mean the same
thing everywhere. Awaiting review counts as failed classification: the pipeline could
not decide, and a person has to.

The remote classification tier's spend is reported for the current month against its
cap; reaching the cap is reported here, with when it happened.
"""

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Final, Protocol
from uuid import UUID

from dbos import DBOSClient, WorkflowStatus
from fastapi import APIRouter, Depends, Query, Request
from pydantic import AwareDatetime, BaseModel
from sqlalchemy import ColumnElement, func, select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from curate_d.classification.gating import spend_period
from curate_d.classification.store import SpendRepository, SpendStatus
from curate_d.config import Settings
from curate_d.domain import PlacementState, ReviewStatus, ScanStatus, SourceObjectState
from curate_d.ledger.tables import (
    classification_holds,
    classifications,
    content_objects,
    destinations,
    placements,
    review_items,
    scans,
    source_objects,
)

_FAILED_WORKFLOWS: Final = ("ERROR", "CANCELLED", "MAX_RECOVERY_ATTEMPTS_EXCEEDED")
_FAILED_PLACEMENTS: Final = (PlacementState.FAILED, PlacementState.UNTRUSTED, PlacementState.ABSENT)


class Bucket(StrEnum):
    """Where a state falls, whatever the stage."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


class ClassificationState(StrEnum):
    """Where a content object stands in classification."""

    UNCLASSIFIED = "unclassified"
    HELD = "held"
    AWAITING_REVIEW = "awaiting_review"
    CLASSIFIED = "classified"


SCAN_BUCKETS: Final[Mapping[str, Bucket]] = {
    ScanStatus.RUNNING: Bucket.IN_PROGRESS,
    ScanStatus.INCOMPLETE: Bucket.FAILED,
    ScanStatus.COMPLETE: Bucket.COMPLETED,
}
SOURCE_BUCKETS: Final[Mapping[str, Bucket]] = {
    SourceObjectState.DISCOVERED: Bucket.PENDING,
    SourceObjectState.FINGERPRINTED: Bucket.COMPLETED,
    SourceObjectState.EXCLUDED: Bucket.COMPLETED,
    SourceObjectState.FAILED: Bucket.FAILED,
}
CLASSIFICATION_BUCKETS: Final[Mapping[str, Bucket]] = {
    ClassificationState.UNCLASSIFIED: Bucket.PENDING,
    ClassificationState.HELD: Bucket.PENDING,
    ClassificationState.AWAITING_REVIEW: Bucket.FAILED,
    ClassificationState.CLASSIFIED: Bucket.COMPLETED,
}
PLACEMENT_BUCKETS: Final[Mapping[str, Bucket]] = {
    PlacementState.PENDING: Bucket.PENDING,
    PlacementState.COPYING: Bucket.IN_PROGRESS,
    PlacementState.VERIFYING: Bucket.IN_PROGRESS,
    PlacementState.PRESENT: Bucket.COMPLETED,
    PlacementState.ABSENT: Bucket.FAILED,
    PlacementState.FAILED: Bucket.FAILED,
    PlacementState.UNTRUSTED: Bucket.FAILED,
}
WORKFLOW_BUCKETS: Final[Mapping[str, Bucket]] = {
    "ENQUEUED": Bucket.PENDING,
    "DELAYED": Bucket.PENDING,
    "PENDING": Bucket.IN_PROGRESS,
    "SUCCESS": Bucket.COMPLETED,
    **dict.fromkeys(_FAILED_WORKFLOWS, Bucket.FAILED),
}


class Progress(BaseModel):
    """One stage's work, by bucket and by the state recorded."""

    pending: int = 0
    in_progress: int = 0
    completed: int = 0
    failed: int = 0
    by_state: dict[str, int] = {}


def progress(counts: Mapping[str, int], buckets: Mapping[str, Bucket]) -> Progress:
    """Fold recorded states into buckets; a state the mapping does not know is pending."""
    totals = dict.fromkeys(Bucket, 0)
    for state, count in counts.items():
        totals[buckets.get(state, Bucket.PENDING)] += count
    return Progress(
        **{bucket.value: total for bucket, total in totals.items()},
        by_state={state: count for state, count in sorted(counts.items()) if count},
    )


class Spend(BaseModel):
    """This month's remote classification spend against its cap."""

    period: str
    spent_usd: Decimal
    cap_usd: Decimal
    calls: int
    cap_reached: bool
    cap_reached_at: AwareDatetime | None = None


class Jobs(BaseModel):
    """Work by stage, workflows by queue, and the spend cap."""

    scans: Progress
    fingerprinting: Progress
    classification: Progress
    placements: Progress
    queues: dict[str, Progress]
    spend: Spend


class Stage(StrEnum):
    """The stage a failed unit belongs to."""

    SCAN = "scan"
    FINGERPRINTING = "fingerprinting"
    CLASSIFICATION = "classification"
    PLACEMENT = "placement"
    WORKFLOW = "workflow"


class FailedUnit(BaseModel):
    """One unit of work that failed, what it concerned, and why."""

    stage: Stage
    reason: str
    where: str | None = None
    """The path, destination and locator, scan root or queue it concerned. A placement's
    is "name@provider:account:locator": the name is the policy's label, and the provider
    and account say where the copy is."""
    content_object_id: UUID | None = None
    placement_id: UUID | None = None
    source_object_id: UUID | None = None
    scan_id: UUID | None = None
    workflow_id: str | None = None
    failed_at: AwareDatetime | None = None


def classification_states(
    *, total: int, classified: int, reviewing: int, held: int
) -> dict[str, int]:
    """Content objects by where they stand in classification; the rest are unclassified."""
    return {
        ClassificationState.CLASSIFIED: classified,
        ClassificationState.AWAITING_REVIEW: reviewing,
        ClassificationState.HELD: held,
        ClassificationState.UNCLASSIFIED: max(total - classified - reviewing - held, 0),
    }


def spend_report(period: str, status: SpendStatus | None, *, cap: Decimal) -> Spend:
    """A period's spend, or nothing spent yet against the configured cap."""
    if status is None:
        return Spend(period=period, spent_usd=Decimal(0), cap_usd=cap, calls=0, cap_reached=False)
    return Spend(
        period=period,
        spent_usd=status.spent_usd,
        cap_usd=status.cap_usd,
        calls=status.calls,
        cap_reached=status.cap_reached_at is not None,
        cap_reached_at=status.cap_reached_at,
    )


def workflow_failure(status: WorkflowStatus) -> FailedUnit:
    """A failed workflow, its decoded error, and where it was queued."""
    return FailedUnit(
        stage=Stage.WORKFLOW,
        reason=f"{status.status}: {status.error}" if status.error else status.status,
        where=f"{status.queue_name or '(unqueued)'}: {status.name}",
        workflow_id=status.workflow_id,
        failed_at=None
        if status.updated_at is None
        else datetime.fromtimestamp(status.updated_at / 1000, tz=UTC),
    )


class JobsBackend(Protocol):
    """Counts and failures across the pipeline."""

    async def jobs(self) -> Jobs:
        """Work by stage now."""
        ...

    async def failures(self, *, limit: int) -> Sequence[FailedUnit]:
        """Up to `limit` failed units of each stage."""
        ...


def router(backend: Callable[[Request], JobsBackend]) -> APIRouter:
    """The observability endpoints over `backend`."""
    routes = APIRouter(prefix="/jobs", tags=["jobs"])
    uses = Depends(backend)

    @routes.get("", response_model=Jobs)
    async def get_jobs(jobs: Annotated[JobsBackend, uses]) -> Jobs:
        """Work by stage and by queue, and this month's spend against the cap."""
        return await jobs.jobs()

    @routes.get("/failures", response_model=list[FailedUnit])
    async def get_failures(
        jobs: Annotated[JobsBackend, uses],
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    ) -> Sequence[FailedUnit]:
        """What failed, which content object, placement or scan it concerned, and why."""
        return await jobs.failures(limit=limit)

    return routes


async def _counts(connection: AsyncConnection, column: ColumnElement[str]) -> dict[str, int]:
    """Rows by the value of a state column."""
    rows = await connection.execute(select(column, func.count()).group_by(column))
    return {str(state): int(count) for state, count in rows}


class LedgerJobs:
    """Counts and failures from the ledger and the workflow system database."""

    def __init__(
        self,
        settings: Settings,
        engine: AsyncEngine,
        workflows: DBOSClient,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        """Bind the settings, ledger, workflow client and clock."""
        self._settings = settings
        self._engine = engine
        self._workflows = workflows
        self._clock = clock

    async def jobs(self) -> Jobs:
        """Work by stage now."""
        async with self._engine.connect() as connection:
            scan_counts = await _counts(connection, scans.c.status)
            source_counts = await _counts(connection, source_objects.c.state)
            placement_counts = await _counts(connection, placements.c.state)
            classification_counts = await self._classification(connection)
            queues = await self._queues(connection)
            spend = await self._spend(connection)
        return Jobs(
            scans=progress(scan_counts, SCAN_BUCKETS),
            fingerprinting=progress(source_counts, SOURCE_BUCKETS),
            classification=progress(classification_counts, CLASSIFICATION_BUCKETS),
            placements=progress(placement_counts, PLACEMENT_BUCKETS),
            queues={queue: progress(counts, WORKFLOW_BUCKETS) for queue, counts in queues.items()},
            spend=spend,
        )

    async def _classification(self, connection: AsyncConnection) -> dict[str, int]:
        """Content objects by where they stand in classification."""
        total = int(await connection.scalar(select(func.count()).select_from(content_objects)) or 0)
        classified = int(
            await connection.scalar(select(func.count()).select_from(classifications)) or 0
        )
        reviewing = int(
            await connection.scalar(
                select(func.count(func.distinct(review_items.c.content_object_id))).where(
                    review_items.c.status == ReviewStatus.OPEN.value
                )
            )
            or 0
        )
        held = int(
            await connection.scalar(select(func.count()).select_from(classification_holds)) or 0
        )
        return classification_states(
            total=total, classified=classified, reviewing=reviewing, held=held
        )

    async def _queues(self, connection: AsyncConnection) -> dict[str, dict[str, int]]:
        """Workflows by queue and status, over every partition and application version."""
        rows = await connection.execute(
            text(
                "SELECT queue_name, status, sum(workflow_count) FROM workflow_status_by_queue "
                "GROUP BY queue_name, status"
            )
        )
        queues: dict[str, dict[str, int]] = {}
        for queue, state, count in rows:
            queues.setdefault(str(queue), {})[str(state)] = int(count)
        return queues

    async def _spend(self, connection: AsyncConnection) -> Spend:
        """This month's spend against its cap."""
        period = spend_period(self._clock())
        status = await SpendRepository(connection).status(period)
        return spend_report(period, status, cap=self._settings.remote_spend_cap_usd)

    async def failures(self, *, limit: int) -> Sequence[FailedUnit]:
        """Up to `limit` failed units of each stage, newest first."""
        async with self._engine.connect() as connection:
            found = [
                *await self._failed_scans(connection, limit),
                *await self._failed_sources(connection, limit),
                *await self._reviews(connection, limit),
                *await self._failed_placements(connection, limit),
            ]
        return [*found, *await self._failed_workflows(limit)]

    async def _failed_scans(self, connection: AsyncConnection, limit: int) -> list[FailedUnit]:
        """Scans stopped incomplete."""
        rows = await connection.execute(
            select(
                scans.c.id,
                scans.c.provider,
                scans.c.account,
                scans.c.root,
                scans.c.failure_reason,
                scans.c.started_at,
            )
            .where(scans.c.status == ScanStatus.INCOMPLETE.value)
            .order_by(scans.c.started_at.desc())
            .limit(limit)
        )
        return [
            FailedUnit(
                stage=Stage.SCAN,
                reason=row.failure_reason or "stopped incomplete",
                where=f"{row.provider} account {row.account}: {row.root}",
                scan_id=row.id,
                failed_at=row.started_at,
            )
            for row in rows
        ]

    async def _failed_sources(self, connection: AsyncConnection, limit: int) -> list[FailedUnit]:
        """Source objects that could not be read or fingerprinted."""
        rows = await connection.execute(
            select(
                source_objects.c.id,
                source_objects.c.provider,
                source_objects.c.account,
                source_objects.c.path,
                source_objects.c.failure_reason,
            )
            .where(source_objects.c.state == SourceObjectState.FAILED.value)
            .order_by(source_objects.c.first_seen_at.desc())
            .limit(limit)
        )
        return [
            FailedUnit(
                stage=Stage.FINGERPRINTING,
                reason=row.failure_reason or "failed",
                where=f"{row.provider} account {row.account}: {row.path}",
                source_object_id=row.id,
            )
            for row in rows
        ]

    async def _reviews(self, connection: AsyncConnection, limit: int) -> list[FailedUnit]:
        """Content the pipeline could not classify or render, waiting for a person."""
        rows = await connection.execute(
            select(
                review_items.c.content_object_id,
                review_items.c.reason,
                review_items.c.detail,
                review_items.c.created_at,
            )
            .where(review_items.c.status == ReviewStatus.OPEN.value)
            .order_by(review_items.c.created_at.desc())
            .limit(limit)
        )
        return [
            FailedUnit(
                stage=Stage.CLASSIFICATION,
                reason=f"{row.reason}: {row.detail}",
                content_object_id=row.content_object_id,
                failed_at=row.created_at,
            )
            for row in rows
        ]

    async def _failed_placements(self, connection: AsyncConnection, limit: int) -> list[FailedUnit]:
        """Placements that failed, were found untrustworthy, or went missing."""
        rows = await connection.execute(
            select(
                placements.c.id,
                placements.c.content_object_id,
                placements.c.destination_name,
                destinations.c.provider,
                destinations.c.account,
                placements.c.locator,
                placements.c.state,
                placements.c.failure_reason,
            )
            .join_from(placements, destinations, placements.c.destination_id == destinations.c.id)
            .where(placements.c.state.in_([state.value for state in _FAILED_PLACEMENTS]))
            .order_by(placements.c.created_at.desc())
            .limit(limit)
        )
        return [
            FailedUnit(
                stage=Stage.PLACEMENT,
                reason=f"{row.state}: {row.failure_reason or 'no reason recorded'}",
                where=f"{row.destination_name}@{row.provider}:{row.account}:{row.locator}",
                content_object_id=row.content_object_id,
                placement_id=row.id,
            )
            for row in rows
        ]

    async def _failed_workflows(self, limit: int) -> list[FailedUnit]:
        """Workflows that ended in error, were cancelled, or exhausted recovery."""
        statuses = await self._workflows.list_workflows_async(
            status=list(_FAILED_WORKFLOWS), limit=limit, sort_desc=True, load_input=False
        )
        return [workflow_failure(status) for status in statuses]
