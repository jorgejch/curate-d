"""Dry runs: start one, and export what it proposes for review in bulk.

A run is enqueued on the workers' `maintenance` queue under its own identity and
evaluates every content object; nothing reaches a destination. Its export lists each
object with the facts derived for it, the locator proposed at each destination, and, for
what could not be resolved, the reason and the values the taxonomy refused, which are
what informs taxonomy refinement.
"""

from collections.abc import Callable, Mapping, Sequence
from enum import StrEnum
from typing import Annotated, Final, Protocol
from uuid import UUID, uuid4

from dbos import DBOSClient, EnqueueOptions, WorkflowStatus
from fastapi import APIRouter, Depends, Request, status
from pydantic import AwareDatetime, BaseModel, BeforeValidator
from sqlalchemy.ext.asyncio import AsyncEngine

from curate_d.api.errors import NOT_FOUND, NotFoundError, problems
from curate_d.domain import (
    ContentObjectId,
    DestinationConfig,
    DryRunOutcome,
    Facts,
    RejectedValue,
)
from curate_d.placement.dry_run import DryRun, DryRunItem, DryRunRequest, DryRunStore
from curate_d.runtime.queues import QueueName
from curate_d.runtime.workflow_ids import dry_run_workflow_id
from curate_d.workflows.dry_run import WORKFLOW_NAME


def _lax_destination(value: object) -> object:
    """Parse a destination from its JSON form."""
    if isinstance(value, Mapping):
        return DestinationConfig.model_validate(dict(value), strict=False)
    return value


class DryRunOrder(BaseModel):
    """What to evaluate against: these destinations, or the policy's rules when empty."""

    destinations: list[Annotated[DestinationConfig, BeforeValidator(_lax_destination)]] = []


class DryRunStarted(BaseModel):
    """The run's identity, and the workflow carrying it out."""

    run_id: UUID
    workflow_id: str


class RunState(StrEnum):
    """Where a run stands. Finished, failed and cancelled are final."""

    QUEUED = "queued"
    RUNNING = "running"
    FINISHED = "finished"
    FAILED = "failed"
    CANCELLED = "cancelled"


class DryRunStatus(BaseModel):
    """Where a run stands, and once started, the configuration it evaluates with."""

    run_id: UUID
    state: RunState
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None
    template_version: str | None = None
    taxonomy_version: int | None = None
    detail: str | None = None
    """Why a run failed or was cancelled."""


class ExportedItem(BaseModel):
    """One content object at one destination, as the run evaluated it."""

    content_object_id: ContentObjectId
    destination: str
    outcome: DryRunOutcome
    paths: list[str]
    facts: Facts | None = None
    locator: str | None = None
    expected_verification: str | None = None
    reason: str | None = None
    rejected_values: list[RejectedValue] = []


class DryRunExport(BaseModel):
    """A run and everything it evaluated."""

    run: DryRunStatus
    counts: dict[DryRunOutcome, int]
    items: list[ExportedItem]


_QUEUED: Final = frozenset({"ENQUEUED", "DELAYED"})
_FAILED: Final = frozenset({"ERROR", "MAX_RECOVERY_ATTEMPTS_EXCEEDED"})


def status_of(run_id: UUID, header: DryRun | None, workflow: WorkflowStatus | None) -> DryRunStatus:
    """Where a run stands, from its recorded header and its workflow's state.

    A finished header is final. Otherwise the workflow says whether the run is waiting,
    running, failed or cancelled; an unfinished run whose workflow is no longer recorded
    has stopped without finishing.

    Raises:
        NotFoundError: there is neither a header nor a workflow.
    """
    if header is None and workflow is None:
        raise NotFoundError("dry run", run_id)
    status = DryRunStatus(run_id=run_id, state=RunState.RUNNING)
    if header is not None:
        status = status.model_copy(
            update={
                "started_at": header.started_at,
                "finished_at": header.finished_at,
                "template_version": header.template_version,
                "taxonomy_version": header.taxonomy_version,
            }
        )
        if header.finished_at is not None:
            return status.model_copy(update={"state": RunState.FINISHED})
    if workflow is None:
        return status.model_copy(
            update={
                "state": RunState.FAILED,
                "detail": "stopped without finishing; its workflow is no longer recorded",
            }
        )
    if workflow.status in _FAILED:
        detail = f"{workflow.status}: {workflow.error}" if workflow.error else workflow.status
        return status.model_copy(update={"state": RunState.FAILED, "detail": detail})
    if workflow.status == "CANCELLED":
        return status.model_copy(update={"state": RunState.CANCELLED, "detail": "cancelled"})
    if workflow.status in _QUEUED and header is None:
        return status.model_copy(update={"state": RunState.QUEUED})
    return status


