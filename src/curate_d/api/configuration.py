"""Taxonomy, path templates and placement policy: view, change, and list versions.

A change is a new version made active in one transaction, so an invalid document never
replaces the one in force. Documents arrive as JSON and are parsed leniently in type and
strictly in shape before the endpoint sees them; a document the parser refuses, including
one carrying a retired field a stored version may still hold, is answered 422 with each
problem located: an unknown `media_kind` value or capability profile among them. A dry
run's draft policy or template set goes through the same validation (`check_submission`)
without being stored.

Category and subcategory conditions depend on the taxonomy, which is versioned on its own.
A policy or template change is checked against the taxonomy in force inside its activation
transaction, holding the taxonomy's lock in shared mode, and refused (422) if a condition
names a value it does not list. A taxonomy change is never refused for the conditions it
leaves without a value: it is answered with them, and they are logged as
`configuration.orphaned_conditions`. It is refused (422) only while the policy or template
set in force does not read, since its orphans could not be reported; a valid policy or
template change replaces such a version without reading it.

A new taxonomy version asks the workers to classify again what it would now accept.
Nothing here touches placements. A new template version changes where reconciliation
puts content from its next pass; until then every placement stays as it is.
"""

from collections.abc import Callable, Mapping, Sequence
from typing import Annotated, Final, Protocol

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, BeforeValidator, Field
from sqlalchemy.ext.asyncio import AsyncEngine

from curate_d.api.errors import REJECTED, RejectedError, problems
from curate_d.api.reclassification import Reclassify, nothing
from curate_d.classification.taxonomy import active_taxonomy
from curate_d.configuration.consistency import (
    OrphanedCondition,
    check_against,
    orphans_in_force,
    submission_parser,
    taxonomy_in_force,
)
from curate_d.configuration.documents import parse_policy, parse_taxonomy
from curate_d.configuration.store import (
    ConfigurationError,
    ConfigurationRepository,
    ConfigurationVersion,
    DocumentValidator,
)
from curate_d.domain import ConfigurationKind, PlacementPolicy, Taxonomy
from curate_d.observability import get_logger
from curate_d.placement.policy import active_policy
from curate_d.rendering.planning import active_templates
from curate_d.rendering.templates import TemplateSet, parse_templates

# The operator name comes from a header set by an authenticating reverse proxy in front of the
# API. The API performs no authentication itself and must not be exposed without such a proxy;
# calls without the header are recorded as the control plane.
OPERATOR_HEADER: Final = "Tailscale-User-Login"
UNNAMED_OPERATOR: Final = "control-plane"

logger = get_logger(__name__)


def _lax(parser: DocumentValidator) -> BeforeValidator:
    """Parse a submitted JSON object leniently in type and strictly in shape."""
    return BeforeValidator(lambda value: parser(value) if isinstance(value, Mapping) else value)


SubmittedTaxonomy = Annotated[Taxonomy, _lax(parse_taxonomy)]
SubmittedTemplates = Annotated[TemplateSet, _lax(parse_templates)]
SubmittedPolicy = Annotated[PlacementPolicy, _lax(parse_policy)]


def check_submission(
    kind: ConfigurationKind, document: BaseModel, *, taxonomy: Taxonomy | None = None
) -> BaseModel:
    """Validate a submitted document as activating it would, storing nothing.

    `check_against`, mapped to the API's refusal. Activation validates with the parser it
    uses, and a dry run checks its drafts with it, so a draft a dry run accepts is one
    `PUT /configuration/{kind}` would accept too.

    Args:
        kind: The kind `document` is submitted as.
        document: The submitted document, already parsed from its request body.
        taxonomy: The taxonomy in force, read by the caller under the taxonomy's shared
            lock: every category and subcategory condition of a policy or template set
            must be one it lists. Ignored for a taxonomy.

    Returns:
        The document as parsed from its stored (JSON) form.

    Raises:
        RejectedError: the document is invalid for `kind`, or names a category or
            subcategory `taxonomy` does not list.
    """
    try:
        return check_against(kind, document, taxonomy=taxonomy)
    except ConfigurationError as error:
        raise RejectedError(str(error)) from None


class TaxonomyConfiguration(BaseModel):
    """The taxonomy in force and its version."""

    version: int
    document: Taxonomy
    orphaned: tuple[OrphanedCondition, ...] = Field(
        default=(),
        description="Policy and template conditions this version, just activated, leaves "
        "without a value to match. Empty when the taxonomy is only read.",
    )


