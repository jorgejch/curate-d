"""Configuration endpoints over a fake store: view, change, refuse, and list versions."""

from dataclasses import replace

from curate_d.api.configuration import OPERATOR_HEADER
from curate_d.configuration.consistency import OrphanedCondition
from curate_d.domain import SEED_TAXONOMY, ConfigurationKind, TaxonomyField
from curate_d.rendering.templates import SEED_TEMPLATES
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


def test_a_policy_selecting_an_unknown_media_kind_is_refused_and_nothing_changes() -> None:
    """The error locates the rule and names the value; version 1 stays in force."""
    fake = FakeConfiguration()
    with client(replace(backends(), configuration=fake)) as http:
        response = http.put(
            "/configuration/policy",
            json={
                "destinations": [_DESTINATION],
                "rules": [
                    {"name": "x", "when": {"media_kind": "photos"}, "destinations": ["archive"]}
                ],
            },
        )
        current = http.get("/configuration/policy").json()
    assert response.status_code == 422
    [error] = response.json()["detail"]
    assert error["loc"] == ["body", "rules", 0]
    assert "media_kind 'photos'" in error["msg"]
    assert current["version"] == 1


def test_a_policy_naming_an_unknown_profile_is_refused_and_nothing_changes() -> None:
    """The error locates the destination's profile; version 1 stays in force."""
    fake = FakeConfiguration()
    with client(replace(backends(), configuration=fake)) as http:
        response = http.put(
            "/configuration/policy",
            json={"destinations": [_DESTINATION | {"profile": "dropbox"}], "rules": []},
        )
        current = http.get("/configuration/policy").json()
    assert response.status_code == 422
    [error] = response.json()["detail"]
    assert error["loc"] == ["body", "destinations", 0, "profile"]
    assert current["version"] == 1


def test_a_template_selecting_an_unknown_media_kind_is_refused() -> None:
    """The error locates the template; version 1 stays in force."""
    fake = FakeConfiguration()
    with client(replace(backends(), configuration=fake)) as http:
        response = http.put("/configuration/templates", json=_templates(when={"media_kind": "x"}))
        current = http.get("/configuration/templates").json()
    assert response.status_code == 422
    [error] = response.json()["detail"]
    assert error["loc"] == ["body", "templates", 0]
    assert "media_kind 'x'" in error["msg"]
    assert current["version"] == 1


def test_a_taxonomy_change_answers_with_the_conditions_it_orphans() -> None:
    """The backend's orphans are reported; reading the taxonomy reports none."""
    orphan = OrphanedCondition(
        kind=ConfigurationKind.POLICY,
        owner="placement rule 'docs'",
        field=TaxonomyField.CATEGORY,
        value="Documents",
    )
    fake = FakeConfiguration(orphaned=(orphan,))
    with client(replace(backends(), configuration=fake)) as http:
        changed = http.put(
            "/configuration/taxonomy", json=SEED_TAXONOMY.model_dump(mode="json")
        ).json()
        current = http.get("/configuration/taxonomy").json()
    assert changed["version"] == 2
    assert changed["orphaned"] == [
        {
            "kind": "policy",
            "owner": "placement rule 'docs'",
            "field": "category",
            "value": "Documents",
        }
    ]
    assert current["orphaned"] == []
