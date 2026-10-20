"""Path templates and destination capability profiles.

A template is a path with placeholders, a list of the facts it requires, and a condition
choosing which content it applies to:

    name: documents
    when: {category: Documents}
    path: "Documents/{subcategory}/{captured_at:%Y}/{original_name}"
    required: [subcategory]

Placeholders name a fact (`{category}`, `{captured_at:%Y-%m}` with a strftime format for
timestamps) or one of the values rendering supplies itself: `{original_name}`,
`{original_stem}`, `{extension}` and `{content_id}`. A placeholder naming anything else is
a configuration error, caught when the template is loaded rather than when a file first
reaches it. A condition's values are checked the same way: a `media_kind` must be a
`MediaKind`, and when a template set is activated, a category or subcategory must be one
the taxonomy in force lists (`domain.conditions`). Reading a stored set checks no taxonomy.

A capability profile describes what a destination accepts: whether it nests folders,
which characters names may not contain, and how long a name may be.
"""

import re
from collections.abc import Mapping
from typing import Final, Self

from pydantic import Field, ValidationInfo, model_validator

from curate_d.domain import (
    CapabilityProfileName,
    DomainModel,
    Facts,
    Taxonomy,
    check_conditions,
    matches,
    taxonomy_in,
)

# Values rendering computes itself rather than reading from facts.
BUILTINS: Final = frozenset({"original_name", "original_stem", "extension", "content_id"})
FACT_FIELDS: Final = frozenset(Facts.model_fields)
_TIMESTAMP_FACTS: Final = frozenset({"captured_at"})
_PLACEHOLDER: Final = re.compile(r"\{([a-z_]+)(?::([^{}]+))?\}")


class Placeholder(DomainModel):
    """One `{name}` or `{name:format}` in a template path."""

    name: str
    format: str | None = None


def placeholders(path: str) -> list[Placeholder]:
    """The placeholders in `path`, in order."""
    return [Placeholder(name=m[1], format=m[2]) for m in _PLACEHOLDER.finditer(path)]


class CapabilityProfile(DomainModel):
    """What names and paths a destination accepts."""

    hierarchical: bool = True
    disallowed_characters: str = Field(
        default="", description="Characters no name may contain, beyond the separator."
    )
    max_component_bytes: int = Field(default=255, ge=16)
    reserved_names: frozenset[str] = frozenset()


# pCloud accepts any character but the separator and NUL in a name, up to 255 bytes, and
# nests folders (rclone.org/pcloud: "restricted filename characters").
PCLOUD_PROFILE: Final = CapabilityProfile(
    hierarchical=True, disallowed_characters="\\", max_component_bytes=255
)
# The most restrictive common target, for destinations that sync to Windows machines.
PORTABLE_PROFILE: Final = CapabilityProfile(
    hierarchical=True,
    disallowed_characters='<>:"\\|?*',
    max_component_bytes=255,
    reserved_names=frozenset({"CON", "PRN", "AUX", "NUL", "COM1", "LPT1"}),
)
# Every name a destination can give has a profile; a unit test holds the two together.
PROFILES: Final[Mapping[CapabilityProfileName, CapabilityProfile]] = {
    CapabilityProfileName.PCLOUD: PCLOUD_PROFILE,
    CapabilityProfileName.PORTABLE: PORTABLE_PROFILE,
}


class Template(DomainModel):
    """How one kind of content is laid out."""

    name: str = Field(min_length=1)
    path: str = Field(min_length=1)
    required: tuple[str, ...] = ()
    when: Mapping[str, str] = Field(
        default_factory=dict,
        description="Facts that must equal these values for the template to apply.",
    )

    @model_validator(mode="after")
    def _check(self, info: ValidationInfo[object]) -> Self:
        """Reject unknown names, unmatchable conditions, and absolute paths.

        Raises:
            ValueError: the template names a placeholder or requirement rendering does
                not know, or a condition that cannot match (with a taxonomy in the
                validation context, a value it does not list).
        """
        known = FACT_FIELDS | BUILTINS
        for placeholder in placeholders(self.path):
            if placeholder.name not in known:
                raise ValueError(f"template {self.name!r} uses unknown fact {placeholder.name!r}")
            if placeholder.format is not None and placeholder.name not in _TIMESTAMP_FACTS:
                raise ValueError(
                    f"template {self.name!r} formats {placeholder.name!r}, which is not a time"
                )
        for fact in self.required:
            if fact not in FACT_FIELDS:
                raise ValueError(f"template {self.name!r} requires unknown fact {fact!r}")
        check_conditions(f"template {self.name!r}", self.when, taxonomy=taxonomy_in(info.context))
        if self.path.startswith("/") or "//" in self.path or ".." in self.path.split("/"):
            raise ValueError(f"template {self.name!r} path must be relative and plain")
        return self

    def applies_to(self, facts: Facts) -> bool:
        """Return whether every condition matches the facts."""
        return matches(self.when, facts)


class TemplateSet(DomainModel):
    """The layout in force: templates tried in order, and the version rendering records."""

    version: str = Field(
        min_length=1,
        pattern=r"^[A-Za-z0-9._-]+$",
        description="Recorded on every placement rendered with this set.",
    )
    templates: tuple[Template, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _check(self) -> Self:
        """Reject duplicate template names.

        Raises:
            ValueError: two templates share a name.
        """
        names = [template.name for template in self.templates]
        if len(set(names)) != len(names):
            raise ValueError("template names must be unique")
        return self

    def select(self, facts: Facts) -> Template | None:
        """The first template whose conditions match."""
        return next((t for t in self.templates if t.applies_to(facts)), None)


SEED_TEMPLATES: Final = TemplateSet(
    version="seed-1",
    templates=(
        Template(
            name="photos",
            when={"media_kind": "photo"},
            path="Photos/{captured_at:%Y}/{captured_at:%Y-%m}/{original_name}",
            required=("captured_at",),
        ),
        Template(
            name="videos",
            when={"media_kind": "video"},
            path="Videos/{captured_at:%Y}/{original_name}",
            required=("captured_at",),
        ),
        Template(
            name="documents",
            when={"category": "Documents"},
            path="Documents/{subcategory}/{original_name}",
            required=("subcategory",),
        ),
        Template(
            name="audio",
            when={"media_kind": "audio"},
            path="Audio/{subcategory}/{original_name}",
            required=("subcategory",),
        ),
        Template(name="everything-else", path="{category}/{original_name}", required=("category",)),
    ),
)


def parse_templates(
    document: Mapping[str, object], *, taxonomy: Taxonomy | None = None
) -> TemplateSet:
    """Parse a templates configuration document.

    Args:
        document: The document, in its JSON form.
        taxonomy: The taxonomy in force, when the set is being activated: every category
            and subcategory condition must be one it lists. Reads pass none.

    Raises:
        ValidationError: the document is not a valid template set.
    """
    return TemplateSet.model_validate(dict(document), strict=False, context={"taxonomy": taxonomy})
