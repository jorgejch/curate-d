"""Configuration endpoints over a fake store: view, change, refuse, and list versions."""

from dataclasses import replace

import pytest

from curate_d.api.configuration import OPERATOR_HEADER, check_submission
from curate_d.api.errors import RejectedError
from curate_d.configuration.documents import parse_policy
from curate_d.domain import SEED_TAXONOMY, ConfigurationKind, TaxonomyField
from curate_d.rendering.templates import SEED_TEMPLATES, parse_templates
from tests.unit.api.fakes import FakeConfiguration, backends, client


def _templates(**changes: object) -> dict[str, object]:
    """The seed template set as JSON, its first template changed by `changes`."""
    document = SEED_TEMPLATES.model_dump(mode="json")
    document["templates"][0].update(changes)
    return document


def test_each_kind_reports_the_version_in_force() -> None:
    """Seeds are version 1."""
    with client() as http:
        for kind in ("taxonomy", "templates", "policy"):
            response = http.get(f"/configuration/{kind}")
            assert response.status_code == 200
            assert response.json()["version"] == 1


def test_a_valid_template_change_becomes_the_next_version() -> None:
    """Stored as version 2 and recorded with who made it."""
    fake = FakeConfiguration()
    with client(replace(backends(), configuration=fake)) as http:
        path = "Pictures/{captured_at:%Y}/{original_name}"
        response = http.put(
            "/configuration/templates",
            json=_templates(path=path),
            headers={OPERATOR_HEADER: "tester"},
        )
        current = http.get("/configuration/templates").json()
        history = http.get("/configuration/templates/history").json()
    assert response.status_code == 200
    assert response.json()["version"] == 2
    assert current["document"]["templates"][0]["path"] == path
    assert [(v["version"], v["active"], v["created_by"]) for v in history] == [
        (2, True, "tester"),
        (1, False, "seed"),
    ]


def test_a_change_without_an_operator_is_recorded_as_the_control_plane() -> None:
    """Calls from inside the cluster carry no operator header."""
    fake = FakeConfiguration()
    with client(replace(backends(), configuration=fake)) as http:
        http.put("/configuration/policy", json={"destinations": [], "rules": []})
    assert fake.versions[ConfigurationKind.POLICY][-1][1] == "control-plane"


def test_a_template_requiring_an_unknown_fact_is_refused_and_nothing_changes() -> None:
    """The error locates the template and names the fact; version 1 stays in force."""
    fake = FakeConfiguration()
    with client(replace(backends(), configuration=fake)) as http:
        response = http.put("/configuration/templates", json=_templates(required=["mood"]))
        current = http.get("/configuration/templates").json()
    assert response.status_code == 422
    [error] = response.json()["detail"]
    assert error["loc"] == ["body", "templates", 0]
    assert "unknown fact 'mood'" in error["msg"]
    assert current["version"] == 1


def test_an_unknown_field_is_refused() -> None:
    """Strict in shape: a misspelt key is not ignored."""
    with client() as http:
        response = http.put("/configuration/policy", json={"destinatons": []})
    assert response.status_code == 422
    assert any(e["loc"] == ["body", "destinatons"] for e in response.json()["detail"])


def test_a_refusal_from_the_store_is_answered_with_its_reason() -> None:
    """The store's own check is reported as a problem."""
    fake = FakeConfiguration(rejection="invalid taxonomy configuration: categories: empty")
    with client(replace(backends(), configuration=fake)) as http:
        response = http.put("/configuration/taxonomy", json={"categories": {}})
    assert response.status_code == 422
    assert response.json() == {"detail": "invalid taxonomy configuration: categories: empty"}


def test_history_of_an_unknown_kind_is_refused() -> None:
    """Only the three kinds exist."""
    with client() as http:
        assert http.get("/configuration/layouts/history").status_code == 422


_DESTINATION = {"name": "archive", "provider": "pcloud", "account": "1", "root_path": "/"}
_RECEIPTS_POLICY = parse_policy(
    {
        "destinations": [_DESTINATION],
        "rules": [{"name": "r", "when": {"category": "Receipts"}, "destinations": ["archive"]}],
    }
)
_RECEIPTS_TAXONOMY = SEED_TAXONOMY.promote(TaxonomyField.CATEGORY, "Receipts")


def test_a_submission_checked_without_a_taxonomy_is_parsed_from_its_json_form() -> None:
    """No taxonomy constrains its categories; the result is the document, reparsed."""
    checked = check_submission(ConfigurationKind.POLICY, _RECEIPTS_POLICY)

    assert checked == _RECEIPTS_POLICY


def test_a_policy_naming_a_category_the_taxonomy_lists_is_accepted() -> None:
    """A taxonomy listing the category lets the policy through."""
    checked = check_submission(
        ConfigurationKind.POLICY, _RECEIPTS_POLICY, taxonomy=_RECEIPTS_TAXONOMY
    )

    assert checked == _RECEIPTS_POLICY


def test_a_policy_naming_a_category_the_taxonomy_does_not_list_is_rejected() -> None:
    """The refusal names the kind, the rule and the value."""
    with pytest.raises(RejectedError) as rejected:
        check_submission(ConfigurationKind.POLICY, _RECEIPTS_POLICY, taxonomy=SEED_TAXONOMY)

    message = str(rejected.value)
    assert message.startswith("invalid policy configuration: rules.0: ")
    assert "placement rule 'r' selects category 'Receipts'" in message


def test_a_template_naming_a_subcategory_under_the_wrong_category_is_rejected() -> None:
    """Templates are checked against the taxonomy like policies."""
    templates = parse_templates(_templates(when={"category": "Photos", "subcategory": "Finance"}))

    with pytest.raises(RejectedError, match=r"invalid templates configuration: templates\.0"):
        check_submission(ConfigurationKind.TEMPLATES, templates, taxonomy=SEED_TAXONOMY)


def test_a_taxonomy_submission_ignores_the_taxonomy_in_force() -> None:
    """A taxonomy is checked on its own."""
    checked = check_submission(
        ConfigurationKind.TAXONOMY, _RECEIPTS_TAXONOMY, taxonomy=SEED_TAXONOMY
    )

    assert checked == _RECEIPTS_TAXONOMY