def exported(item: DryRunItem) -> ExportedItem:
    """The export row for an item."""
    return ExportedItem(
        content_object_id=item.content_object_id,
        destination=item.destination_name,
        outcome=item.outcome,
        paths=list(item.paths),
        facts=item.facts,
        locator=item.locator,
        expected_verification=None
        if item.expected_level is None
        else item.expected_level.name.lower(),
        reason=item.reason,
        rejected_values=list(item.rejected_values),
    )


def export_of(run: DryRunStatus, items: Sequence[DryRunItem]) -> DryRunExport:
    """A run's export, with its items counted by outcome; partial until it finishes."""
    counts = dict.fromkeys(DryRunOutcome, 0)
    for item in items:
        counts[item.outcome] += 1
    return DryRunExport(run=run, counts=counts, items=[exported(item) for item in items])


class DryRunsBackend(Protocol):
    """Starting dry runs and reading their results."""

    async def start(self, request: DryRunRequest) -> str:
        """Enqueue a run; return its workflow id."""
        ...

    async def status(self, run_id: UUID) -> DryRunStatus:
        """Where a run stands.

        Raises:
            NotFoundError: there is no such run.
        """
        ...

    async def items(self, run_id: UUID) -> Sequence[DryRunItem]:
        """A run's items, by content object and destination."""
        ...


def router(backend: Callable[[Request], DryRunsBackend]) -> APIRouter:
    """The dry-run endpoints over `backend`."""
    routes = APIRouter(prefix="/dry-runs", tags=["dry runs"])
    uses = Depends(backend)

    @routes.post("", status_code=status.HTTP_202_ACCEPTED, response_model=DryRunStarted)
    async def start(order: DryRunOrder, runs: Annotated[DryRunsBackend, uses]) -> DryRunStarted:
        """Start a dry run; poll its status, then export it."""
        run_id = uuid4()
        workflow_id = await runs.start(
            DryRunRequest(run_id=run_id, destinations=tuple(order.destinations))
        )
        return DryRunStarted(run_id=run_id, workflow_id=workflow_id)

    @routes.get("/{run_id}", response_model=DryRunStatus, responses=problems(NOT_FOUND))
    async def get_status(run_id: UUID, runs: Annotated[DryRunsBackend, uses]) -> DryRunStatus:
        """Whether the run has finished, and what it evaluated with."""
        return await runs.status(run_id)

    @routes.get("/{run_id}/export", response_model=DryRunExport, responses=problems(NOT_FOUND))
    async def export(run_id: UUID, runs: Annotated[DryRunsBackend, uses]) -> DryRunExport:
        """Every evaluated object with its facts, proposed locators and unresolved reasons."""
        run = await runs.status(run_id)
        return export_of(run, await runs.items(run_id))

    return routes


class LedgerDryRuns:
    """Dry runs enqueued through the workflow system database, read from the ledger."""

    def __init__(self, engine: AsyncEngine, workflows: DBOSClient) -> None:
        """Bind the ledger and the workflow client."""
        self._engine = engine
        self._workflows = workflows

    async def start(self, request: DryRunRequest) -> str:
        """Enqueue the run under its own identity, so a repeat collapses."""
        workflow_id = dry_run_workflow_id(str(request.run_id))
        options: EnqueueOptions = {
            "workflow_name": WORKFLOW_NAME,
            "queue_name": QueueName.MAINTENANCE.value,
            "workflow_id": workflow_id,
        }
        await self._workflows.enqueue_async(options, request)
        return workflow_id

    async def status(self, run_id: UUID) -> DryRunStatus:
        """The run's header and its workflow's state, combined."""
        async with self._engine.connect() as connection:
            header = await DryRunStore(connection).get(run_id)
        if header is not None and header.finished_at is not None:
            return status_of(run_id, header, None)
        workflows = await self._workflows.list_workflows_async(
            workflow_ids=[dry_run_workflow_id(str(run_id))], load_input=False
        )
        return status_of(run_id, header, workflows[0] if workflows else None)

    async def items(self, run_id: UUID) -> Sequence[DryRunItem]:
        """A run's items."""
        async with self._engine.connect() as connection:
            return await DryRunStore(connection).items(run_id)
