"""Identifier derivation and the invariants built on top of it."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from curate_d.domain import (
    ChangeSignal,
    ContentObject,
    HashAlgorithm,
    Placement,
    Provider,
    SourceObject,
    StrongFingerprint,
    VerificationLevel,
    derive_destination_id,
    derive_placement_id,
    derive_source_object_id,
    normalize_root,
)

_WHEN = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def _fingerprint(digest_char: str) -> StrongFingerprint:
    """Build a SHA-256 fingerprint by repeating `digest_char` to a valid digest length."""
    return StrongFingerprint(algorithm=HashAlgorithm.SHA256, digest=digest_char * 64)


def test_equal_fingerprints_derive_one_content_object_identity() -> None:
    """Deduplication rests on this: same bytes, same row, classified once."""
    first = ContentObject.build(
        fingerprint=_fingerprint("a"), size_bytes=10, media_type="image/png"
    )
    second = ContentObject.build(
        fingerprint=_fingerprint("a"), size_bytes=10, media_type="image/png"
    )
    assert first.id == second.id


def test_different_fingerprints_derive_different_identities() -> None:
    """Distinct fingerprints derive distinct content object identities."""
    first = ContentObject.build(
        fingerprint=_fingerprint("a"), size_bytes=10, media_type="image/png"
    )
    second = ContentObject.build(
        fingerprint=_fingerprint("b"), size_bytes=10, media_type="image/png"
    )
    assert first.id != second.id


def test_rejects_a_media_type_that_is_not_type_subtype() -> None:
    """A media_type without a `type/subtype` shape is rejected."""
    with pytest.raises(ValidationError, match="type/subtype"):
        ContentObject.build(fingerprint=_fingerprint("a"), size_bytes=1, media_type="image")


def test_source_identity_survives_a_rename() -> None:
    """Provider-native identity anchors change detection; the path is just an attribute."""
    before = SourceObject.build(
        provider=Provider.PCLOUD,
        account="acct",
        native_id="42",
        path="/before.jpg",
        size_bytes=5,
        modified_at=_WHEN,
        version_marker="r1",
    )
    after = SourceObject.build(
        provider=Provider.PCLOUD,
        account="acct",
        native_id="42",
        path="/after.jpg",
        size_bytes=5,
        modified_at=_WHEN,
        version_marker="r1",
    )
    assert before.id == after.id


def test_identity_components_cannot_be_confused_by_concatenation() -> None:
    """A separator that can appear in a component would collapse distinct keys."""
    assert derive_source_object_id("pcloud", "ab", "c") != derive_source_object_id(
        "pcloud", "a", "bc"
    )


def test_absent_version_marker_records_the_weaker_change_signal() -> None:
    """Building without a version marker falls back to size-and-modified-time."""
    source = SourceObject.build(
        provider=Provider.PCLOUD,
        account="acct",
        native_id="42",
        path="/x.jpg",
        size_bytes=5,
        modified_at=_WHEN,
    )
    assert source.change_signal is ChangeSignal.SIZE_AND_MODIFIED_TIME


def test_one_content_object_supports_placements_at_several_destinations() -> None:
    """Placements for the same content object at different destinations have distinct ids."""
    content = ContentObject.build(
        fingerprint=_fingerprint("c"), size_bytes=1, media_type="image/png"
    )
    archive_id = derive_destination_id("pcloud", "acct-1", "/Archive")
    archive = Placement.build(
        content_object_id=content.id,
        destination_id=archive_id,
        destination_name="archive",
        locator="/Archive/x.png",
        template_version="v1",
    )
    working = Placement.build(
        content_object_id=content.id,
        destination_id=derive_destination_id("pcloud", "acct-1", "/Working"),
        destination_name="working",
        locator="/Working/x.png",
        template_version="v1",
    )
    assert archive.id != working.id
    assert archive.id == derive_placement_id(content.id, archive_id)


@pytest.mark.parametrize(
    ("level", "permitted"),
    [
        (VerificationLevel.UNVERIFIABLE, False),
        (VerificationLevel.WEAK, False),
        (VerificationLevel.STRONG_IN_TRANSIT, True),
        (VerificationLevel.STRONG_COMMON_HASH, True),
    ],
)
def test_only_strong_verification_can_unlock_source_release(
    level: VerificationLevel, permitted: bool
) -> None:
    """Only the two strong verification levels permit source release."""
    assert level.permits_source_release is permitted


def test_a_placement_starts_unverified_and_not_present() -> None:
    """A copy that finished is not a copy that was checked."""
    placement = Placement.build(
        content_object_id=ContentObject.build(
            fingerprint=_fingerprint("d"), size_bytes=1, media_type="image/png"
        ).id,
        destination_id=derive_destination_id("pcloud", "acct-1", "/Archive"),
        destination_name="archive",
        locator="/Archive/x.png",
        template_version="v1",
    )
    assert not placement.verification_level.permits_source_release


def test_a_renamed_destination_keeps_its_identity() -> None:
    """The name is no input: one provider, account and root derive one identifier."""
    assert derive_destination_id("pcloud", "acct-1", "/Library") == derive_destination_id(
        "pcloud", "acct-1", "/Library"
    )


@pytest.mark.parametrize(
    ("provider", "account", "root"),
    [("other", "acct-1", "/Library"), ("pcloud", "acct-2", "/Library"), ("pcloud", "acct-1", "/X")],
)
def test_each_part_of_a_destination_identity_distinguishes_it(
    provider: str, account: str, root: str
) -> None:
    """Provider, account and root each change the destination identifier."""
    assert derive_destination_id(provider, account, root) != derive_destination_id(
        "pcloud", "acct-1", "/Library"
    )


def test_a_trailing_slash_names_the_same_destination() -> None:
    """ "/Library/" and "/Library" are one root."""
    assert derive_destination_id("pcloud", "a", "/Library/") == derive_destination_id(
        "pcloud", "a", "/Library"
    )


@pytest.mark.parametrize(
    ("root", "canonical"), [("/", "/"), ("///", "/"), ("/a/", "/a"), ("/a/b//", "/a/b")]
)
def test_roots_normalize_to_one_form(root: str, canonical: str) -> None:
    """Trailing slashes are dropped, except on the root of the account."""
    assert normalize_root(root) == canonical


@pytest.mark.parametrize("root", ["a/b", "/a//b", "/a/./b", "/a/../b", "/.."])
def test_ambiguous_or_relative_roots_are_refused(root: str) -> None:
    """A root that is relative, or has an empty, "." or ".." segment, is refused."""
    with pytest.raises(ValueError, match="root path"):
        normalize_root(root)


def test_one_name_in_two_accounts_derives_two_placements() -> None:
    """A placement belongs to the destination identity, not the name it was placed under."""
    content = ContentObject.build(
        fingerprint=_fingerprint("e"), size_bytes=1, media_type="image/png"
    )
    first = derive_placement_id(content.id, derive_destination_id("pcloud", "a", "/Library"))
    second = derive_placement_id(content.id, derive_destination_id("pcloud", "b", "/Library"))
    assert first != second
