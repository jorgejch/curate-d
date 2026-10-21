"""Configuration endpoints over the ledger: changes persist, refusals change nothing.

A policy or template change is checked against the taxonomy in force under the taxonomy's
shared lock; a taxonomy change answers and logs the conditions it orphans, and is refused
while the policy in force does not read.
"""

import asyncio
import copy
import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine
from structlog.testing import capture_logs

from curate_d.api.app import create_app
from curate_d.api.configuration import LedgerConfiguration
from curate_d.classification.taxonomy import active_taxonomy, promote
from curate_d.config import Settings
from curate_d.configuration.consistency import taxonomy_in_force
from curate_d.configuration.documents import parse_policy, parse_stored_policy, parse_taxonomy
from curate_d.configuration.store import ConfigurationRepository, DocumentValidator
from curate_d.domain import (
    ConfigurationKind,
    ContentObject,
    HashAlgorithm,
    Placement,
    RejectedValue,
    StrongFingerprint,
    TaxonomyField,
)
from curate_d.ledger import ContentObjectRepository, PlacementRepository
from curate_d.ledger.tables import configurations, content_objects, placements
from curate_d.placement.policy import active_policy
from curate_d.rendering.planning import active_templates
from curate_d.rendering.templates import parse_templates

pytestmark = pytest.mark.integration

# Restored in this order. Without a taxonomy context any order is valid; the taxonomy
# goes last anyway, after the documents whose conditions it lists.
_PARSERS: dict[ConfigurationKind, DocumentValidator] = {
    ConfigurationKind.POLICY: parse_stored_policy,
    ConfigurationKind.TEMPLATES: parse_templates,
    ConfigurationKind.TAXONOMY: parse_taxonomy,
}
_DESTINATION = {"name": "archive", "provider": "pcloud", "account": "1", "root_path": "/"}


@pytest.fixture
async def restore(ledger_engine: AsyncEngine) -> AsyncIterator[None]:
    """Put back each kind's document in force before the test."""
    before = {}
    async with ledger_engine.begin() as connection:
        repository = ConfigurationRepository(connection)
        # On a fresh database nothing is in force until the API's first read seeds the
        # default, so there would be nothing to put back and a test's change would outlive
        # it. Seeding through the same helpers first leaves every kind with a document.
        await active_taxonomy(repository)
        await active_templates(repository)
        await active_policy(repository)
        for kind in _PARSERS:
            before[kind] = await repository.active(kind)
    yield
    async with ledger_engine.begin() as connection:
        repository = ConfigurationRepository(connection)
        for kind, active in before.items():
            now = await repository.active(kind)
            if active is not None and now is not None and now[1] != active[1]:
                await repository.activate(
                    kind, active[1], _PARSERS[kind], created_by="test-restore"
                )


@pytest.fixture
def http(settings: Settings, restore: None) -> Iterator[TestClient]:
    """The app over the ledger."""
    with TestClient(create_app(settings)) as client:
        yield client


async def test_a_valid_template_change_persists_and_is_in_force(
    http: TestClient, ledger_engine: AsyncEngine
) -> None:
    """The new version is stored, active, and attributed."""
    current = http.get("/configuration/templates").json()
    document = current["document"]
    document["version"] = f"test-{uuid.uuid4().hex[:8]}"
    changed = http.put(
        "/configuration/templates", json=document, headers={"Tailscale-User-Login": "tester"}
    )

    assert changed.status_code == 200
    assert changed.json()["version"] == current["version"] + 1
    async with ledger_engine.connect() as connection:
        active = await ConfigurationRepository(connection).active(ConfigurationKind.TEMPLATES)
    assert active is not None
    assert active[0] == current["version"] + 1
    assert active[1]["version"] == document["version"]
    [newest, *_] = http.get("/configuration/templates/history").json()
    assert (newest["active"], newest["created_by"]) == (True, "tester")


