"""Dry runs: start one, and export what it proposes for review in bulk.

A run is enqueued on the workers' `maintenance` queue under its own identity and
evaluates every content object; nothing reaches a destination. It evaluates with the
policy and template set in force, or with drafts submitted with it: a draft is parsed as
`PUT /configuration/{kind}` checks it, against the taxonomy in force, refused with 422
before anything is enqueued, and never stored as a configuration version, so a policy can
be tried before it is activated.

Its export lists each object with the facts derived for it, the locator proposed at each
destination and the rules that selected it, and, for what could not be resolved, the
reason and the values the taxonomy refused, which are what informs taxonomy refinement.
Every rule of the policy evaluated is counted by the content objects it selected, and
classified content no rule selects is listed as `unselected`.
"""

from collections.abc import Callable, Mapping, Sequence
from enum import StrEnum
from typing import Annotated, Final, Protocol, Self
from uuid import UUID, uuid4

from dbos import DBOSClient, EnqueueOptions, WorkflowStatus
from fastapi import APIRouter, Depends, Request, status
from pydantic import AwareDatetime, BaseModel, BeforeValidator, model_validator
from sqlalchemy.ext.asyncio import AsyncEngine

from curate_d.api.errors import NOT_FOUND, REJECTED, NotFoundError, RejectedError, problems
from curate_d.configuration.consistency import taxonomy_in_force
from curate_d.configuration.documents import parse_policy
from curate_d.configuration.store import ConfigurationError, ConfigurationRepository
from curate_d.domain import (
    ConfigurationKind,
    ContentObjectId,
    DestinationConfig,
    DryRunOutcome,
    Facts,
    PlacementPolicy,
    PolicySource,
    RejectedValue,
)
from curate_d.placement.dry_run import DryRun, DryRunItem, DryRunRequest, DryRunStore
from curate_d.rendering.templates import TemplateSet, parse_templates
from curate_d.runtime.queues import QueueName
from curate_d.runtime.workflow_ids import dry_run_workflow_id
from curate_d.workflows.dry_run import WORKFLOW_NAME, check_drafts


def _lax_destination(value: object) -> object:
    """Return a destination parsed from its JSON form."""
    if isinstance(value, Mapping):
        return DestinationConfig.model_validate(dict(value), strict=False)
    return value


def _lax_policy(value: object) -> object:
    """Return a draft policy parsed from its JSON form, as a submitted policy is."""
    return parse_policy(value) if isinstance(value, Mapping) else value


def _lax_templates(value: object) -> object:
    """Return a draft template set parsed from its JSON form, as a submitted set is."""
    return parse_templates(value) if isinstance(value, Mapping) else value


class DryRunOrder(BaseModel):
    """What to evaluate against: named destinations, a draft policy, or the policy in force.

    Named destinations ignore placement rules, so they cannot be combined with a draft
    policy. A draft template set replaces the one in force in either case.
    """

    destinations: list[Annotated[DestinationConfig, BeforeValidator(_lax_destination)]] = []
    policy: Annotated[PlacementPolicy, BeforeValidator(_lax_policy)] | None = None
    templates: Annotated[TemplateSet, BeforeValidator(_lax_templates)] | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        """Refuse a draft policy alongside named destinations.

        Raises:
            ValueError: both were given.
        """
        if self.policy is not None and self.destinations:
            raise ValueError(
                "give either destinations or a draft policy: named destinations ignore "
                "the policy's rules"
            )
        return self


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
    """Where a run stands, and once started, the configuration it evaluates with.

    `policy_source` says whether the rules came from the policy in force (whose version
    is `policy_version`), from a draft, or were bypassed by named destinations; it is
    None until the run starts, and for runs recorded before it was. `policy_rules` names
    the rules evaluated in policy order: a rule's index is its position there.
    """

    run_id: UUID
    state: RunState
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None
    template_version: str | None = None
    templates_draft: bool = False
    taxonomy_version: int | None = None
    policy_source: PolicySource | None = None
    policy_version: int | None = None
    policy_rules: list[str] = []
    detail: str | None = None
    """Why a run failed or was cancelled."""


class DryRunDrafts(BaseModel):
    """The draft documents a run evaluated with, as recorded; None where it used none."""

    policy: dict[str, object] | None = None
    templates: dict[str, object] | None = None


class ExportedRule(BaseModel):
    """A rule of the policy evaluated: its index in `policy_rules`, and its name.

    The index tells apart rules that share a name, which a policy may hold.
    """

    index: int
    name: str


class RuleCount(ExportedRule):
    """A rule of the policy evaluated, with how many content objects it selected."""

    count: int


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
    rules: list[ExportedRule] = []
    """The policy rules that selected this destination; empty for named destinations."""


class DryRunExport(BaseModel):
    """A run and everything it evaluated.

    `rule_counts` has one entry per rule of the policy evaluated, in policy order, with
    how many content objects it selected, so a rule that selects nothing shows as zero and
    two rules of one name are counted apart. Content no rule selects is counted under the
    `unselected` outcome.
    """

    run: DryRunStatus
    drafts: DryRunDrafts = DryRunDrafts()
    counts: dict[DryRunOutcome, int]
    rule_counts: list[RuleCount] = []
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
                "templates_draft": header.templates_draft,
                "taxonomy_version": header.taxonomy_version,
                "policy_source": header.policy_source,
                "policy_version": header.policy_version,
                "policy_rules": list(header.policy_rules),
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
    """Return the export row for an item."""
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
        rules=[ExportedRule(index=rule.index, name=rule.name) for rule in item.rules],
    )


