"""Dry-run evaluation: what placement would do, without doing any of it.

For each content object, the dry run reports where each destination it would be placed
at puts it, and the verification rung the copy could reach given the hashes both
providers report. It reads the ledger and writes only its own results: nothing reaches a
destination, no source is touched, and no placement, review item or audit entry is
created, so a real execution afterwards proceeds as though it had never run.

Content that cannot be evaluated — unclassified, awaiting review, or failing to render —
is reported with the reason, and evaluation continues with the rest.

A run evaluates with the policy in force, with a draft policy submitted with it, or
against destinations it names, ignoring rules; and with the template set in force or a
draft one. A draft is carried in the request and recorded on the run's header, never
stored as a configuration version. Each item names the rules that selected its
destination, each by its position in the policy and its name, since a policy may hold two
rules of one name; classified content no rule selects is reported as one `unselected`
item with no destination.

A locator the run has already proposed for other content counts as taken, as it would be
once that copy started: the later content is given its suffixed locator, just as a real
start claims one. Content is evaluated in id order, so the earlier id keeps the rendered
locator; in a real run whichever copy starts first does.
"""

from collections.abc import Mapping, Sequence
from typing import Final, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from curate_d.classification.store import ClassificationRepository, ReviewRepository
from curate_d.domain import (
    ContentObject,
    ContentObjectId,
    DestinationConfig,
    DomainModel,
    DryRunOutcome,
    Facts,
    HashAlgorithm,
    Placement,
    PlacementPolicy,
    PolicySource,
    Provider,
    RejectedValue,
    SourceObject,
    SourceObjectState,
    VerificationLevel,
    matches,
)
from curate_d.ledger import ContentObjectRepository, PlacementRepository, SourceObjectRepository
from curate_d.ledger.mapping import (
    Row,
    identifier,
    moment,
    opt_moment,
    opt_number,
    opt_text,
    text,
)
from curate_d.ledger.tables import content_objects, dry_run_items, dry_runs, provider_connections
from curate_d.placement.pipeline import claim
from curate_d.placement.verification import expected_level
from curate_d.providers.hashes import advertised_hashes
from curate_d.rendering.planning import PlacementPlan, RenderFailure, plan_at
from curate_d.rendering.templates import TemplateSet

# Content objects evaluated per step.
PAGE_SIZE: Final = 200
# The destination recorded for content that never reached rendering.
NO_DESTINATION: Final = ""


class DryRunRequest(DomainModel):
    """One dry run: its identity, and optionally what to evaluate with instead of what is in force.

    Drafts travel in the request, so the workflow evaluates exactly what was submitted
    whenever it runs, and nothing is written to the configuration versions.
    """

    run_id: UUID
    destinations: tuple[DestinationConfig, ...] = Field(
        default=(),
        description="Evaluate every classified object against each of these. Empty: the "
        "policy's rules decide, as for a real placement.",
    )
    policy: PlacementPolicy | None = Field(
        default=None,
        description="A draft policy whose rules decide instead of the policy in force.",
    )
    templates: TemplateSet | None = Field(
        default=None,
        description="A draft template set to render with instead of the set in force.",
    )

    @model_validator(mode="after")
    def _check(self) -> Self:
        """Refuse a draft policy alongside named destinations, which ignore its rules.

        Raises:
            ValueError: both a draft policy and destinations were given.
        """
        if self.policy is not None and self.destinations:
            raise ValueError(
                "a draft policy and named destinations cannot be combined: named "
                "destinations ignore the policy's rules"
            )
        return self


class DryRun(DomainModel):
    """A dry run's header: when it ran, and what it evaluated with.

    `policy_source` is None for a run recorded before the source was. `policy_rules`
    names the rules evaluated in policy order, so a rule's index is its position there.
    The draft documents are kept as the JSON they were evaluated as, so a run stays
    readable whatever later versions of the parser accept.
    """

    id: UUID
    started_at: AwareDatetime
    finished_at: AwareDatetime | None = None
    template_version: str
    taxonomy_version: int
    policy_source: PolicySource | None = None
    policy_version: int | None = Field(
        default=None, description="The policy version in force; set only for an active run."
    )
    policy_rules: tuple[str, ...] = ()
    templates_draft: bool = False
    policy_document: Mapping[str, object] | None = None
    templates_document: Mapping[str, object] | None = None