async def test_an_invalid_policy_is_refused_and_the_previous_one_stays_in_force(
    http: TestClient, ledger_engine: AsyncEngine
) -> None:
    """A rule naming an undeclared destination is refused; no version is added."""
    before = http.get("/configuration/policy").json()
    refused = http.put(
        "/configuration/policy",
        json={
            "destinations": [],
            "rules": [{"name": "photos", "when": {"category": "Photos"}, "destinations": ["x"]}],
        },
    )

    assert refused.status_code == 422
    assert "x" in refused.text
    assert http.get("/configuration/policy").json() == before
    async with ledger_engine.connect() as connection:
        history = await ConfigurationRepository(connection).history(ConfigurationKind.POLICY)
    assert history[0].version == before["version"]


async def test_a_template_change_leaves_existing_placements_untouched(
    http: TestClient, ledger_engine: AsyncEngine
) -> None:
    """Placements move only when reconciliation acts."""
    async with ledger_engine.begin() as connection:
        content = await ContentObjectRepository(connection).upsert(
            ContentObject.build(
                fingerprint=StrongFingerprint(
                    algorithm=HashAlgorithm.SHA256, digest=uuid.uuid4().hex * 2
                ),
                size_bytes=10,
                media_type="image/jpeg",
            )
        )
        placed = await PlacementRepository(connection).upsert(
            Placement.build(
                content_object_id=content.id,
                destination_name=f"lib-{uuid.uuid4().hex[:8]}",
                locator="/lib/Photos/2024/x.jpg",
                template_version="seed-1",
            )
        )
    try:
        document = http.get("/configuration/templates").json()["document"]
        document["version"] = f"test-{uuid.uuid4().hex[:8]}"
        assert http.put("/configuration/templates", json=document).status_code == 200
        async with ledger_engine.connect() as connection:
            row = (
                (await connection.execute(select(placements).where(placements.c.id == placed.id)))
                .mappings()
                .one()
            )
        assert (row["locator"], row["template_version"]) == (placed.locator, "seed-1")
    finally:
        async with ledger_engine.begin() as connection:
            await connection.execute(delete(placements).where(placements.c.id == placed.id))
            await connection.execute(
                delete(content_objects).where(content_objects.c.id == content.id)
            )


async def _policy_versions(ledger_engine: AsyncEngine) -> list[int]:
    """Return every stored policy version, newest first."""
    async with ledger_engine.connect() as connection:
        history = await ConfigurationRepository(connection).history(ConfigurationKind.POLICY)
    return [version.version for version in history]


@pytest.mark.parametrize(
    ("document", "location"),
    [
        (
            {
                "destinations": [_DESTINATION],
                "rules": [
                    {"name": "x", "when": {"media_kind": "photos"}, "destinations": ["archive"]}
                ],
            },
            ["body", "rules", 0],
        ),
        (
            {"destinations": [_DESTINATION | {"profile": "dropbox"}], "rules": []},
            ["body", "destinations", 0, "profile"],
        ),
    ],
)
async def test_a_policy_with_an_unknown_media_kind_or_profile_changes_nothing(
    http: TestClient,
    ledger_engine: AsyncEngine,
    document: dict[str, object],
    location: list[object],
) -> None:
    """Refused with the location; the version in force and the history are unchanged."""
    before = http.get("/configuration/policy").json()
    versions = await _policy_versions(ledger_engine)

    refused = http.put("/configuration/policy", json=document)

    assert refused.status_code == 422
    assert [error["loc"] for error in refused.json()["detail"]] == [location]
    assert http.get("/configuration/policy").json() == before
    assert await _policy_versions(ledger_engine) == versions


async def test_a_template_with_an_unknown_media_kind_changes_nothing(http: TestClient) -> None:
    """Refused with the template's location; the set in force is unchanged."""
    before = http.get("/configuration/templates").json()
    document = copy.deepcopy(before["document"])
    document["templates"][0]["when"] = {"media_kind": "photos"}

    refused = http.put("/configuration/templates", json=document)

    assert refused.status_code == 422
    assert ["body", "templates", 0] in [error["loc"] for error in refused.json()["detail"]]
    assert http.get("/configuration/templates").json() == before


