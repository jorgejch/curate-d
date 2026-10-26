"""Planning a placement from stored facts: a locator, or a failure for review."""

from datetime import UTC, datetime
from typing import cast
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection

from curate_d.domain import (
    AuditEntryType,
    ContentObjectId,
    DestinationConfig,
    DestinationId,
    Facts,
    HashAlgorithm,
    MediaKind,
    Placement,
    Provider,
    StrongFingerprint,
    derive_destination_id,
)
from curate_d.rendering import planning
from curate_d.rendering.planning import (
    PlacementPlan,
    RenderFailure,
    original_name,
    plan,
    plan_at,
    render_failure_audit,
)
from curate_d.rendering.templates import PCLOUD_PROFILE, SEED_TEMPLATES

_CONTENT = ContentObjectId(uuid4())
_FINGERPRINT = StrongFingerprint(algorithm=HashAlgorithm.SHA256, digest="d" * 64)
_NOW = datetime(2026, 9, 29, tzinfo=UTC)
_ARCHIVE = DestinationConfig(
    name="archive", provider=Provider.PCLOUD, account="acct-1", root_path="/Organized"
)
_ARCHIVE_ID = derive_destination_id("pcloud", "acct-1", "/Organized")
_PHOTO = Facts(media_kind=MediaKind.PHOTO, category="Photos", captured_at=_NOW)
_PLAIN = "/Organized/Photos/2026/2026-09/first.jpg"


def _plan(
    facts: Facts, occupants: dict[str, Placement] | None = None
) -> PlacementPlan | RenderFailure:
    """Plan `facts` for the archive destination."""
    return plan(
        SEED_TEMPLATES,
        facts,
        content_object_id=_CONTENT,
        fingerprint=_FINGERPRINT,
        paths=("/b/second.jpg", "/a/first.jpg"),
        destination_id=_ARCHIVE_ID,
        destination_name="archive",
        profile=PCLOUD_PROFILE,
        root="/Organized",
        occupants=occupants or {},
    )


def test_a_photo_with_its_facts_gets_a_plan() -> None:
    """The first source name by path is the one filed."""
    result = _plan(Facts(media_kind=MediaKind.PHOTO, category="Photos", captured_at=_NOW))
    assert isinstance(result, PlacementPlan)
    assert result.locator == "/Organized/Photos/2026/2026-09/first.jpg"
    assert result.rendered.template_version == "seed-1"


def test_a_missing_fact_is_a_failure_naming_it_and_no_plan() -> None:
    """Nothing to place, and the reason says which fact is missing."""
    result = _plan(Facts(media_kind=MediaKind.PHOTO, category="Photos"))
    assert isinstance(result, RenderFailure)
    assert result.missing == ("captured_at",)
    entry = render_failure_audit(result, now=_NOW)
    assert entry.entry_type is AuditEntryType.RENDER_FAILED
    assert entry.details["missing"] == "captured_at"
    assert entry.content_object_id == _CONTENT


def test_an_occupied_locator_is_resolved_in_the_plan() -> None:
    """Different content at the locator moves this content aside."""
    other = Placement.build(
        content_object_id=ContentObjectId(uuid4()),
        destination_id=_ARCHIVE_ID,
        destination_name="archive",
        locator="/Organized/Photos/2026/2026-09/first.jpg",
        template_version="seed-1",
    )
    result = _plan(
        Facts(media_kind=MediaKind.PHOTO, category="Photos", captured_at=_NOW),
        {other.locator: other},
    )
    assert isinstance(result, PlacementPlan)
    assert result.locator == "/Organized/Photos/2026/2026-09/first (dddddddd).jpg"


def test_a_content_without_names_is_filed_as_unnamed() -> None:
    """A content object always has a source, but the fallback is explicit."""
    assert original_name([]) == "unnamed"
    assert original_name(["/z/b.txt", "/a/c.txt"]) == "c.txt"


class _Occupied:
    """A placement repository holding one placement, found only at its own identity."""

    held: Placement

    def __init__(self, connection: object) -> None:
        """Ignore the connection."""

    async def find_at_locator(
        self, *, destination_id: DestinationId, locator: str
    ) -> Placement | None:
        """Return the held placement when it is at this identity and locator."""
        held = self.held
        return held if (held.destination_id, held.locator) == (destination_id, locator) else None


async def _plan_at(
    monkeypatch: pytest.MonkeyPatch, held: Placement, destination: DestinationConfig
) -> PlacementPlan | RenderFailure:
    """Plan the photo at `destination` against a ledger holding `held`."""
    monkeypatch.setattr(_Occupied, "held", held, raising=False)
    monkeypatch.setattr(planning, "PlacementRepository", _Occupied)
    return await plan_at(
        cast(AsyncConnection, None),
        SEED_TEMPLATES,
        _PHOTO,
        content_object_id=_CONTENT,
        fingerprint=_FINGERPRINT,
        paths=("/a/first.jpg",),
        destination=destination,
    )


def _other_at(account: str, name: str) -> Placement:
    """Return other content's placement at `_PLAIN` of `/Organized` in `account`."""
    return Placement.build(
        content_object_id=ContentObjectId(uuid4()),
        destination_id=derive_destination_id("pcloud", account, "/Organized"),
        destination_name=name,
        locator=_PLAIN,
        template_version="seed-1",
    )


async def test_an_occupant_at_the_same_identity_is_a_collision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The plan names its identity and moves aside from other content there."""
    result = await _plan_at(monkeypatch, _other_at("acct-1", "archive"), _ARCHIVE)
    assert isinstance(result, PlacementPlan)
    assert result.destination_id == _ARCHIVE_ID
    assert result.locator != _PLAIN


async def test_an_occupant_at_another_identity_is_no_collision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Other content at one locator in another account takes nothing from this one."""
    result = await _plan_at(monkeypatch, _other_at("acct-2", "backup"), _ARCHIVE)
    assert isinstance(result, PlacementPlan)
    assert result.locator == _PLAIN


async def test_an_occupant_at_a_names_previous_account_is_no_collision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A placement made under `archive` before it moved accounts holds no path at the new one."""
    moved = _ARCHIVE.model_copy(update={"account": "acct-new"})
    result = await _plan_at(monkeypatch, _other_at("acct-1", "archive"), moved)
    assert isinstance(result, PlacementPlan)
    assert result.locator == _PLAIN
