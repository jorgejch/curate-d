"""A policy derives the destinations content should have, and validates itself."""

import pickle

import pytest
from pydantic import BaseModel, ValidationError

from curate_d.configuration.documents import parse_policy, parse_stored_policy
from curate_d.domain import (
    SEED_POLICY,
    SEED_TAXONOMY,
    CapabilityProfileName,
    DestinationConfig,
    Facts,
    MediaKind,
    PlacementPolicy,
    Provider,
    RetentionRule,
    VerificationLevel,
    check_destinations,
    derive_destination_id,
)
from curate_d.reconciliation.retention import RetentionDecision, RetentionOutcome
from curate_d.rendering.templates import PORTABLE_PROFILE, PROFILES

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


def test_selections_name_every_matching_rule_in_policy_order() -> None:
    """Two rules targeting one destination both appear; a rule that does not match does not."""
    finance = Facts(media_kind=MediaKind.DOCUMENT, category="Documents", subcategory="Finance")
    selections = _policy().selections(finance)
    assert selections == {"archive": ("everything", "finance-twice"), "backup": ("finance-twice",)}
    assert list(selections) == sorted(selections)
    assert "photos-twice" not in {rule for rules in selections.values() for rule in rules}


def test_selections_agree_with_the_desired_destinations() -> None:
    """The destinations are the selections' keys."""
    photo = Facts(media_kind=MediaKind.PHOTO, category="Photos")
    assert tuple(_policy().selections(photo)) == _policy().desired_destinations(photo)
    assert SEED_POLICY.selections(photo) == {}


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
    document = _POLICY | change
    with pytest.raises(ValidationError):
        parse_policy(document)


def test_a_policy_round_trips_through_json() -> None:
    """The stored form parses back to the same policy."""
    policy = _policy()
    assert parse_policy(policy.model_dump(mode="json")) == policy
    assert parse_stored_policy(policy.model_dump(mode="json")) == policy


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
    assert policy.destinations[0].profile == CapabilityProfileName.PORTABLE
    assert type(policy.destinations[0].profile) is str
    assert policy.model_dump(mode="json")["destinations"][0]["profile"] == "portable"
    assert parse_policy(policy.model_dump(mode="json")) == policy


def test_a_destination_defaults_to_the_pcloud_profile() -> None:
    """A destination naming no profile gets pCloud's."""
    assert _policy().destinations[0].profile == CapabilityProfileName.PCLOUD
    assert type(_policy().destinations[0].profile) is str


def test_an_unknown_profile_is_refused_as_an_enum_mismatch() -> None:
    """The refusal locates the profile and lists the names, as the enum-typed field did."""
    destination = {
        "name": "archive",
        "provider": "pcloud",
        "account": "1",
        "root_path": "/",
        "profile": "dropbox",
    }
    document = _POLICY | {"destinations": [destination], "rules": []}
    with pytest.raises(ValidationError) as refused:
        parse_policy(document)
    [error] = refused.value.errors()
    assert error["loc"] == ("destinations", 0, "profile")
    assert error["type"] == "enum"
    assert error["msg"] == "Input should be 'pcloud' or 'portable'"


def test_a_profile_given_as_the_member_is_held_as_its_plain_name() -> None:
    """A new checkpoint pickles the string, never the enum, as older builds did."""
    destination = DestinationConfig(
        name="archive",
        provider=Provider.PCLOUD,
        account="1",
        root_path="/",
        profile=CapabilityProfileName.PORTABLE,
    )
    assert type(destination.profile) is str
    assert type(pickle.loads(pickle.dumps(destination)).profile) is str


def test_a_destination_checkpointed_with_a_plain_profile_reads_and_plans() -> None:
    """A pickle holding the string profile satisfies the field and indexes `PROFILES`."""
    current = DestinationConfig(
        name="archive", provider=Provider.PCLOUD, account="1", root_path="/"
    )
    state = current.__getstate__()
    state["__dict__"] = state["__dict__"] | {"profile": "portable"}
    restored = DestinationConfig.__new__(DestinationConfig)
    restored.__setstate__(state)
    unpickled = pickle.loads(pickle.dumps(restored))
    assert isinstance(unpickled, DestinationConfig)
    assert unpickled.profile == CapabilityProfileName.PORTABLE
    assert PROFILES[CapabilityProfileName(unpickled.profile)] is PORTABLE_PROFILE


