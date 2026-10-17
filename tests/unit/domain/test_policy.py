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


def test_destination_names_cover_every_defined_destination() -> None:
    """Including one no rule targets for the content at hand."""
    assert _policy().destination_names() == {"archive", "backup"}
    assert SEED_POLICY.destination_names() == frozenset()


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
