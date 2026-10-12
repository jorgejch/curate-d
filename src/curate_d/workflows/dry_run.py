"""The dry-run workflow: evaluate every content object, persist what placement would do.

Runs on the `maintenance` queue. The run's header is written first, pinning the template
set, taxonomy and policy it evaluates with: the versions in force, or the drafts the
request carries, which are recorded on the header and never stored as configuration
versions. Then content is evaluated a page at a time, each page read in a step and its
items written in one transaction, and the header is closed. A page that fails midway is
evaluated again on recovery and its items replaced. Nothing outside dry_runs and
dry_run_items is written.
"""

from datetime import datetime
from typing import Final
from uuid import UUID

from dbos import DBOS, SetWorkflowID

from curate_d.classification.taxonomy import active_taxonomy
from curate_d.configuration.store import ConfigurationRepository
from curate_d.domain import ContentObjectId, DomainModel, PlacementPolicy, PolicySource
from curate_d.placement.dry_run import (
    DryRun,
    DryRunItem,
    DryRunRequest,
    DryRunStore,
    Evaluator,
    content_page,
)
from curate_d.placement.policy import active_policy
from curate_d.rendering.planning import active_templates
from curate_d.rendering.templates import TemplateSet
from curate_d.runtime.queues import QueueName
from curate_d.runtime.services import current
from curate_d.runtime.workflow_ids import dry_run_workflow_id

# Registered under this name, so a process that does not run workflows can enqueue one.
WORKFLOW_NAME: Final = "dry_run"


class DryRunContext(DomainModel):
    """What one run evaluates with, fixed when it starts."""

    templates: TemplateSet
    policy: PlacementPolicy


class DryRunPage(DomainModel):
    """One page's items, and where the next page starts."""

    items: tuple[DryRunItem, ...]
    last: ContentObjectId | None


async def start_dry_run(request: DryRunRequest) -> str:
    """Enqueue a dry run and return its workflow id; a repeated request collapses."""
    workflow_id = dry_run_workflow_id(str(request.run_id))
    with SetWorkflowID(workflow_id):
        await DBOS.enqueue_workflow_async(QueueName.MAINTENANCE.value, dry_run, request)
    return workflow_id


# Steps.


@DBOS.step()
async def _dry_run_now_step() -> datetime:
    """Read the clock once, so a replay records the same time."""
    return current().clock()


@DBOS.step()
async def _dry_run_page_step(
    request: DryRunRequest, context: DryRunContext, after: ContentObjectId | None
) -> DryRunPage:
    """Evaluate the next page of content objects, reading the ledger only."""
    async with current().datasource.engine.connect() as connection:
        ids = await content_page(connection, after=after)
        evaluator = Evaluator(
            connection,
            run_id=request.run_id,
            templates=context.templates,
            policy=context.policy,
            destinations=request.destinations,
        )
        items = [item for content_id in ids for item in await evaluator.evaluate(content_id)]
    return DryRunPage(items=tuple(items), last=ids[-1] if ids else None)


# Transactions.


async def _open_tx(request: DryRunRequest, now: datetime) -> DryRunContext:
    """Record the run's header with the template set, taxonomy and policy it evaluates with.

    A draft in the request takes the place of the version in force and is recorded on the
    header; the versions in force are only read.
    """
    connection = await current().datasource.sql_session().connection()
    configurations = ConfigurationRepository(connection)
    templates = request.templates or (await active_templates(configurations)).templates
    taxonomy = await active_taxonomy(configurations)
    version: int | None = None
    if request.policy is not None:
        policy, source = request.policy, PolicySource.DRAFT
    elif request.destinations:
        # Named destinations ignore the rules; the policy is never consulted.
        policy, source = PlacementPolicy(), PolicySource.DESTINATIONS
    else:
        active = await active_policy(configurations)
        policy, source, version = active.policy, PolicySource.ACTIVE, active.version
    await DryRunStore(connection).create(
        DryRun(
            id=request.run_id,
            started_at=now,
            template_version=templates.version,
            taxonomy_version=taxonomy.version,
            policy_source=source,
            policy_version=version,
            policy_rules=tuple(rule.name for rule in policy.rules),
            templates_draft=request.templates is not None,
            policy_document=None
            if request.policy is None
            else request.policy.model_dump(mode="json"),
            templates_document=None
            if request.templates is None
            else request.templates.model_dump(mode="json"),
        )
    )
    return DryRunContext(templates=templates, policy=policy)


async def _record_tx(items: tuple[DryRunItem, ...]) -> None:
    """Record one page's items."""
    connection = await current().datasource.sql_session().connection()
    await DryRunStore(connection).add(items)


async def _close_tx(run_id: UUID, now: datetime) -> None:
    """Record that the run finished."""
    connection = await current().datasource.sql_session().connection()
    await DryRunStore(connection).finish(run_id, now)


# Workflows.


@DBOS.workflow(name=WORKFLOW_NAME)
async def dry_run(request: DryRunRequest) -> int:
    """Evaluate every content object and persist the results; return how many items."""
    datasource = current().datasource
    context = await datasource.run_tx_step_async(
        {"name": "open_dry_run"}, _open_tx, request, await _dry_run_now_step()
    )
    after: ContentObjectId | None = None
    total = 0
    while True:
        page = await _dry_run_page_step(request, context, after)
        if page.last is None:
            break
        if page.items:
            await datasource.run_tx_step_async({"name": "record_dry_run"}, _record_tx, page.items)
        total += len(page.items)
        after = page.last
    await datasource.run_tx_step_async(
        {"name": "close_dry_run"}, _close_tx, request.run_id, await _dry_run_now_step()
    )
    return total
