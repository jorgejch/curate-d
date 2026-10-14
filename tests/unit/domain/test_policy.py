"""A policy derives the destinations content should have, and validates itself."""

import pytest
from pydantic import ValidationError

from curate_d.configuration.documents import parse_policy
from curate_d.domain import (
    SEED_POLICY,
    SEED_TAXONOMY,
    CapabilityProfileName,
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
        {"rules": [{"name": "x", "when": {"media_kind": "photos"}, "destinations": ["archive"]}]},
        {"retention": [{"name": "r", "when": {"media_kind": "photos"}}]},
        {"rules": [{"name": "x", "when": {"category": "Photos "}, "destinations": ["archive"]}]},
        {
            "destinations": [
                {
                    "name": "archive",
                    "provider": "pcloud",
                    "account": "1",
                    "root_path": "/",
                    "profile": "dropbox",
                }
            ],
            "rules": [],
        },
    ],
)
def test_an_invalid_policy_is_rejected(change: dict[str, object]) -> None:
    """Undefined destinations, duplicates, bad conditions, weak release, unknown profiles fail."""
    with pytest.raises(ValidationError):
        parse_policy(_POLICY | change)


def test_a_policy_round_trips_through_json() -> None:
    """The stored form parses back to the same policy."""
    policy = _policy()
    assert parse_policy(policy.model_dump(mode="json")) == policy


@pytest.mark.parametrize("kind", list(MediaKind))
def test_every_media_kind_is_accepted_as_a_condition(kind: MediaKind) -> None:
    """Placement and retention rules can select any `MediaKind`."""
    when = {"media_kind": kind.value}
    policy = parse_policy(
        _POLICY
        | {
            "rules": [{"name": "x", "when": when, "destinations": ["archive"]}],
            "retention": [{"name": "r", "when": when}],
        }
    )
    assert policy.desired_destinations(Facts(media_kind=kind)) == ("archive",)


def test_a_policy_with_the_portable_profile_round_trips() -> None:
    """The profile parses from its JSON name and is stored as it."""
    document = _POLICY | {
        "destinations": [
            {
                "name": "archive",
                "provider": "pcloud",
                "account": "1",
                "root_path": "/Organized",
                "profile": "portable",
            }
        ],
        "rules": [],
    }
    policy = parse_policy(document)
    assert policy.destinations[0].profile is CapabilityProfileName.PORTABLE
    assert policy.model_dump(mode="json")["destinations"][0]["profile"] == "portable"
    assert parse_policy(policy.model_dump(mode="json")) == policy


def test_a_destination_defaults_to_the_pcloud_profile() -> None:
    """A destination naming no profile gets pCloud's."""
    assert _policy().destinations[0].profile is CapabilityProfileName.PCLOUD


def _rule(when: dict[str, str]) -> dict[str, object]:
    """Return `_POLICY` with one placement rule selecting `when`."""
    return _POLICY | {"rules": [{"name": "x", "when": when, "destinations": ["archive"]}]}


@pytest.mark.parametrize(
    ("when", "message"),
    [
        ({"category": "Fotos"}, "category 'Fotos'"),
        ({"category": "Documents", "subcategory": "Receipts"}, "subcategory 'Receipts'"),
        ({"subcategory": "Receipts"}, "subcategory 'Receipts'"),
    ],
)
def test_against_a_taxonomy_an_unlisted_condition_value_is_refused(
    when: dict[str, str], message: str
) -> None:
    """The error locates the rule and names the value."""
    with pytest.raises(ValidationError) as refused:
        parse_policy(_rule(when), taxonomy=SEED_TAXONOMY)
    [error] = refused.value.errors()
    assert error["loc"] == ("rules", 0)
    assert message in error["msg"]
    assert "placement rule 'x'" in error["msg"]


def test_against_a_taxonomy_a_retention_condition_is_checked_too() -> None:
    """Retention rules select content the same way."""
    document = _POLICY | {"retention": [{"name": "r", "when": {"category": "Fotos"}}]}
    with pytest.raises(ValidationError, match="retention rule 'r' selects category 'Fotos'"):
        parse_policy(document, taxonomy=SEED_TAXONOMY)


def test_against_a_taxonomy_a_listed_subcategory_alone_is_accepted() -> None:
    """A subcategory with no category needs only to be listed under some category."""
    parse_policy(_rule({"subcategory": "Finance"}), taxonomy=SEED_TAXONOMY)


def test_the_test_policy_passes_against_the_seed_taxonomy() -> None:
    """Every value the policy under test names is listed."""
    assert parse_policy(_POLICY, taxonomy=SEED_TAXONOMY) == _policy()


def test_without_a_taxonomy_an_unlisted_category_is_read() -> None:
    """Reads stay tolerant: a stored policy is never re-checked against the taxonomy."""
    assert parse_policy(_rule({"category": "Fotos"})).rules[0].when == {"category": "Fotos"}
