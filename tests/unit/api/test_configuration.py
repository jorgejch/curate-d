"""Configuration endpoints over a fake store: view, change, refuse, and list versions."""

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine
from structlog.testing import capture_logs

from curate_d.api import configuration as module
from curate_d.api.configuration import (
    OPERATOR_HEADER,
    LedgerConfiguration,
    check_submission,
    report_orphans,
)
from curate_d.api.errors import RejectedError
from curate_d.configuration.consistency import OrphanedCondition
from curate_d.configuration.documents import parse_policy
from curate_d.configuration.store import ConfigurationRepository, DocumentValidator, check_document
from curate_d.domain import SEED_TAXONOMY, ConfigurationKind, Taxonomy, TaxonomyField
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


def test_a_policy_selecting_an_unknown_media_kind_is_refused_and_nothing_changes() -> None:
    """The error locates the rule and names the value; version 1 stays in force."""
    fake = FakeConfiguration()
    rule = {"name": "x", "when": {"media_kind": "photos"}, "destinations": ["archive"]}
    with client(replace(backends(), configuration=fake)) as http:
        response = http.put(
            "/configuration/policy", json={"destinations": [_DESTINATION], "rules": [rule]}
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


_ORPHAN = OrphanedCondition(
    kind=ConfigurationKind.POLICY,
    owner="placement rule 'docs'",
    field=TaxonomyField.CATEGORY,
    value="Documents",
)


def test_a_taxonomy_change_answers_with_the_conditions_it_orphans() -> None:
    """The backend's orphans are reported; reading the taxonomy reports none."""
    fake = FakeConfiguration(orphaned=(_ORPHAN,))
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


def test_a_taxonomy_change_orphaning_nothing_answers_an_empty_list() -> None:
    """`orphaned` is always present on a taxonomy change."""
    with client() as http:
        changed = http.put("/configuration/taxonomy", json=SEED_TAXONOMY.model_dump(mode="json"))
    assert changed.status_code == 200
    assert changed.json()["orphaned"] == []


@dataclass
class _Stored:
    """A configuration store holding the given active documents, by kind."""

    stored: dict[ConfigurationKind, Mapping[str, object]] = field(default_factory=dict)

    async def active(self, kind: ConfigurationKind) -> tuple[int, Mapping[str, object]] | None:
        """Return the stored document as version 1, or None if none was stored."""
        document = self.stored.get(kind)
        return None if document is None else (1, document)


async def test_orphans_are_reported_from_the_versions_in_force() -> None:
    """A taxonomy dropping a category in use reports the conditions naming it."""
    shrunken = Taxonomy(categories={"Photos": ("Camera",)})
    stored = _Stored({ConfigurationKind.POLICY: _RECEIPTS_POLICY.model_dump(mode="json")})

    found = await report_orphans(cast(ConfigurationRepository, stored), shrunken)

    assert [(o.kind, o.owner, o.value) for o in found] == [
        (ConfigurationKind.POLICY, "placement rule 'r'", "Receipts"),
        (ConfigurationKind.TEMPLATES, "template 'documents'", "Documents"),
    ]


async def test_a_taxonomy_change_is_refused_while_the_policy_in_force_does_not_read() -> None:
    """The refusal names the unreadable kind and its problem, so the operator can replace it."""
    unreadable = {"destinations": [_DESTINATION | {"profile": "dropbox"}], "rules": []}
    stored = cast(ConfigurationRepository, _Stored({ConfigurationKind.POLICY: unreadable}))

    with pytest.raises(RejectedError) as rejected:
        await report_orphans(stored, SEED_TAXONOMY)

    message = str(rejected.value)
    assert message.startswith("the policy configuration in force does not read")
    assert "invalid policy configuration: destinations.0.profile" in message


class _Engine:
    """Hands out a connection the fake repository ignores."""

    def begin(self) -> "_Engine":
        """Return itself, as a context manager."""
        return self

    async def __aenter__(self) -> object:
        """Return a connection."""
        return object()

    async def __aexit__(self, *_exc: object) -> None:
        """Release nothing."""


@dataclass
class _Ledger:
    """What a fake repository saw: the shared locks taken and the documents activated."""

    locked: list[ConfigurationKind] = field(default_factory=list)
    activated: list[ConfigurationKind] = field(default_factory=list)


@pytest.fixture
def ledger(monkeypatch: pytest.MonkeyPatch) -> _Ledger:
    """Back `LedgerConfiguration` with a repository that checks and records activations."""
    seen = _Ledger()

    class Repository:
        def __init__(self, _connection: object) -> None:
            """Bind nothing."""

        async def lock_shared(self, kind: ConfigurationKind) -> None:
            """Record the shared lock."""
            seen.locked.append(kind)

        async def activate(
            self,
            kind: ConfigurationKind,
            document: Mapping[str, object],
            validator: DocumentValidator,
            *,
            created_by: str,
        ) -> int:
            """Check the document as the store does, and store it as version 4."""
            check_document(kind, document, validator)
            seen.activated.append(kind)
            return 4

    async def in_force(_configurations: object) -> Taxonomy:
        """Return the seed taxonomy."""
        return SEED_TAXONOMY

    monkeypatch.setattr(module, "ConfigurationRepository", Repository)
    monkeypatch.setattr(module, "taxonomy_in_force", in_force)
    return seen


async def test_a_policy_is_checked_against_the_taxonomy_under_its_shared_lock(
    ledger: _Ledger,
) -> None:
    """A category the taxonomy in force does not list refuses the policy, and nothing is stored."""
    backend = LedgerConfiguration(cast(AsyncEngine, _Engine()))

    with pytest.raises(RejectedError, match="category 'Receipts'"):
        await backend.activate(ConfigurationKind.POLICY, _RECEIPTS_POLICY, by="tester")

    assert ledger.locked == [ConfigurationKind.TAXONOMY]
    assert ledger.activated == []


async def test_a_template_set_the_taxonomy_lists_is_activated(ledger: _Ledger) -> None:
    """A valid set is stored after the shared lock, and orphans nothing."""
    backend = LedgerConfiguration(cast(AsyncEngine, _Engine()))

    activated = await backend.activate(ConfigurationKind.TEMPLATES, SEED_TEMPLATES, by="tester")

    assert (activated.version, activated.orphaned) == (4, ())
    assert (ledger.locked, ledger.activated) == (
        [ConfigurationKind.TAXONOMY],
        [ConfigurationKind.TEMPLATES],
    )


async def test_a_taxonomy_orphaning_conditions_logs_them_and_reclassifies(
    ledger: _Ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The orphans are answered and logged; the new version is sent to reclassification."""
    orphan = OrphanedCondition(
        kind=ConfigurationKind.POLICY,
        owner="placement rule 'r'",
        field=TaxonomyField.CATEGORY,
        value="Receipts",
    )

    async def orphaning(_configurations: object, _taxonomy: Taxonomy) -> list[OrphanedCondition]:
        """Report one orphaned condition."""
        return [orphan]

    reclassified: list[int] = []

    async def reclassify(version: int) -> None:
        """Record the version sent to reclassification."""
        reclassified.append(version)

    monkeypatch.setattr(module, "report_orphans", orphaning)
    backend = LedgerConfiguration(cast(AsyncEngine, _Engine()), reclassify=reclassify)

    with capture_logs() as events:
        activated = await backend.activate(ConfigurationKind.TAXONOMY, SEED_TAXONOMY, by="tester")

    assert activated.orphaned == (orphan,)
    assert ledger.locked == []
    assert reclassified == [4]
    [logged] = [e for e in events if e["event"] == "configuration.orphaned_conditions"]
    assert (logged["taxonomy_version"], logged["count"]) == (4, 1)
    assert logged["conditions"] == ["policy: placement rule 'r' category='Receipts'"]


async def test_a_taxonomy_orphaning_nothing_logs_nothing(
    ledger: _Ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No orphans, no warning."""

    async def none(_configurations: object, _taxonomy: Taxonomy) -> list[OrphanedCondition]:
        """Report nothing."""
        return []

    monkeypatch.setattr(module, "report_orphans", none)
    backend = LedgerConfiguration(cast(AsyncEngine, _Engine()))

    with capture_logs() as events:
        activated = await backend.activate(ConfigurationKind.TAXONOMY, SEED_TAXONOMY, by="tester")

    assert activated.orphaned == ()
    assert not [e for e in events if e["event"] == "configuration.orphaned_conditions"]