def _rule(when: dict[str, str]) -> dict[str, object]:
    """Return `_POLICY` with one placement rule selecting `when`."""
    return _POLICY | {"rules": [{"name": "x", "when": when, "destinations": ["archive"]}]}


@pytest.mark.parametrize(
    ("when", "message"),
    [
        ({"category": "Fotos"}, "category 'Fotos'"),
        ({"category": "Documents", "subcategory": "Receipts"}, "subcategory 'Receipts'"),
        ({"category": "Photos", "subcategory": "Finance"}, "subcategory 'Finance'"),
        ({"subcategory": "Receipts"}, "subcategory 'Receipts'"),
    ],
)
def test_against_a_taxonomy_an_unlisted_condition_value_is_refused(
    when: dict[str, str], message: str
) -> None:
    """The error locates the rule and names the value."""
    document = _rule(when)
    with pytest.raises(ValidationError) as refused:
        parse_policy(document, taxonomy=SEED_TAXONOMY)
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
    """A new document carrying the flag is refused like any unknown field, at its rule."""
    document = _with_retired_flag(True)
    with pytest.raises(ValidationError) as refused:
        parse_policy(document)
    assert [error["loc"] for error in refused.value.errors()] == [
        ("retention", 0, "source_counts_as_copy")
    ]


def test_a_stored_policy_is_still_parsed_strictly_in_shape() -> None:
    """Only the retired flag is dropped; another unknown field is still refused."""
    document = _POLICY | {"retention": [{"name": "r", "keep_forever": True}]}
    with pytest.raises(ValidationError):
        parse_stored_policy(document)


@pytest.mark.parametrize("retention", [["photos"], {"name": "photos"}])
def test_a_stored_policy_with_malformed_retention_is_refused(retention: object) -> None:
    """A retention section that is not a list of objects is refused, not dropped from."""
    document = _POLICY | {"retention": retention}
    with pytest.raises(ValidationError):
        parse_stored_policy(document)


def _unpickled_with_retired_field[M: BaseModel](model: M) -> M:
    """Return `model` as a checkpoint pickled before the retirement would unpickle it.

    Stored versions carry every field, so the old checkpoint also marks it as set.
    """
    state = model.__getstate__()
    state["__dict__"] = state["__dict__"] | {"source_counts_as_copy": True}
    state["__pydantic_fields_set__"] = state["__pydantic_fields_set__"] | {"source_counts_as_copy"}
    loaded = type(model).__new__(type(model))
    loaded.__setstate__(state)
    unpickled = pickle.loads(pickle.dumps(loaded))
    assert isinstance(unpickled, type(model))
    return unpickled


def test_a_pickled_rule_with_the_retired_field_still_loads() -> None:
    """Step checkpoints taken before the retirement still load, and drop the field."""
    rule = _unpickled_with_retired_field(
        RetentionRule(name="photos", release_source=True, required_copies=1)
    )
    assert "source_counts_as_copy" not in rule.model_dump()
    assert "source_counts_as_copy" not in rule.model_dump(exclude_unset=True)
    assert rule == RetentionRule(name="photos", release_source=True, required_copies=1)
    decision = _unpickled_with_retired_field(
        RetentionDecision(outcome=RetentionOutcome.RETAIN, reason="retained", rule=rule.name)
    )
    assert decision.outcome is RetentionOutcome.RETAIN
    assert "source_counts_as_copy" not in decision.audit_details()
    assert "source_counts_as_copy" not in decision.model_dump(mode="json")
    assert "source_counts_as_copy" not in decision.model_dump_json(exclude_unset=True)


def _destinations(*specs: tuple[str, str, str]) -> dict[str, object]:
    """Return a policy change defining destinations from (name, account, root) triples."""
    return {
        "rules": [],
        "retention": [],
        "destinations": [
            {"name": name, "provider": "pcloud", "account": account, "root_path": root}
            for name, account, root in specs
        ],
    }


