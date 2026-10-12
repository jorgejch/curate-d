"""Claiming a destination locator against what already holds each candidate."""

from uuid import uuid4

import pytest

from curate_d.domain import (
    ContentObjectId,
    HashAlgorithm,
    Placement,
    StrongFingerprint,
    derive_destination_id,
)
from curate_d.placement.pipeline import LocatorConflictError, claim
from curate_d.rendering.collision import disambiguate

_LOCATOR = "/Photos/2024/2024-05/IMG_0001.JPG"
_MINE = ContentObjectId(uuid4())
_FINGERPRINT = StrongFingerprint(algorithm=HashAlgorithm.SHA256, digest="ab" * 32)


def _held_by(content_object_id: ContentObjectId, locator: str) -> Placement:
    """A placement of `content_object_id` at `locator`."""
    return Placement.build(
        content_object_id=content_object_id,
        destination_id=derive_destination_id("pcloud", "acct-1", "/"),
        destination_name="library",
        locator=locator,
        template_version="v1",
    )


async def _claim(held: dict[str, Placement]) -> tuple[str, ContentObjectId | None]:
    """Claim the rendered locator against `held`, by locator."""

    async def holder(candidate: str) -> Placement | None:
        """What `held` has at `candidate`."""
        return held.get(candidate)

    return await claim(_LOCATOR, content_object_id=_MINE, fingerprint=_FINGERPRINT, holder=holder)


async def test_a_free_locator_is_claimed_as_rendered() -> None:
    """Nothing there: the rendered locator."""
    assert await _claim({}) == (_LOCATOR, None)


async def test_this_contents_own_locator_is_kept() -> None:
    """A retry is not displaced by its own earlier claim."""
    assert await _claim({_LOCATOR: _held_by(_MINE, _LOCATOR)}) == (_LOCATOR, None)


async def test_other_content_pushes_this_one_to_its_fingerprint_suffix() -> None:
    """The collision resolves as rendering resolves it, naming who held the path."""
    other = ContentObjectId(uuid4())
    claimed = await _claim({_LOCATOR: _held_by(other, _LOCATOR)})
    assert claimed == (disambiguate(_LOCATOR, _FINGERPRINT), other)


async def test_a_suffixed_locator_this_content_holds_is_kept() -> None:
    """Pushed aside before, the content finds its own suffixed copy."""
    other = ContentObjectId(uuid4())
    suffixed = disambiguate(_LOCATOR, _FINGERPRINT)
    held = {_LOCATOR: _held_by(other, _LOCATOR), suffixed: _held_by(_MINE, suffixed)}
    assert await _claim(held) == (suffixed, other)


async def test_every_candidate_held_by_other_content_is_a_conflict() -> None:
    """No free locator is an error rather than an overwrite."""
    other = ContentObjectId(uuid4())
    held: dict[str, Placement] = {}
    candidate = _LOCATOR
    for _ in range(8):
        held[candidate] = _held_by(other, candidate)
        candidate = disambiguate(candidate, _FINGERPRINT)
    with pytest.raises(LocatorConflictError, match="no free locator"):
        await _claim(held)