class SelectingRule(DomainModel):
    """A placement rule that selected a destination: its position in the policy, and its name.

    The position tells apart rules that share a name, which a policy may hold; a policy
    version never changes, so the position stays the rule's for the whole run.
    """

    index: int = Field(ge=0)
    name: str


class DryRunItem(DomainModel):
    """What the dry run concluded for one content object at one destination."""

    run_id: UUID
    content_object_id: ContentObjectId
    destination_name: str = NO_DESTINATION
    outcome: DryRunOutcome
    paths: tuple[str, ...] = ()
    facts: Facts | None = None
    locator: str | None = None
    expected_level: VerificationLevel | None = None
    reason: str | None = None
    rejected_values: tuple[RejectedValue, ...] = ()
    rules: tuple[SelectingRule, ...] = Field(
        default=(), description="The policy rules that selected the destination, in order."
    )


class DryRunStore:
    """Dry runs and their items."""

    def __init__(self, connection: AsyncConnection) -> None:
        """Wrap a caller-owned connection."""
        self._connection = connection

    async def create(self, run: DryRun) -> None:
        """Record that a run started, discarding what an earlier execution recorded for it.

        The workflow opens a run in a datasource transaction, which re-executes only once
        its checkpoint is gone, as after a rewind from before it. The run then starts over
        with the configuration read now, so the header and items written under the
        earlier one are replaced, never kept beside items evaluated under this one.
        """
        await self._connection.execute(
            dry_run_items.delete().where(dry_run_items.c.run_id == run.id)
        )
        await self._connection.execute(dry_runs.delete().where(dry_runs.c.id == run.id))
        await self._connection.execute(dry_runs.insert().values(**_run_values(run)))

    async def finish(self, run_id: UUID, now: AwareDatetime) -> None:
        """Record that a run finished."""
        await self._connection.execute(
            dry_runs.update().where(dry_runs.c.id == run_id).values(finished_at=now)
        )

    async def add(self, items: Sequence[DryRunItem]) -> None:
        """Record items, replacing any a repeated page already recorded."""
        for item in items:
            await self._connection.execute(
                dry_run_items.delete().where(
                    dry_run_items.c.run_id == item.run_id,
                    dry_run_items.c.content_object_id == item.content_object_id,
                    dry_run_items.c.destination_name == item.destination_name,
                )
            )
        if items:
            await self._connection.execute(
                dry_run_items.insert(), [_item_values(item) for item in items]
            )

    async def get(self, run_id: UUID) -> DryRun | None:
        """A run's header, or None if there is no such run."""
        row = (
            (await self._connection.execute(select(dry_runs).where(dry_runs.c.id == run_id)))
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        return _to_run(row)

    async def proposed_for(
        self, run_id: UUID, destination_name: str, locator: str
    ) -> ContentObjectId | None:
        """The content a run already proposed `locator` for at a destination, if any."""
        value = await self._connection.scalar(
            select(dry_run_items.c.content_object_id).where(
                dry_run_items.c.run_id == run_id,
                dry_run_items.c.destination_name == destination_name,
                dry_run_items.c.locator == locator,
                dry_run_items.c.outcome == DryRunOutcome.PROPOSED.value,
            )
        )
        return None if value is None else ContentObjectId(value)

    async def items(self, run_id: UUID) -> list[DryRunItem]:
        """A run's items, by content object and destination."""
        rows = await self._connection.execute(
            select(dry_run_items)
            .where(dry_run_items.c.run_id == run_id)
            .order_by(dry_run_items.c.content_object_id, dry_run_items.c.destination_name)
        )
        return [_to_item(row) for row in rows.mappings()]


def _run_values(run: DryRun) -> dict[str, object]:
    """Return the dry_runs row for a header."""
    return {
        "id": run.id,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "template_version": run.template_version,
        "taxonomy_version": run.taxonomy_version,
        "policy_source": None if run.policy_source is None else run.policy_source.value,
        "policy_version": run.policy_version,
        "policy_rules": list(run.policy_rules),
        "templates_draft": run.templates_draft,
        "policy_document": None if run.policy_document is None else dict(run.policy_document),
        "templates_document": (
            None if run.templates_document is None else dict(run.templates_document)
        ),
    }


def _document(row: Row, column: str) -> Mapping[str, object] | None:
    """Return a JSON document column as a mapping, or None if it is empty."""
    value = row[column]
    return {str(key): item for key, item in value.items()} if isinstance(value, dict) else None


def _names(row: Row, column: str) -> tuple[str, ...]:
    """Return a JSON list-of-names column as a tuple."""
    value = row[column]
    return tuple(str(name) for name in value) if isinstance(value, list) else ()


def _selecting_rules(row: Row) -> tuple[SelectingRule, ...]:
    """Return an item's JSON list of selecting rules as a tuple."""
    value = row["rules"]
    return tuple(
        SelectingRule.model_validate(rule, strict=False)
        for rule in (value if isinstance(value, list) else [])
    )


def _to_run(row: Row) -> DryRun:
    """Return a header from its dry_runs row."""
    source = opt_text(row, "policy_source")
    return DryRun(
        id=identifier(row, "id"),
        started_at=moment(row, "started_at"),
        finished_at=opt_moment(row, "finished_at"),
        template_version=text(row, "template_version"),
        taxonomy_version=int(str(row["taxonomy_version"])),
        policy_source=None if source is None else PolicySource(source),
        policy_version=opt_number(row, "policy_version"),
        policy_rules=_names(row, "policy_rules"),
        templates_draft=bool(row["templates_draft"]),
        policy_document=_document(row, "policy_document"),
        templates_document=_document(row, "templates_document"),
    )


def _item_values(item: DryRunItem) -> dict[str, object]:
    """The dry_run_items row for an item."""
    return {
        "run_id": item.run_id,
        "content_object_id": item.content_object_id,
        "destination_name": item.destination_name,
        "outcome": item.outcome.value,
        "paths": "\n".join(item.paths),
        "facts": None if item.facts is None else item.facts.model_dump(mode="json"),
        "locator": item.locator,
        "expected_level": None if item.expected_level is None else int(item.expected_level),
        "reason": item.reason,
        "rejected_values": [value.model_dump(mode="json") for value in item.rejected_values],
        "rules": [rule.model_dump(mode="json") for rule in item.rules],
    }


def _to_item(row: Row) -> DryRunItem:
    """An item from its dry_run_items row."""
    paths = text(row, "paths")
    level = row["expected_level"]
    facts = row["facts"]
    values = row["rejected_values"]
    return DryRunItem(
        run_id=identifier(row, "run_id"),
        content_object_id=ContentObjectId(identifier(row, "content_object_id")),
        destination_name=text(row, "destination_name"),
        outcome=DryRunOutcome(text(row, "outcome")),
        paths=tuple(paths.split("\n")) if paths else (),
        facts=None if facts is None else Facts.model_validate(facts, strict=False),
        locator=opt_text(row, "locator"),
        expected_level=None if level is None else VerificationLevel(int(str(level))),
        reason=opt_text(row, "reason"),
        rejected_values=tuple(
            RejectedValue.model_validate(value, strict=False)
            for value in (values if isinstance(values, list) else [])
        ),
        rules=_selecting_rules(row),
    )


def selections(policy: PlacementPolicy, facts: Facts) -> dict[str, tuple[SelectingRule, ...]]:
    """Return each destination a matching rule asks for, with every rule that does.

    `PlacementPolicy.selections` with each rule named by its position too, so two rules
    of one name stay apart.

    Returns:
        Destination names in sorted order, each mapped to the matching rules that target
        it, in policy order.
    """
    selected: dict[str, list[SelectingRule]] = {}
    for index, rule in enumerate(policy.rules):
        if matches(rule.when, facts):
            for name in rule.destinations:
                rules = selected.setdefault(name, [])
                if not rules or rules[-1].index != index:
                    rules.append(SelectingRule(index=index, name=rule.name))
    return {name: tuple(selected[name]) for name in sorted(selected)}


def _failure(error: Exception) -> str:
    """Return the reason recorded for an evaluation that raised `error`."""
    return f"evaluation failed: {type(error).__name__}: {error}"


async def content_page(
    connection: AsyncConnection, *, after: ContentObjectId | None, limit: int = PAGE_SIZE
) -> list[ContentObjectId]:
    """Content object ids in id order, after `after`."""
    query = select(content_objects.c.id).order_by(content_objects.c.id).limit(limit)
    if after is not None:
        query = query.where(content_objects.c.id > after)
    return [ContentObjectId(row[0]) for row in await connection.execute(query)]


class Evaluator:
    """Evaluates content objects against a template set and a policy, reading the ledger."""

    def __init__(
        self,
        connection: AsyncConnection,
        *,
        run_id: UUID,
        templates: TemplateSet,
        policy: PlacementPolicy,
        destinations: Sequence[DestinationConfig] = (),
    ) -> None:
        """Bind the run, what it evaluates with, and any destinations overriding the policy."""
        self._connection = connection
        self._run_id = run_id
        self._templates = templates
        self._policy = policy
        self._destinations = tuple(destinations)
        self._hosts: dict[tuple[Provider, str], str | None] = {}
        # Locators proposed while this evaluator ran, by destination; earlier pages' are
        # read back from the run's recorded items.
        self._proposed: dict[str, dict[str, ContentObjectId]] = {}

    async def evaluate(self, content_object_id: ContentObjectId) -> list[DryRunItem]:
        """Return the items for one content object; a failure is an item, never raised.

        A failure at one selected destination fails that destination's item only, which
        keeps the rules that selected it; one before selection is a single item.
        """
        try:
            return await self._evaluate(content_object_id)
        except Exception as error:
            return [
                DryRunItem(
                    run_id=self._run_id,
                    content_object_id=content_object_id,
                    outcome=DryRunOutcome.RENDER_FAILED,
                    reason=_failure(error),
                )
            ]

    async def _evaluate(self, content_object_id: ContentObjectId) -> list[DryRunItem]:
        """Items for one content object."""
        sources = await SourceObjectRepository(self._connection).find_by_content_object(
            content_object_id
        )
        paths = tuple(sorted(s.path for s in sources))
        unresolved = DryRunItem(
            run_id=self._run_id,
            content_object_id=content_object_id,
            outcome=DryRunOutcome.UNCLASSIFIED,
            paths=paths,
        )
        reviews = ReviewRepository(self._connection)
        if await reviews.excluded(content_object_id):
            return [unresolved.model_copy(update={"reason": "excluded in review"})]
        review = await reviews.open_for(content_object_id)
        if review is not None:
            return [
                unresolved.model_copy(
                    update={
                        "reason": f"awaiting review ({review.reason.value}): {review.detail}",
                        "rejected_values": review.rejected_values,
                    }
                )
            ]
        classification = await ClassificationRepository(self._connection).get(content_object_id)
        if classification is None:
            return [unresolved.model_copy(update={"reason": "not classified yet"})]
        content = await ContentObjectRepository(self._connection).get(content_object_id)
        if content is None:
            return [unresolved.model_copy(update={"reason": "content object is gone"})]
        facts = classification.facts
        selected: list[tuple[DestinationConfig, tuple[SelectingRule, ...]]] = [
            (destination, ()) for destination in self._destinations
        ]
        if not self._destinations:
            selected = [
                (destination, rules)
                for name, rules in selections(self._policy, facts).items()
                if (destination := self._policy.destination(name)) is not None
            ]
            if not selected:
                return [
                    unresolved.model_copy(
                        update={
                            "outcome": DryRunOutcome.UNSELECTED,
                            "facts": facts,
                            "reason": "no placement rule selects it",
                        }
                    )
                ]
        copied = next((s for s in sources if s.state is SourceObjectState.FINGERPRINTED), None)
        items: list[DryRunItem] = []
        for destination, rules in selected:
            base = DryRunItem(
                run_id=self._run_id,
                content_object_id=content_object_id,
                destination_name=destination.name,
                outcome=DryRunOutcome.RENDER_FAILED,
                paths=paths,
                facts=facts,
                rules=rules,
            )
            try:
                items.append(await self._evaluate_at(base, destination, facts, content, copied))
            except Exception as error:
                items.append(base.model_copy(update={"reason": _failure(error)}))
        return items

    async def _evaluate_at(
        self,
        base: DryRunItem,
        destination: DestinationConfig,
        facts: Facts,
        content: ContentObject,
        copied: SourceObject | None,
    ) -> DryRunItem:
        """Return `base` completed with what placing the content at `destination` would do."""
        outcome = await plan_at(
            self._connection,
            self._templates,
            facts,
            content_object_id=content.id,
            fingerprint=content.fingerprint,
            paths=base.paths,
            destination=destination,
        )
        if isinstance(outcome, RenderFailure):
            return base.model_copy(update={"reason": outcome.reason})
        locator, present = await self._claim(outcome, content)
        return base.model_copy(
            update={
                "outcome": DryRunOutcome.ALREADY_PRESENT if present else DryRunOutcome.PROPOSED,
                "locator": locator,
                "expected_level": await self._expected_level(copied, destination),
            }
        )

    async def _claim(self, planned: PlacementPlan, content: ContentObject) -> tuple[str, bool]:
        """The locator a copy would claim, given what this run proposed; and if it is there.

        Returns:
            The locator, and whether the content already occupies it.
        """
        if planned.already_present:
            return planned.locator, True
        destination = planned.destination_name
        proposed = self._proposed.setdefault(destination, {})
        placements_repo = PlacementRepository(self._connection)
        store = DryRunStore(self._connection)
        own: set[str] = set()

        async def holder(candidate: str) -> Placement | None:
            """The ledger's occupant of `candidate`, else other content this run put there."""
            occupant = await placements_repo.find_at_locator(
                destination_name=destination, locator=candidate
            )
            if occupant is not None:
                if occupant.content_object_id == content.id:
                    own.add(candidate)
                return occupant
            other = proposed.get(candidate) or await store.proposed_for(
                self._run_id, destination, candidate
            )
            if other is None or other == content.id:
                return None
            return Placement.build(
                content_object_id=other,
                destination_name=destination,
                locator=candidate,
                template_version=planned.rendered.template_version,
            )

        locator, _ = await claim(
            planned.locator,
            content_object_id=content.id,
            fingerprint=content.fingerprint,
            holder=holder,
        )
        if locator in own:
            return locator, True
        proposed[locator] = content.id
        return locator, False

    async def _expected_level(
        self, source: SourceObject | None, destination: DestinationConfig
    ) -> VerificationLevel:
        """The rung a copy from `source` to `destination` could reach."""
        source_algorithms: frozenset[HashAlgorithm] = frozenset()
        if source is not None:
            host = await self._host(source.provider, source.account)
            source_algorithms = advertised_hashes(source.provider, host)
        destination_algorithms = advertised_hashes(
            destination.provider, await self._host(destination.provider, destination.account)
        )
        return expected_level(source_algorithms, destination_algorithms)

    async def _host(self, provider: Provider, account: str) -> str | None:
        """The API host serving an account, if it is connected."""
        key = (provider, account)
        if key not in self._hosts:
            self._hosts[key] = await self._connection.scalar(
                select(provider_connections.c.api_host).where(
                    provider_connections.c.provider == provider.value,
                    provider_connections.c.account == account,
                )
            )
        return self._hosts[key]