def drafts_of(header: DryRun | None) -> DryRunDrafts:
    """Return the draft documents a run's header records."""
    if header is None:
        return DryRunDrafts()
    return DryRunDrafts(
        policy=None if header.policy_document is None else dict(header.policy_document),
        templates=None if header.templates_document is None else dict(header.templates_document),
    )


def export_of(
    run: DryRunStatus, items: Sequence[DryRunItem], drafts: DryRunDrafts | None = None
) -> DryRunExport:
    """Return a run's export, counted by outcome and by rule; partial until it finishes.

    A rule counts each content object it selected once, whatever the number of
    destinations it selected for it.
    """
    counts = dict.fromkeys(DryRunOutcome, 0)
    selected: dict[tuple[int, str], set[ContentObjectId]] = {
        (index, name): set() for index, name in enumerate(run.policy_rules)
    }
    for item in items:
        counts[item.outcome] += 1
        for rule in item.rules:
            selected.setdefault((rule.index, rule.name), set()).add(item.content_object_id)
    return DryRunExport(
        run=run,
        drafts=drafts or DryRunDrafts(),
        counts=counts,
        rule_counts=[
            RuleCount(index=index, name=name, count=len(selected[index, name]))
            for index, name in sorted(selected)
        ],
        items=[exported(item) for item in items],
    )


class DryRunsBackend(Protocol):
    """Starting dry runs and reading their results."""

    async def start(self, request: DryRunRequest) -> str:
        """Enqueue a run; return its workflow id.

        Raises:
            RejectedError: a draft in the request is invalid; nothing was enqueued.
        """
        ...

    async def status(self, run_id: UUID) -> DryRunStatus:
        """Where a run stands.

        Raises:
            NotFoundError: there is no such run.
        """
        ...

    async def items(self, run_id: UUID) -> Sequence[DryRunItem]:
        """Return a run's items, by content object and destination."""
        ...

    async def drafts(self, run_id: UUID) -> DryRunDrafts:
        """Return the draft documents a run evaluated with."""
        ...


def router(backend: Callable[[Request], DryRunsBackend]) -> APIRouter:
    """The dry-run endpoints over `backend`."""
    routes = APIRouter(prefix="/dry-runs", tags=["dry runs"])
    uses = Depends(backend)

    @routes.post(
        "",
        status_code=status.HTTP_202_ACCEPTED,
        response_model=DryRunStarted,
        responses=problems(REJECTED),
    )
    async def start(order: DryRunOrder, runs: Annotated[DryRunsBackend, uses]) -> DryRunStarted:
        """Start a dry run, with drafts if given; poll its status, then export it."""
        run_id = uuid4()
        workflow_id = await runs.start(
            DryRunRequest(
                run_id=run_id,
                destinations=tuple(order.destinations),
                policy=order.policy,
                templates=order.templates,
            )
        )
        return DryRunStarted(run_id=run_id, workflow_id=workflow_id)

    @routes.get("/{run_id}", response_model=DryRunStatus, responses=problems(NOT_FOUND))
    async def get_status(run_id: UUID, runs: Annotated[DryRunsBackend, uses]) -> DryRunStatus:
        """Whether the run has finished, and what it evaluated with."""
        return await runs.status(run_id)

    @routes.get("/{run_id}/export", response_model=DryRunExport, responses=problems(NOT_FOUND))
    async def export(run_id: UUID, runs: Annotated[DryRunsBackend, uses]) -> DryRunExport:
        """Return every evaluated object with its facts, locators, rules and unresolved reasons."""
        run = await runs.status(run_id)
        return export_of(run, await runs.items(run_id), await runs.drafts(run_id))

    return routes


class LedgerDryRuns:
    """Dry runs enqueued through the workflow system database, read from the ledger."""

    def __init__(self, engine: AsyncEngine, workflows: DBOSClient) -> None:
        """Bind the ledger and the workflow client."""
        self._engine = engine
        self._workflows = workflows

    async def start(self, request: DryRunRequest) -> str:
        """Validate any drafts, then enqueue the run under its own identity.

        A draft passes the checks activating it would, against the taxonomy in force read
        under the taxonomy's shared lock. The lock is released before the enqueue: nothing
        is stored, and the run records the taxonomy version it evaluates with. A repeat
        under the same run id collapses onto the first.

        Raises:
            RejectedError: a draft is invalid, or the taxonomy in force does not read;
                nothing was enqueued.
        """
        if request.policy is not None or request.templates is not None:
            await self._check_drafts(request)
        workflow_id = dry_run_workflow_id(str(request.run_id))
        options: EnqueueOptions = {
            "workflow_name": WORKFLOW_NAME,
            "queue_name": QueueName.MAINTENANCE.value,
            "workflow_id": workflow_id,
        }
        await self._workflows.enqueue_async(options, request)
        return workflow_id

    async def _check_drafts(self, request: DryRunRequest) -> None:
        """Refuse a draft that `PUT /configuration/{kind}` would refuse.

        Raises:
            RejectedError: a draft is invalid, or the taxonomy in force does not read.
        """
        try:
            async with self._engine.begin() as connection:
                configurations = ConfigurationRepository(connection)
                await configurations.lock_shared(ConfigurationKind.TAXONOMY)
                taxonomy = await taxonomy_in_force(configurations)
            check_drafts(request, taxonomy)
        except ConfigurationError as error:
            raise RejectedError(str(error)) from None

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
        """Return a run's items."""
        async with self._engine.connect() as connection:
            return await DryRunStore(connection).items(run_id)

    async def drafts(self, run_id: UUID) -> DryRunDrafts:
        """Return the draft documents the run's header records."""
        async with self._engine.connect() as connection:
            return drafts_of(await DryRunStore(connection).get(run_id))