@pytest.mark.parametrize(
    ("specs", "problem"),
    [
        ((("a", "1", "/Library"), ("b", "1", "/Library")), "'a' and 'b' are the same place"),
        ((("a", "1", "/Library"), ("b", "1", "/Library/")), "'a' and 'b' are the same place"),
        ((("a", "1", "/Library"), ("b", "1", "/Library/Photos")), "'a' .* and 'b' .* overlap"),
        ((("a", "1", "/Library/Photos"), ("b", "1", "/Library")), "'a' .* and 'b' .* overlap"),
        ((("a", "1", "/"), ("b", "1", "/Anything")), "'a' .* and 'b' .* overlap"),
    ],
)
def test_destinations_sharing_files_are_refused(
    specs: tuple[tuple[str, str, str], ...], problem: str
) -> None:
    """Two names for one place, or one root inside another in one account, would count twice."""
    document = _POLICY | _destinations(*specs)
    with pytest.raises(ValidationError, match=problem):
        parse_policy(document)


@pytest.mark.parametrize(
    "specs",
    [
        (("a", "1", "/Library"), ("b", "2", "/Library")),
        (("a", "1", "/a"), ("b", "1", "/ab")),
        (("a", "1", "/Library/Photos"), ("b", "1", "/Library/Video")),
    ],
)
def test_distinct_places_are_accepted(specs: tuple[tuple[str, str, str], ...]) -> None:
    """One root in two accounts, or sibling roots in one, are separate destinations."""
    policy = parse_policy(_POLICY | _destinations(*specs))
    assert len({d.id for d in policy.destinations}) == len(specs)


@pytest.mark.parametrize("root", ["Library", "/a//b", "/a/../b", "/a/./b"])
def test_a_root_that_does_not_normalize_is_refused_naming_the_destination(root: str) -> None:
    """A relative root, or one with an empty, "." or ".." segment, does not name one place."""
    document = _POLICY | _destinations(("a", "1", root))
    with pytest.raises(ValidationError, match="destination 'a': root path"):
        parse_policy(document)


def test_a_root_is_held_in_canonical_form() -> None:
    """A trailing slash is dropped, so the stored policy names the root one way."""
    policy = parse_policy(_POLICY | _destinations(("a", "1", "/Library/")))
    assert policy.destinations[0].root_path == "/Library"


def test_the_identity_is_derived_not_stored() -> None:
    """The stored document has no identity fields, so older documents and inputs still parse."""
    destination = _policy().destinations[0]
    assert set(destination.model_dump(mode="json")) == {
        "name",
        "provider",
        "account",
        "root_path",
        "profile",
    }
    assert destination.id == derive_destination_id("pcloud", "1", "/Organized")


def test_a_name_moved_to_another_account_parses_as_another_identity() -> None:
    """Redefining a name is allowed; the name is a label, the account is the identity."""
    before = parse_policy(_POLICY | _destinations(("a", "1", "/Library")))
    after = parse_policy(_POLICY | _destinations(("a", "2", "/Library")))
    assert before.destinations[0].id != after.destinations[0].id


def test_a_rename_keeps_the_identity() -> None:
    """Only provider, account and root decide the identity."""
    before = parse_policy(_POLICY | _destinations(("a", "1", "/Library")))
    after = parse_policy(_POLICY | _destinations(("b", "1", "/Library")))
    assert before.destinations[0].id == after.destinations[0].id


def test_destinations_by_id_maps_each_identity_to_its_name() -> None:
    """The policy's current name is what a decision reports a counted copy under."""
    assert _policy().destinations_by_id() == {
        derive_destination_id("pcloud", "1", "/Organized"): "archive",
        derive_destination_id("pcloud", "2", "/Backup"): "backup",
    }


def test_check_destinations_refuses_a_repeated_name_naming_it() -> None:
    """A repeated name is refused with the name in the message."""
    first = DestinationConfig(name="a", provider=Provider.PCLOUD, account="1", root_path="/x")
    second = DestinationConfig(name="a", provider=Provider.PCLOUD, account="2", root_path="/y")
    with pytest.raises(ValueError, match=r"repeated: \['a'\]"):
        check_destinations((first, second))


def test_check_destinations_refuses_an_unvalidated_root_naming_it() -> None:
    """A config built without validation still has its root checked."""
    unchecked = DestinationConfig.model_construct(
        name="a", provider=Provider.PCLOUD, account="1", root_path="/a/../b", profile="pcloud"
    )
    with pytest.raises(ValueError, match="destination 'a': root path"):
        check_destinations((unchecked,))
