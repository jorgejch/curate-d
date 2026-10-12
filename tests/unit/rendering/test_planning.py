"""Planning a placement from stored facts: a locator, or a failure for review."""

from datetime import UTC, datetime
from uuid import uuid4

from curate_d.domain import (
    AuditEntryType,
    ContentObjectId,
    Facts,
    HashAlgorithm,
    MediaKind,
    Placement,
    StrongFingerprint,
    derive_destination_id,
)
from curate_d.rendering.planning import (
    PlacementPlan,
    RenderFailure,
    original_name,
    plan,
    render_failure_audit,
)
from curate_d.rendering.templates import PCLOUD_PROFILE, SEED_TEMPLATES

_CONTENT = ContentObjectId(uuid4())
_FINGERPRINT = StrongFingerprint(algorithm=HashAlgorithm.SHA256, digest="d" * 64)
_NOW = datetime(2026, 9, 29, tzinfo=UTC)


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
        destination_id=derive_destination_id("pcloud", "acct-1", "/Organized"),
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