class TemplatesConfiguration(BaseModel):
    """The template set in force and its version."""

    version: int
    document: TemplateSet


class PolicyConfiguration(BaseModel):
    """The placement policy in force and its version."""

    version: int
    document: PlacementPolicy


class Activated(BaseModel):
    """The version an activation stored, and the conditions a new taxonomy orphans."""

    version: int
    orphaned: tuple[OrphanedCondition, ...] = ()


class ConfigurationBackend(Protocol):
    """Configuration documents and their versions."""

    async def active(self, kind: ConfigurationKind) -> tuple[int, BaseModel]:
        """The version in force and its document, seeding the default if never set."""
        ...

    async def activate(self, kind: ConfigurationKind, document: BaseModel, *, by: str) -> Activated:
        """Store `document` as the next version and make it active.

        Raises:
            RejectedError: the document is invalid, a policy or template condition names a
                value the taxonomy in force does not list, or a taxonomy is submitted while
                the policy or template set in force does not read; the version in force is
                unchanged.
        """
        ...

    async def history(self, kind: ConfigurationKind) -> Sequence[ConfigurationVersion]:
        """Every version of `kind`, newest first."""
        ...


def _as[M: BaseModel](document: BaseModel, model: type[M]) -> M:
    """`document` as the model its kind is stored as.

    Raises:
        TypeError: the backend handed back another kind's document.
    """
    if not isinstance(document, model):
        raise TypeError(f"expected a {model.__name__}, got a {type(document).__name__}")
    return document


def operator(login: Annotated[str | None, Header(alias=OPERATOR_HEADER)] = None) -> str:
    """Who is making the change, as the reverse proxy names them."""
    return login or UNNAMED_OPERATOR


def router(backend: Callable[[Request], ConfigurationBackend]) -> APIRouter:
    """The configuration endpoints over `backend`."""
    routes = APIRouter(prefix="/configuration", tags=["configuration"])
    uses = Depends(backend)
    by = Depends(operator)

    @routes.get("/taxonomy", response_model=TaxonomyConfiguration)
    async def get_taxonomy(
        configuration: Annotated[ConfigurationBackend, uses],
    ) -> TaxonomyConfiguration:
        """The taxonomy in force."""
        version, document = await configuration.active(ConfigurationKind.TAXONOMY)
        return TaxonomyConfiguration(version=version, document=_as(document, Taxonomy))

    @routes.put("/taxonomy", response_model=TaxonomyConfiguration, responses=problems(REJECTED))
    async def put_taxonomy(
        document: SubmittedTaxonomy,
        configuration: Annotated[ConfigurationBackend, uses],
        who: Annotated[str, by],
    ) -> TaxonomyConfiguration:
        """Make `document` the taxonomy in force, answering with the conditions it orphans."""
        activated = await configuration.activate(ConfigurationKind.TAXONOMY, document, by=who)
        return TaxonomyConfiguration(
            version=activated.version, document=document, orphaned=activated.orphaned
        )

    @routes.get("/templates", response_model=TemplatesConfiguration)
    async def get_templates(
        configuration: Annotated[ConfigurationBackend, uses],
    ) -> TemplatesConfiguration:
        """The path templates in force."""
        version, document = await configuration.active(ConfigurationKind.TEMPLATES)
        return TemplatesConfiguration(version=version, document=_as(document, TemplateSet))

    @routes.put("/templates", response_model=TemplatesConfiguration, responses=problems(REJECTED))
    async def put_templates(
        document: SubmittedTemplates,
        configuration: Annotated[ConfigurationBackend, uses],
        who: Annotated[str, by],
    ) -> TemplatesConfiguration:
        """Make `document` the path templates in force; placements move on reconciliation."""
        activated = await configuration.activate(ConfigurationKind.TEMPLATES, document, by=who)
        return TemplatesConfiguration(version=activated.version, document=document)

    @routes.get("/policy", response_model=PolicyConfiguration)
    async def get_policy(
        configuration: Annotated[ConfigurationBackend, uses],
    ) -> PolicyConfiguration:
        """The placement policy in force."""
        version, document = await configuration.active(ConfigurationKind.POLICY)
        return PolicyConfiguration(version=version, document=_as(document, PlacementPolicy))

    @routes.put("/policy", response_model=PolicyConfiguration, responses=problems(REJECTED))
    async def put_policy(
        document: SubmittedPolicy,
        configuration: Annotated[ConfigurationBackend, uses],
        who: Annotated[str, by],
    ) -> PolicyConfiguration:
        """Make `document` the placement policy in force."""
        activated = await configuration.activate(ConfigurationKind.POLICY, document, by=who)
        return PolicyConfiguration(version=activated.version, document=document)

    @routes.get("/{kind}/history", response_model=list[ConfigurationVersion])
    async def history(
        kind: ConfigurationKind, configuration: Annotated[ConfigurationBackend, uses]
    ) -> Sequence[ConfigurationVersion]:
        """Every version of `kind`, newest first, and which is in force."""
        return await configuration.history(kind)

    return routes