async def test_a_policy_category_the_taxonomy_lacks_is_refused_until_it_is_added(
    http: TestClient, ledger_engine: AsyncEngine
) -> None:
    """Checked against the taxonomy in force at activation; accepted once it lists the value."""
    category = f"Unlisted-{uuid.uuid4().hex[:8]}"
    document = {
        "destinations": [_DESTINATION],
        "rules": [{"name": "odd", "when": {"category": category}, "destinations": ["archive"]}],
    }
    before = http.get("/configuration/policy").json()
    versions = await _policy_versions(ledger_engine)

    refused = http.put("/configuration/policy", json=document)

    assert refused.status_code == 422
    detail = refused.json()["detail"]
    assert detail.startswith("invalid policy configuration: rules.0: ")
    assert f"placement rule 'odd' selects category {category!r}" in detail
    assert http.get("/configuration/policy").json() == before
    assert await _policy_versions(ledger_engine) == versions

    taxonomy = http.get("/configuration/taxonomy").json()["document"]
    taxonomy["categories"][category] = []
    assert http.put("/configuration/taxonomy", json=taxonomy).status_code == 200

    accepted = http.put("/configuration/policy", json=document)
    assert accepted.status_code == 200
    assert accepted.json()["version"] == before["version"] + 1


async def test_a_template_subcategory_outside_its_category_is_refused(http: TestClient) -> None:
    """The template's subcategory must be listed under the category it names."""
    before = http.get("/configuration/templates").json()
    document = copy.deepcopy(before["document"])
    document["templates"].insert(
        0,
        {
            "name": f"odd-{uuid.uuid4().hex[:8]}",
            "path": "{original_name}",
            "when": {"category": "Photos", "subcategory": f"Unlisted-{uuid.uuid4().hex[:8]}"},
        },
    )

    refused = http.put("/configuration/templates", json=document)

    assert refused.status_code == 422
    assert "invalid templates configuration: templates.0: " in refused.json()["detail"]
    assert http.get("/configuration/templates").json() == before


async def test_a_taxonomy_that_drops_a_value_in_use_is_activated_and_reports_it(
    http: TestClient,
) -> None:
    """Never refused for orphans: answered with each orphaned condition; the policy still reads."""
    category = f"Doomed-{uuid.uuid4().hex[:8]}"
    rule, keep = f"rule-{category}", f"keep-{category}"
    template = f"template-{category}".lower()
    taxonomy = http.get("/configuration/taxonomy").json()
    widened = taxonomy["document"]
    widened["categories"][category] = []
    assert http.put("/configuration/taxonomy", json=widened).status_code == 200
    assert (
        http.put(
            "/configuration/policy",
            json={
                "destinations": [_DESTINATION],
                "rules": [
                    {"name": rule, "when": {"category": category}, "destinations": ["archive"]}
                ],
                "retention": [{"name": keep, "when": {"category": category}}],
            },
        ).status_code
        == 200
    )
    templates = http.get("/configuration/templates").json()["document"]
    templates["templates"].insert(
        0, {"name": template, "path": "{original_name}", "when": {"category": category}}
    )
    assert http.put("/configuration/templates", json=templates).status_code == 200

    del widened["categories"][category]
    with capture_logs() as events:
        narrowed = http.put("/configuration/taxonomy", json=widened)

    assert narrowed.status_code == 200
    body = narrowed.json()
    assert body["version"] == taxonomy["version"] + 2
    assert [o for o in body["orphaned"] if o["value"] == category] == [
        {
            "kind": "policy",
            "owner": f"placement rule {rule!r}",
            "field": "category",
            "value": category,
        },
        {
            "kind": "policy",
            "owner": f"retention rule {keep!r}",
            "field": "category",
            "value": category,
        },
        {
            "kind": "templates",
            "owner": f"template {template!r}",
            "field": "category",
            "value": category,
        },
    ]
    [logged] = [e for e in events if e["event"] == "configuration.orphaned_conditions"]
    assert logged["log_level"] == "warning"
    assert logged["taxonomy_version"] == body["version"]
    assert logged["count"] == len(body["orphaned"])
    assert [c for c in logged["conditions"] if category in c] == [
        f"policy: placement rule {rule!r} category={category!r}",
        f"policy: retention rule {keep!r} category={category!r}",
        f"templates: template {template!r} category={category!r}",
    ]
    policy = http.get("/configuration/policy")
    assert policy.status_code == 200
    assert policy.json()["document"]["rules"][0]["when"] == {"category": category}
    assert http.get("/configuration/taxonomy").json()["orphaned"] == []


