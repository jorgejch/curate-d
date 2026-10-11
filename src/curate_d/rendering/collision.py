"""Two contents, one locator.

When a rendered locator is already occupied by different content, the new content gets a
deterministic suffix derived from its own fingerprint, so the same collision always
resolves to the same place and existing content is never overwritten. When it is
occupied by the same content, the existing placement already satisfies the requirement
and nothing is transferred.
"""

from pathlib import PurePosixPath

from curate_d.domain import ContentObjectId, DomainModel, Placement, StrongFingerprint

_SUFFIX_DIGITS = 8


class Resolution(DomainModel):
    """Where content goes, and whether it is already there."""

    locator: str
    already_present: bool = False


def disambiguate(locator: str, fingerprint: StrongFingerprint) -> str:
    """`locator` with the fingerprint's leading digits inserted before the extension."""
    path = PurePosixPath(locator)
    tag = fingerprint.digest[:_SUFFIX_DIGITS]
    return str(path.with_name(f"{path.stem} ({tag}){path.suffix}"))


def resolve(
    locator: str,
    occupant: Placement | None,
    *,
    content_object_id: ContentObjectId,
    fingerprint: StrongFingerprint,
) -> Resolution:
    """Resolve a rendered locator against whatever already occupies it."""
    if occupant is None:
        return Resolution(locator=locator)
    if occupant.content_object_id == content_object_id:
        return Resolution(locator=locator, already_present=True)
    return Resolution(locator=disambiguate(locator, fingerprint))