async def report_orphans(
    configurations: ConfigurationRepository, taxonomy: Taxonomy
) -> list[OrphanedCondition]:
    """Return the conditions of the policy and template set in force `taxonomy` orphans.

    Raises:
        RejectedError: the policy or template set in force does not read, so its orphans
            cannot be reported; it names the kind and each problem.
    """
    try:
        return await orphans_in_force(configurations, taxonomy)
    except ConfigurationError as unreadable:
        raise RejectedError(
            f"the {unreadable.kind.value} configuration in force does not read, so the "
            "conditions this taxonomy orphans cannot be reported; activate a valid "
            f"{unreadable.kind.value} configuration first. {unreadable}"
        ) from None


class LedgerConfiguration:
    """Configuration versions in the ledger."""

    def __init__(self, engine: AsyncEngine, *, reclassify: Reclassify = nothing) -> None:
        """Bind the ledger, and what to do when a taxonomy version becomes active."""
        self._engine = engine
        self._reclassify = reclassify

    async def active(self, kind: ConfigurationKind) -> tuple[int, BaseModel]:
        """The version in force and its document, seeding the default if never set."""
        async with self._engine.begin() as connection:
            configurations = ConfigurationRepository(connection)
            if kind is ConfigurationKind.TAXONOMY:
                taxonomy = await active_taxonomy(configurations)
                return taxonomy.version, taxonomy.taxonomy
            if kind is ConfigurationKind.TEMPLATES:
                templates = await active_templates(configurations)
                return templates.configuration_version, templates.templates
            policy = await active_policy(configurations)
            return policy.version, policy.policy

    async def activate(self, kind: ConfigurationKind, document: BaseModel, *, by: str) -> Activated:
        """Store `document` as the next version and make it active.

        A policy or template set is checked against the taxonomy in force, read under the
        taxonomy's shared lock in the same transaction; the version in force of its own
        kind is never read. A taxonomy is stored under its exclusive lock, then the policy
        and template set in force are checked against it, in the same transaction.

        Raises:
            RejectedError: the document is invalid, a policy or template condition names a
                value the taxonomy in force does not list, or a taxonomy is submitted while
                the policy or template set in force does not read; the version in force is
                unchanged.
        """
        orphaned: list[OrphanedCondition] = []
        try:
            async with self._engine.begin() as connection:
                configurations = ConfigurationRepository(connection)
                taxonomy = None
                if kind is not ConfigurationKind.TAXONOMY:
                    await configurations.lock_shared(ConfigurationKind.TAXONOMY)
                    taxonomy = await taxonomy_in_force(configurations)
                version = await configurations.activate(
                    kind,
                    document.model_dump(mode="json"),
                    submission_parser(kind, taxonomy),
                    created_by=by,
                )
                if kind is ConfigurationKind.TAXONOMY:
                    orphaned = await report_orphans(configurations, _as(document, Taxonomy))
        except ConfigurationError as error:
            raise RejectedError(str(error)) from None
        if kind is ConfigurationKind.TAXONOMY:
            if orphaned:
                logger.warning(
                    "configuration.orphaned_conditions",
                    taxonomy_version=version,
                    count=len(orphaned),
                    conditions=[
                        f"{o.kind.value}: {o.owner} {o.field.value}={o.value!r}" for o in orphaned
                    ],
                )
            await self._reclassify(version)
        return Activated(version=version, orphaned=tuple(orphaned))

    async def history(self, kind: ConfigurationKind) -> Sequence[ConfigurationVersion]:
        """Every version of `kind`, newest first."""
        async with self._engine.connect() as connection:
            return await ConfigurationRepository(connection).history(kind)