async def test_a_taxonomy_that_drops_no_value_in_use_orphans_nothing(http: TestClient) -> None:
    """A value nothing selects on is removed without a report or a log entry."""
    category = f"Unused-{uuid.uuid4().hex[:8]}"
    taxonomy = http.get("/configuration/taxonomy").json()["document"]
    taxonomy["categories"][category] = []
    assert http.put("/configuration/taxonomy", json=taxonomy).status_code == 200
    assert (
        http.put("/configuration/policy", json={"destinations": [], "rules": []}).status_code == 200
    )
    del taxonomy["categories"][category]

    with capture_logs() as events:
        narrowed = http.put("/configuration/taxonomy", json=taxonomy)

    assert narrowed.status_code == 200
    assert narrowed.json()["orphaned"] == []
    assert not [e for e in events if e["event"] == "configuration.orphaned_conditions"]


async def test_a_taxonomy_change_is_refused_while_the_policy_in_force_does_not_read(
    http: TestClient, ledger_engine: AsyncEngine
) -> None:
    """Its orphans cannot be reported, so it is 422 naming the policy; a valid policy recovers."""
    taxonomy = http.get("/configuration/taxonomy").json()
    http.get("/configuration/policy")
    unreadable = {"destinations": [_DESTINATION | {"profile": "dropbox"}], "rules": []}
    policies = configurations.c.kind == ConfigurationKind.POLICY.value
    async with ledger_engine.begin() as connection:
        latest = await connection.scalar(select(func.max(configurations.c.version)).where(policies))
        await connection.execute(
            configurations.update().where(policies, configurations.c.active).values(active=False)
        )
        await connection.execute(
            configurations.insert().values(
                kind=ConfigurationKind.POLICY.value,
                version=int(latest) + 1,
                document=unreadable,
                active=True,
                created_by="test-unreadable",
            )
        )

    refused = http.put("/configuration/taxonomy", json=taxonomy["document"])

    assert refused.status_code == 422
    detail = refused.json()["detail"]
    assert detail.startswith("the policy configuration in force does not read")
    assert "destinations.0.profile" in detail
    assert http.get("/configuration/taxonomy").json()["version"] == taxonomy["version"]
    replaced = http.put("/configuration/policy", json={"destinations": [], "rules": []})
    assert replaced.status_code == 200
    assert http.put("/configuration/taxonomy", json=taxonomy["document"]).status_code == 200


async def test_a_policy_activation_waits_for_a_taxonomy_activation_in_flight(
    ledger_engine: AsyncEngine, restore: None
) -> None:
    """The policy is checked against the taxonomy that commits, never one about to change."""
    category = f"Pending-{uuid.uuid4().hex[:8]}"
    configuration = LedgerConfiguration(ledger_engine)
    policy = parse_policy(
        {
            "destinations": [_DESTINATION],
            "rules": [{"name": "p", "when": {"category": category}, "destinations": ["archive"]}],
        }
    )
    async with ledger_engine.connect() as holder:
        await holder.begin()
        repository = ConfigurationRepository(holder)
        taxonomy = await taxonomy_in_force(repository)
        widened = taxonomy.promote(TaxonomyField.CATEGORY, category)
        await repository.activate(
            ConfigurationKind.TAXONOMY,
            widened.model_dump(mode="json"),
            parse_taxonomy,
            created_by="test",
        )
        waiting = asyncio.create_task(
            configuration.activate(ConfigurationKind.POLICY, policy, by="test")
        )
        await asyncio.sleep(0.5)
        assert not waiting.done()
        await holder.commit()
    activated = await asyncio.wait_for(waiting, timeout=10)
    assert activated.orphaned == ()


