"""From stored facts to a placement plan, or a reason there can be none.

The step between classification and transfer. It renders the locator, resolves it against
whatever already occupies it at the destination, and either returns what to place where,
or the failure a person needs to see. A failure creates no placement; its review item and
audit entry say which fact was missing.
"""

from collections.abc import Mapping, Sequence
from typing import Final

from pydantic import AwareDatetime, Field
from sqlalchemy.ext.asyncio import AsyncConnection

from curate_d.configuration.store import ConfigurationRepository
from curate_d.domain import (
    AuditEntry,
    AuditEntryType,
    AuditValue,
    ConfigurationKind,
    ContentObjectId,
    DestinationConfig,
    DomainModel,
    Facts,
    Placement,
    StrongFingerprint,
)
from curate_d.ledger import PlacementRepository
from curate_d.rendering.collision import resolve
from curate_d.rendering.render import Rendered, RenderError, render
from curate_d.rendering.templates import (
    PROFILES,
    SEED_TEMPLATES,
    CapabilityProfile,
    TemplateSet,
    parse_templates,
)

DECIDED_BY: Final = "rendering"


class ActiveTemplates(DomainModel):
    """The template set in force and its configuration version."""

    configuration_version: int = Field(ge=1)
    templates: TemplateSet


async def active_templates(configurations: ConfigurationRepository) -> ActiveTemplates:
    """The active template set, storing the seed as version 1 if none was ever stored."""
    version = await configurations.ensure(
        ConfigurationKind.TEMPLATES, SEED_TEMPLATES, parse_templates, created_by="seed"
    )
    stored = await configurations.active(ConfigurationKind.TEMPLATES)
    assert stored is not None
    return ActiveTemplates(configuration_version=version, templates=parse_templates(stored[1]))


class PlacementPlan(DomainModel):
    """Where one content object goes at one destination."""

    content_object_id: ContentObjectId
    destination_name: str
    rendered: Rendered
    locator: str = Field(description="The rendered locator after collision resolution.")
    already_present: bool = False


class RenderFailure(DomainModel):
    """Why content could not be given a locator."""

    content_object_id: ContentObjectId
    destination_name: str
    reason: str
    missing: tuple[str, ...] = ()


def original_name(paths: Sequence[str]) -> str:
    """The name a file is filed under: the first of its source names, by path order."""
    return sorted(paths)[0].rsplit("/", 1)[-1] if paths else "unnamed"


def plan(
    templates: TemplateSet,
    facts: Facts,
    *,
    content_object_id: ContentObjectId,
    fingerprint: StrongFingerprint,
    paths: Sequence[str],
    destination_name: str,
    profile: CapabilityProfile,
    root: str,
    occupants: Mapping[str, Placement],
) -> PlacementPlan | RenderFailure:
    """Render and resolve one placement, or explain why it cannot be made.

    `occupants` maps locators to the placements already there at this destination, for
    the locators this content could render to; collision resolution needs nothing else.
    """
    try:
        rendered = render(
            templates,
            facts,
            original_name=original_name(paths),
            content_object_id=content_object_id,
            profile=profile,
            root=root,
        )
    except RenderError as error:
        return RenderFailure(
            content_object_id=content_object_id,
            destination_name=destination_name,
            reason=str(error),
            missing=error.missing,
        )
    resolution = resolve(
        rendered.locator,
        occupants.get(rendered.locator),
        content_object_id=content_object_id,
        fingerprint=fingerprint,
    )
    return PlacementPlan(
        content_object_id=content_object_id,
        destination_name=destination_name,
        rendered=rendered,
        locator=resolution.locator,
        already_present=resolution.already_present,
    )


async def plan_at(
    connection: AsyncConnection,
    templates: TemplateSet,
    facts: Facts,
    *,
    content_object_id: ContentObjectId,
    fingerprint: StrongFingerprint,
    paths: Sequence[str],
    destination: DestinationConfig,
) -> PlacementPlan | RenderFailure:
    """Plan one placement at `destination`, resolved against what the ledger has there."""
    profile = PROFILES.get(destination.profile)
    if profile is None:
        return RenderFailure(
            content_object_id=content_object_id,
            destination_name=destination.name,
            reason=f"unknown capability profile {destination.profile!r}",
        )

    def attempt(occupants: Mapping[str, Placement]) -> PlacementPlan | RenderFailure:
        """Plan against `occupants`."""
        return plan(
            templates,
            facts,
            content_object_id=content_object_id,
            fingerprint=fingerprint,
            paths=paths,
            destination_name=destination.name,
            profile=profile,
            root=destination.root_path,
            occupants=occupants,
        )

    first = attempt({})
    if isinstance(first, RenderFailure):
        return first
    occupant = await PlacementRepository(connection).find_at_locator(
        destination_name=destination.name, locator=first.rendered.locator
    )
    return first if occupant is None else attempt({first.rendered.locator: occupant})


def render_failure_audit(failure: RenderFailure, *, now: AwareDatetime) -> AuditEntry:
    """The audit entry for a render failure."""
    details: dict[str, AuditValue] = {
        "destination": failure.destination_name,
        "reason": failure.reason,
        "missing": ", ".join(failure.missing) or None,
    }
    return AuditEntry(
        entry_type=AuditEntryType.RENDER_FAILED,
        occurred_at=now,
        decided_by=DECIDED_BY,
        details=details,
        content_object_id=failure.content_object_id,
    )
