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
reaches it.

A capability profile describes what a destination accepts: whether it nests folders,
which characters names may not contain, and how long a name may be.
"""

import re
from collections.abc import Mapping
from typing import Final, Self

from pydantic import Field, model_validator

from curate_d.domain import DomainModel, Facts

# Values rendering computes itself rather than reading from facts.
BUILTINS: Final = frozenset({"original_name", "original_stem", "extension", "content_id"})
FACT_FIELDS: Final = frozenset(Facts.model_fields)
_TIMESTAMP_FACTS: Final = frozenset({"captured_at"})
_PLACEHOLDER: Final = re.compile(r"\{([a-z_]+)(?::([^{}]+))?\}")
_CONDITION_FIELDS: Final = frozenset({"media_kind", "category", "subcategory"})


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
PROFILES: Final[Mapping[str, CapabilityProfile]] = {
    "pcloud": PCLOUD_PROFILE,
    "portable": PORTABLE_PROFILE,
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
    def _check(self) -> Self:
        """Reject unknown placeholders, requirements and conditions, and absolute paths.

        Raises:
            ValueError: the template names something rendering does not know.
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
        for field in self.when:
            if field not in _CONDITION_FIELDS:
                raise ValueError(
                    f"template {self.name!r} selects on {field!r}; only "
                    f"{', '.join(sorted(_CONDITION_FIELDS))} can select a template"
                )
        if self.path.startswith("/") or "//" in self.path or ".." in self.path.split("/"):
            raise ValueError(f"template {self.name!r} path must be relative and plain")
        return self

    def applies_to(self, facts: Facts) -> bool:
        """Whether every condition matches the facts."""
        values = facts.model_dump(mode="json")
        return all(values.get(field) == value for field, value in self.when.items())


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


def parse_templates(document: Mapping[str, object]) -> TemplateSet:
    """Parse a templates configuration document.

    Raises:
        ValidationError: the document is not a valid template set.
    """
    return TemplateSet.model_validate(dict(document), strict=False)
