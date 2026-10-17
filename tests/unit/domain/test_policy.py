"""A policy derives the destinations content should have, and validates itself."""

import pytest
from pydantic import ValidationError

from curate_d.configuration.documents import parse_policy
from curate_d.domain import (
    SEED_POLICY,
    Facts,
    MediaKind,
    PlacementPolicy,
    VerificationLevel,
)

_POLICY = {
    "destinations": [
        {"name": "archive", "provider": "pcloud", "account": "1", "root_path": "/Organized"},
        {"name": "backup", "provider": "pcloud", "account": "2", "root_path": "/Backup"},
    ],
    "rules": [
        {"name": "everything", "destinations": ["archive"]},
        {"name": "photos-twice", "when": {"media_kind": "photo"}, "destinations": ["backup"]},
        {
            "name": "finance-twice",
            "when": {"category": "Documents", "subcategory": "Finance"},
            "destinations": ["backup", "archive"],
        },
    ],
    "retention": [
        {
            "name": "photos",
            "when": {"media_kind": "photo"},
            "release_source": True,
            "required_copies": 2,
        }
    ],
}


def _policy() -> PlacementPolicy:
    """The policy under test."""
    return parse_policy(_POLICY)


@pytest.mark.parametrize(
    ("facts", "expected"),
    [
        (Facts(media_kind=MediaKind.PHOTO, category="Photos"), ("archive", "backup")),
        (
            Facts(media_kind=MediaKind.DOCUMENT, category="Documents", subcategory="Finance"),
            ("archive", "backup"),
        ),
        (
            Facts(media_kind=MediaKind.DOCUMENT, category="Documents", subcategory="Home"),
            ("archive",),
        ),
        (Facts(media_kind=MediaKind.AUDIO), ("archive",)),
    ],
)
def test_a_policy_derives_the_expected_destinations(
    facts: Facts, expected: tuple[str, ...]
) -> None:
    """Every matching rule contributes; the result is stable and duplicate-free."""
    assert _policy().desired_destinations(facts) == expected


def test_the_seed_policy_places_nothing() -> None:
    """Nothing moves until a person says where things go."""
    assert SEED_POLICY.desired_destinations(Facts(media_kind=MediaKind.PHOTO)) == ()


def test_retention_defaults_to_retain() -> None:
    """Content no retention rule matches has no rule, which means keep the original."""
    policy = _policy()
    assert policy.retention_for(Facts(media_kind=MediaKind.DOCUMENT)) is None
    rule = policy.retention_for(Facts(media_kind=MediaKind.PHOTO))
    assert rule is not None and rule.release_source


@pytest.mark.parametrize(
    "change",
    [
        {"rules": [{"name": "x", "destinations": ["nowhere"]}]},
        {
            "destinations": [
                {"name": "a", "provider": "pcloud", "account": "1", "root_path": "/"},
                {"name": "a", "provider": "pcloud", "account": "2", "root_path": "/"},
            ]
        },
        {"rules": [{"name": "x", "when": {"title": "t"}, "destinations": ["archive"]}]},
        {
            "retention": [
                {"name": "r", "release_source": True, "minimum_level": VerificationLevel.WEAK.value}
            ]
        },
        {
            "destinations": [
                {"name": "Bad Name", "provider": "pcloud", "account": "1", "root_path": "/"}
            ]
        },
    ],
)
def test_an_invalid_policy_is_rejected(change: dict[str, object]) -> None:
    """Undefined destinations, duplicate names, bad conditions and weak release all fail."""
    with pytest.raises(ValidationError):
        parse_policy(_POLICY | change)


def test_a_policy_round_trips_through_json() -> None:
    """The stored form parses back to the same policy."""
    policy = _policy()
    assert parse_policy(policy.model_dump(mode="json")) == policy


def _destinations(*specs: tuple[str, str, str]) -> dict[str, object]:
    """Return a policy change defining destinations from (name, account, root) triples."""
    return {
        "rules": [],
        "destinations": [
            {"name": name, "provider": "pcloud", "account": account, "root_path": root}
            for name, account, root in specs
        ],
    }


@pytest.mark.parametrize(
    ("specs", "problem"),
    [
        ((("a", "1", "/Library"), ("b", "1", "/Library/")), "are the same place"),
        ((("a", "1", "/Library"), ("b", "1", "/Library/Photos")), "overlap"),
        ((("a", "1", "/Library/Photos"), ("b", "1", "/Library")), "overlap"),
        ((("a", "1", "/"), ("b", "1", "/Anything")), "overlap"),
    ],
)
def test_destinations_sharing_files_are_refused(
    specs: tuple[tuple[str, str, str], ...], problem: str
) -> None:
    """Two names for one place, or one root inside another in one account, would count twice."""
    with pytest.raises(ValidationError, match=problem):
        parse_policy(_POLICY | _destinations(*specs))


@pytest.mark.parametrize(
    "specs",
    [
        (("a", "1", "/Library"), ("b", "2", "/Library")),
        (("a", "1", "/Library"), ("b", "1", "/Library2")),
        (("a", "1", "/Library/Photos"), ("b", "1", "/Library/Video")),
    ],
)
def test_distinct_places_are_accepted(specs: tuple[tuple[str, str, str], ...]) -> None:
    """One root in two accounts, or sibling roots in one, are separate destinations."""
    policy = parse_policy(_POLICY | _destinations(*specs))
    assert len({d.id for d in policy.destinations}) == len(specs)


@pytest.mark.parametrize("root", ["/a//b", "/a/../b", "/a/./b"])
def test_an_ambiguous_root_is_refused(root: str) -> None:
    """A root with an empty, "." or ".." segment does not name one place."""
    with pytest.raises(ValidationError, match="root path"):
        parse_policy(_POLICY | _destinations(("a", "1", root)))


def test_a_root_is_stored_in_canonical_form() -> None:
    """A trailing slash is dropped, so the stored policy names the root one way."""
    policy = parse_policy(_POLICY | _destinations(("a", "1", "/Library/")))
    assert policy.destinations[0].root_path == "/Library"


def test_the_identity_is_derived_not_stored() -> None:
    """The stored document has no identity fields, so older documents and inputs still parse."""
    destination = _policy().destinations[0]
    dumped = destination.model_dump(mode="json")
    assert set(dumped) == {"name", "provider", "account", "root_path", "profile"}
    assert destination.identity.id == destination.id


def test_a_rename_keeps_the_identity_and_a_new_account_changes_it() -> None:
    """The name is a label: only provider, account and root decide the identity."""
    archive = _policy().destinations[0]
    renamed = archive.model_copy(update={"name": "renamed"})
    moved = archive.model_copy(update={"account": "elsewhere"})
    assert renamed.id == archive.id
    assert moved.id != archive.id
