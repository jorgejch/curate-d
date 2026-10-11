"""Rendering facts into a destination locator.

A pure function of the facts, the original file name, the content's identity, the
template set and the destination's capability profile. It reads no clock, no randomness
and nothing at the destination, so the same inputs always give the same locator, and a
new template set re-renders the whole library from stored facts without classifying
anything again.

Names are normalized by one documented rule, applied in this order to each component:

1. Unicode NFC, so a name typed on two systems is one name.
2. Every character the destination disallows, the separator, NUL and control characters
   become `%XX` per UTF-8 byte, as in a URL.
3. A component that is `.` or `..`, or a reserved name, gets `_` appended.
4. A component longer than the destination allows keeps as much of its stem as fits
   beside `~` and the first eight hex digits of its SHA-256, and keeps its extension.

Every component the rule changed is recorded with its original, so the original value is
always recoverable from the placement's audit entry.
"""

import hashlib
import unicodedata
from datetime import datetime
from pathlib import PurePosixPath
from typing import Final

from pydantic import Field

from curate_d.domain import ContentObjectId, DomainModel, Facts
from curate_d.rendering.templates import CapabilityProfile, Template, TemplateSet, placeholders

_DEFAULT_TIME_FORMAT: Final = "%Y-%m-%d"
_FLAT_JOINER: Final = " - "


class RenderError(ValueError):
    """Facts cannot be rendered: no template applies, or required facts are missing."""

    def __init__(self, message: str, *, missing: tuple[str, ...] = ()) -> None:
        """Name what is missing."""
        self.missing = missing
        super().__init__(message)


class Transformation(DomainModel):
    """One path component the normalization rule changed."""

    original: str
    rendered: str
    rule: str


class Rendered(DomainModel):
    """A locator and how it was produced."""

    locator: str = Field(min_length=1)
    template_name: str
    template_version: str
    transformations: tuple[Transformation, ...] = ()


def _value(facts: Facts, name: str, time_format: str | None, builtins: dict[str, str]) -> str:
    """One placeholder's text; empty when an optional fact is absent."""
    if name in builtins:
        return builtins[name]
    value = getattr(facts, name)
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime(time_format or _DEFAULT_TIME_FORMAT)
    if hasattr(value, "value"):
        return str(value.value)
    return str(value)


def _escape(component: str, profile: CapabilityProfile) -> str:
    """Percent-encode every character the destination cannot hold in a name."""
    escaped: list[str] = []
    for character in component:
        if (
            character == "/"
            or character in profile.disallowed_characters
            or ord(character) < 0x20
            or ord(character) == 0x7F
        ):
            escaped.append("".join(f"%{byte:02X}" for byte in character.encode("utf-8")))
        else:
            escaped.append(character)
    return "".join(escaped)


def _shorten(component: str, limit: int) -> str:
    """Fit a component into `limit` UTF-8 bytes, keeping its extension and a digest."""
    digest = hashlib.sha256(component.encode("utf-8")).hexdigest()[:8]
    suffix = PurePosixPath(component).suffix
    if len(suffix.encode("utf-8")) > limit // 2:
        suffix = ""
    stem = component[: len(component) - len(suffix)] if suffix else component
    marker = f"~{digest}{suffix}"
    budget = limit - len(marker.encode("utf-8"))
    kept = stem.encode("utf-8")[:budget].decode("utf-8", errors="ignore")
    return f"{kept}{marker}"


def normalize(component: str, profile: CapabilityProfile) -> tuple[str, list[str]]:
    """Apply the normalization rule to one component, naming each step that changed it."""
    rules: list[str] = []
    result = unicodedata.normalize("NFC", component)
    if result != component:
        rules.append("nfc")
    escaped = _escape(result, profile)
    if escaped != result:
        rules.append("escaped")
        result = escaped
    if result in {".", ".."} or result.upper() in profile.reserved_names:
        result = f"{result}_"
        rules.append("reserved")
    if len(result.encode("utf-8")) > profile.max_component_bytes:
        result = _shorten(result, profile.max_component_bytes)
        rules.append("shortened")
    return result, rules


def render_with(
    template: Template,
    template_version: str,
    facts: Facts,
    *,
    original_name: str,
    content_object_id: ContentObjectId,
    profile: CapabilityProfile,
    root: str,
) -> Rendered:
    """Render with one given template.

    Raises:
        RenderError: a fact the template requires is absent.
    """
    missing = tuple(field for field in template.required if getattr(facts, field) is None)
    if missing:
        raise RenderError(
            f"template {template.name!r} requires {', '.join(missing)}, which the facts lack",
            missing=missing,
        )
    original = PurePosixPath(original_name)
    builtins = {
        "original_name": original.name,
        "original_stem": original.stem,
        "extension": original.suffix.lower(),
        "content_id": str(content_object_id)[:8],
    }
    components: list[str] = []
    transformations: list[Transformation] = []
    for segment in template.path.split("/"):
        text = segment
        for placeholder in placeholders(segment):
            token = (
                f"{{{placeholder.name}}}"
                if placeholder.format is None
                else f"{{{placeholder.name}:{placeholder.format}}}"
            )
            text = text.replace(
                token, _value(facts, placeholder.name, placeholder.format, builtins)
            )
        text = text.strip()
        if not text:
            continue
        normalized, rules = normalize(text, profile)
        if rules:
            transformations.append(
                Transformation(original=text, rendered=normalized, rule="+".join(rules))
            )
        components.append(normalized)
    if not components:
        raise RenderError(f"template {template.name!r} rendered an empty path")
    if not profile.hierarchical and len(components) > 1:
        flat, rules = normalize(_FLAT_JOINER.join(components), profile)
        if rules:
            transformations.append(
                Transformation(
                    original=_FLAT_JOINER.join(components), rendered=flat, rule="+".join(rules)
                )
            )
        components = [flat]
    return Rendered(
        locator=f"{root.rstrip('/')}/{'/'.join(components)}",
        template_name=template.name,
        template_version=template_version,
        transformations=tuple(transformations),
    )


def render(
    templates: TemplateSet,
    facts: Facts,
    *,
    original_name: str,
    content_object_id: ContentObjectId,
    profile: CapabilityProfile,
    root: str,
) -> Rendered:
    """Render facts with the first template that applies.

    Raises:
        RenderError: no template applies, or the one that does lacks a required fact.
    """
    template = templates.select(facts)
    if template is None:
        raise RenderError("no template applies to these facts")
    return render_with(
        template,
        templates.version,
        facts,
        original_name=original_name,
        content_object_id=content_object_id,
        profile=profile,
        root=root,
    )