async def test_a_promotion_extends_a_taxonomy_activated_while_it_waited(
    ledger_engine: AsyncEngine, restore: None
) -> None:
    """Promotion reads the taxonomy under its lock, so it never drops a value just added."""
    added, promoted = (f"{name}-{uuid.uuid4().hex[:8]}" for name in ("Added", "Promoted"))

    async def promoting() -> object:
        """Promote `promoted` in a transaction of its own."""
        async with ledger_engine.begin() as connection:
            return await promote(
                ConfigurationRepository(connection),
                [RejectedValue(field=TaxonomyField.CATEGORY, value=promoted)],
                promoted_by="test",
            )

    async with ledger_engine.begin() as connection:
        # Stored first, so promotion has a version in force to read without waiting.
        await active_taxonomy(ConfigurationRepository(connection))
    async with ledger_engine.connect() as holder:
        await holder.begin()
        repository = ConfigurationRepository(holder)
        taxonomy = await taxonomy_in_force(repository)
        await repository.activate(
            ConfigurationKind.TAXONOMY,
            taxonomy.promote(TaxonomyField.CATEGORY, added).model_dump(mode="json"),
            parse_taxonomy,
            created_by="test",
        )
        waiting = asyncio.create_task(promoting())
        await asyncio.sleep(0.5)
        assert not waiting.done()
        await holder.commit()
    await asyncio.wait_for(waiting, timeout=10)

    async with ledger_engine.connect() as connection:
        in_force = await taxonomy_in_force(ConfigurationRepository(connection))
    assert in_force.accepts(TaxonomyField.CATEGORY, added)
    assert in_force.accepts(TaxonomyField.CATEGORY, promoted)


def _retired_flag_policy() -> dict[str, object]:
    """Return a policy whose one retention rule carries `source_counts_as_copy`."""
    rule = {"name": f"keep-{uuid.uuid4().hex[:8]}", "release_source": False, "required_copies": 3}
    return {"destinations": [], "rules": [], "retention": [rule | {"source_counts_as_copy": False}]}


async def test_a_policy_carrying_the_retired_flag_is_refused(http: TestClient) -> None:
    """A new document with `source_counts_as_copy` is 422 and the version in force stays."""
    before = http.get("/configuration/policy").json()
    refused = http.put("/configuration/policy", json=_retired_flag_policy())
    assert refused.status_code == 422
    assert "source_counts_as_copy" in refused.text
    assert http.get("/configuration/policy").json() == before


async def test_a_stored_policy_with_the_retired_flag_is_served_without_it(
    http: TestClient, ledger_engine: AsyncEngine
) -> None:
    """A version stored with `source_counts_as_copy` is still in force and read without it."""
    http.get("/configuration/policy")
    stored = _retired_flag_policy()
    policies = configurations.c.kind == ConfigurationKind.POLICY.value
    async with ledger_engine.begin() as connection:
        latest = await connection.scalar(select(func.max(configurations.c.version)).where(policies))
        version = int(latest) + 1
        await connection.execute(
            configurations.update().where(policies, configurations.c.active).values(active=False)
        )
        await connection.execute(
            configurations.insert().values(
                kind=ConfigurationKind.POLICY.value,
                version=version,
                document=stored,
                active=True,
                created_by="test-retired-flag",
            )
        )
    served = http.get("/configuration/policy")
    assert served.status_code == 200
    assert served.json()["version"] == version
    [retention] = served.json()["document"]["retention"]
    assert retention["required_copies"] == 3
    assert "source_counts_as_copy" not in retention
