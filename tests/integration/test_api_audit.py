"""Audit inspection over the ledger: a content object's history in recorded order."""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine

from curate_d.api.app import create_app
from curate_d.config import Settings
from curate_d.domain import (
    AuditEntry,
    AuditEntryType,
    ContentObject,
    DestinationIdentity,
    HashAlgorithm,
    Placement,
    PlacementState,
    Provider,
    StrongFingerprint,
    VerificationLevel,
)
from curate_d.ledger import (
    AuditRepository,
    ContentObjectRepository,
    DestinationRepository,
    PlacementRepository,
)
from curate_d.ledger.tables import content_objects, destinations, placements

pytestmark = pytest.mark.integration

_AT = datetime(2026, 9, 30, 8, tzinfo=UTC)


@pytest.fixture
def http(settings: Settings) -> Iterator[TestClient]:
    """The app over the ledger."""
    with TestClient(create_app(settings)) as client:
        yield client


async def test_history_is_ordered_and_shows_placements_with_their_verification(
    http: TestClient, ledger_engine: AsyncEngine
) -> None:
    """Entries recorded out of order come back by when they happened; a name moved to another
    account shows a placement at each, by identity and location."""
    tag = uuid.uuid4().hex[:8]
    name = f"lib-{tag}"
    old, moved = (
        DestinationIdentity(provider=Provider.PCLOUD, account=f"{side}-{tag}", root_path="/lib")
        for side in ("old", "new")
    )
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
        repository = DestinationRepository(connection)
        for identity in (old, moved):
            await repository.ensure(identity)
        placed, again = [
            await PlacementRepository(connection).upsert(
                Placement.build(
                    content_object_id=content.id,
                    destination_id=identity.id,
                    destination_name=name,
                    locator="/lib/x.jpg",
                    template_version="seed-1",
                ).model_copy(
                    update={
                        "state": PlacementState.PRESENT,
                        "verification_level": VerificationLevel.STRONG_COMMON_HASH,
                        "verified_at": _AT,
                    }
                )
            )
            for identity in (old, moved)
        ]
        audit = AuditRepository(connection)
        for entry_type, minutes in (
            (AuditEntryType.RETENTION_DECIDED, 30),
            (AuditEntryType.CLASSIFICATION_DECIDED, 0),
            (AuditEntryType.PLACEMENT_REGISTERED, 20),
            (AuditEntryType.VERIFICATION_SUCCEEDED, 10),
        ):
            await audit.append(
                AuditEntry(
                    entry_type=entry_type,
                    occurred_at=_AT + timedelta(minutes=minutes),
                    decided_by="test",
                    details={"level": "strong_common_hash"},
                    content_object_id=content.id,
                    placement_id=placed.id if minutes in (10, 20) else None,
                )
            )
    try:
        response = http.get(f"/content/{content.id}/history")
    finally:
        async with ledger_engine.begin() as connection:
            await connection.execute(
                delete(placements).where(placements.c.content_object_id == content.id)
            )
            await connection.execute(
                delete(destinations).where(destinations.c.id.in_([old.id, moved.id]))
            )
            await connection.execute(
                delete(content_objects).where(content_objects.c.id == content.id)
            )

    assert response.status_code == 200
    history = response.json()
    assert [e["entry_type"] for e in history["events"]] == [
        "classification_decided",
        "verification_succeeded",
        "placement_registered",
        "retention_decided",
    ]
    assert history["events"][2]["placement_id"] == str(placed.id)
    assert {
        (p["id"], p["destination"], p["destination_id"], p["location"], p["state"])
        for p in history["placements"]
    } == {
        (str(placed.id), name, str(old.id), f"pcloud:old-{tag}:/lib", "present"),
        (str(again.id), name, str(moved.id), f"pcloud:new-{tag}:/lib", "present"),
    }
    assert {p["verification_level"] for p in history["placements"]} == {"strong_common_hash"}


async def test_an_unknown_content_object_is_reported_as_such(http: TestClient) -> None:
    """404 rather than an empty history."""
    missing = uuid.uuid4()
    response = http.get(f"/content/{missing}/history")
    assert response.status_code == 404
    assert response.json()["detail"] == f"no content object {missing}"
