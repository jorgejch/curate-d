"""A policy derives the destinations content should have, and validates itself."""

import pickle

import pytest
from pydantic import BaseModel, ValidationError

from curate_d.configuration.documents import parse_policy, parse_stored_policy
from curate_d.domain import (
    SEED_POLICY,
    Facts,
    MediaKind,
    PlacementPolicy,
    RetentionRule,
    VerificationLevel,
)
from curate_d.reconciliation.retention import RetentionOutcome, decide_retention

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
    assert parse_stored_policy(policy.model_dump(mode="json")) == policy


def _with_retired_flag(value: bool) -> dict[str, object]:
    """Return the policy document with `source_counts_as_copy` on its retention rule."""
    retention = _POLICY["retention"]
    assert isinstance(retention, list)
    return _POLICY | {"retention": [rule | {"source_counts_as_copy": value} for rule in retention]}


@pytest.mark.parametrize("value", [True, False])
def test_a_stored_policy_with_the_retired_flag_still_parses(value: bool) -> None:
    """A version stored before the flag was retired means the same policy without it."""
    assert parse_stored_policy(_with_retired_flag(value)) == _policy()


def test_a_submitted_policy_with_the_retired_flag_is_refused() -> None:
    """A new document carrying the flag is refused like any unknown field."""
    with pytest.raises(ValidationError) as refused:
        parse_policy(_with_retired_flag(True))
    assert [error["loc"] for error in refused.value.errors()] == [
        ("retention", 0, "source_counts_as_copy")
    ]


def test_a_stored_policy_is_still_parsed_strictly_in_shape() -> None:
    """Only the retired flag is dropped; another unknown field is still refused."""
    with pytest.raises(ValidationError):
        parse_stored_policy(_POLICY | {"retention": [{"name": "r", "keep_forever": True}]})


def _unpickled_with_retired_field[M: BaseModel](model: M) -> M:
    """Return `model` as a checkpoint pickled before the retirement would unpickle it."""
    state = model.__getstate__()
    state["__dict__"] = state["__dict__"] | {"source_counts_as_copy": True}
    loaded = type(model).__new__(type(model))
    loaded.__setstate__(state)
    unpickled = pickle.loads(pickle.dumps(loaded))
    assert isinstance(unpickled, type(model))
    return unpickled


def test_a_pickled_rule_with_the_retired_field_still_loads() -> None:
    """Step checkpoints taken before the retirement still decide, and drop the field."""
    rule = _unpickled_with_retired_field(
        RetentionRule(name="photos", release_source=True, required_copies=1)
    )
    assert "source_counts_as_copy" not in rule.model_dump()
    decision = _unpickled_with_retired_field(decide_retention(rule, []))
    assert decision.outcome is RetentionOutcome.RETAIN
    assert "source_counts_as_copy" not in decision.audit_details()
    assert "source_counts_as_copy" not in decision.model_dump(mode="json")
