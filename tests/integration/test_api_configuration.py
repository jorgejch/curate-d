"""Configuration endpoints over the ledger: changes persist, refusals change nothing."""

import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine

from curate_d.api.app import create_app
from curate_d.config import Settings
from curate_d.configuration.documents import parse_policy, parse_taxonomy
from curate_d.configuration.store import ConfigurationRepository, DocumentValidator
from curate_d.domain import (
    ConfigurationKind,
    ContentObject,
    HashAlgorithm,
    Placement,
    StrongFingerprint,
)
from curate_d.ledger import ContentObjectRepository, PlacementRepository
from curate_d.ledger.tables import content_objects, placements
from curate_d.rendering.templates import parse_templates

pytestmark = pytest.mark.integration

_PARSERS: dict[ConfigurationKind, DocumentValidator] = {
    ConfigurationKind.TAXONOMY: parse_taxonomy,
    ConfigurationKind.TEMPLATES: parse_templates,
    ConfigurationKind.POLICY: parse_policy,
}


@pytest.fixture
async def restore(ledger_engine: AsyncEngine) -> AsyncIterator[None]:
    """Put back each kind's document in force before the test."""
    before = {}
    async with ledger_engine.begin() as connection:
        repository = ConfigurationRepository(connection)
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
