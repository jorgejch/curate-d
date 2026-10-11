"""Taxonomy, path templates and placement policy: view, change, and list versions.

A change is a new version made active in one transaction, so an invalid document never
replaces the one in force. Documents arrive as JSON and are parsed the way stored ones
are, leniently in type and strictly in shape, before the endpoint sees them; a document
the parser refuses is answered 422 with each problem located.

A new taxonomy version asks the workers to classify again what it would now accept.
Nothing here touches placements. A new template version changes where reconciliation
puts content from its next pass; until then every placement stays as it is.
"""

from collections.abc import Callable, Mapping, Sequence
from typing import Annotated, Final, Protocol

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, BeforeValidator
from sqlalchemy.ext.asyncio import AsyncEngine

from curate_d.api.errors import REJECTED, RejectedError, problems
from curate_d.api.reclassification import Reclassify, nothing
from curate_d.classification.taxonomy import active_taxonomy
from curate_d.configuration.documents import parse_policy, parse_taxonomy
from curate_d.configuration.store import (
    ConfigurationError,
    ConfigurationRepository,
    ConfigurationVersion,
    DocumentValidator,
)
from curate_d.domain import ConfigurationKind, PlacementPolicy, Taxonomy
from curate_d.placement.policy import active_policy
from curate_d.rendering.planning import active_templates
from curate_d.rendering.templates import TemplateSet, parse_templates

# The operator name comes from a header set by an authenticating reverse proxy in front of the
# API. The API performs no authentication itself and must not be exposed without such a proxy;
# calls without the header are recorded as the control plane.
OPERATOR_HEADER: Final = "Tailscale-User-Login"
UNNAMED_OPERATOR: Final = "control-plane"

_PARSERS: Final[Mapping[ConfigurationKind, DocumentValidator]] = {
    ConfigurationKind.TAXONOMY: parse_taxonomy,
    ConfigurationKind.TEMPLATES: parse_templates,
    ConfigurationKind.POLICY: parse_policy,
}


def _lax(parser: DocumentValidator) -> BeforeValidator:
    """Parse a submitted JSON object the way a stored document is parsed."""
    return BeforeValidator(lambda value: parser(value) if isinstance(value, Mapping) else value)


SubmittedTaxonomy = Annotated[Taxonomy, _lax(parse_taxonomy)]
SubmittedTemplates = Annotated[TemplateSet, _lax(parse_templates)]
SubmittedPolicy = Annotated[PlacementPolicy, _lax(parse_policy)]


class TaxonomyConfiguration(BaseModel):
    """The taxonomy in force and its version."""

    version: int
    document: Taxonomy


class TemplatesConfiguration(BaseModel):
    """The template set in force and its version."""

    version: int
    document: TemplateSet


class PolicyConfiguration(BaseModel):
    """The placement policy in force and its version."""

    version: int
    document: PlacementPolicy


class ConfigurationBackend(Protocol):
    """Configuration documents and their versions."""

    async def active(self, kind: ConfigurationKind) -> tuple[int, BaseModel]:
        """The version in force and its document, seeding the default if never set."""
        ...

    async def activate(self, kind: ConfigurationKind, document: BaseModel, *, by: str) -> int:
        """Store `document` as the next version and make it active.

        Raises:
            RejectedError: the document is invalid; the version in force is unchanged.
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
        """Make `document` the taxonomy in force."""
        version = await configuration.activate(ConfigurationKind.TAXONOMY, document, by=who)
        return TaxonomyConfiguration(version=version, document=document)

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
        version = await configuration.activate(ConfigurationKind.TEMPLATES, document, by=who)
        return TemplatesConfiguration(version=version, document=document)

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
        version = await configuration.activate(ConfigurationKind.POLICY, document, by=who)
        return PolicyConfiguration(version=version, document=document)

    @routes.get("/{kind}/history", response_model=list[ConfigurationVersion])
    async def history(
        kind: ConfigurationKind, configuration: Annotated[ConfigurationBackend, uses]
    ) -> Sequence[ConfigurationVersion]:
        """Every version of `kind`, newest first, and which is in force."""
        return await configuration.history(kind)

    return routes


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

    async def activate(self, kind: ConfigurationKind, document: BaseModel, *, by: str) -> int:
        """Store `document` as the next version and make it active.

        Raises:
            RejectedError: the document is invalid; the version in force is unchanged.
        """
        try:
            async with self._engine.begin() as connection:
                version = await ConfigurationRepository(connection).activate(
                    kind, document.model_dump(mode="json"), _PARSERS[kind], created_by=by
                )
        except ConfigurationError as error:
            raise RejectedError(str(error)) from None
        if kind is ConfigurationKind.TAXONOMY:
            await self._reclassify(version)
        return version

    async def history(self, kind: ConfigurationKind) -> Sequence[ConfigurationVersion]:
        """Every version of `kind`, newest first."""
        async with self._engine.connect() as connection:
            return await ConfigurationRepository(connection).history(kind)
