"""Audit inspection: ordered history, placements with their verification, unknown ids."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from curate_d.api import audit as module
from curate_d.api.audit import LedgerAudit, history_of
from curate_d.api.errors import NotFoundError
from curate_d.domain import (
    AuditEntry,
    AuditEntryType,
    ContentObject,
    ContentObjectId,
    DestinationIdentity,
    HashAlgorithm,
    Placement,
    PlacementState,
    Provider,
    StrongFingerprint,
    VerificationLevel,
)
from tests.unit.api.fakes import FakeAudit, backends, client

_AT = datetime(2026, 9, 30, 8, tzinfo=UTC)


def _content() -> ContentObject:
    """A photo's content object."""
    return ContentObject.build(
        fingerprint=StrongFingerprint(algorithm=HashAlgorithm.SHA256, digest="ab" * 32),
        size_bytes=2048,
        media_type="image/jpeg",
    )


def _entry(entry_type: AuditEntryType, minutes: int, content: ContentObject) -> AuditEntry:
    """An entry `minutes` after the first."""
    return AuditEntry(
        entry_type=entry_type,
        occurred_at=_AT + timedelta(minutes=minutes),
        decided_by="test",
        details={"step": minutes},
        content_object_id=content.id,
    )


def _at(account: str, root: str) -> DestinationIdentity:
    """Return the identity of a pCloud destination."""
    return DestinationIdentity(provider=Provider.PCLOUD, account=account, root_path=root)


_LIBRARY = _at("acct-1", "/library")
_OFFSITE = _at("acct-1", "/offsite")
_KNOWN = {identity.id: identity for identity in (_LIBRARY, _OFFSITE)}


def _placement(
    content: ContentObject, destination: str, identity: DestinationIdentity | None = None
) -> Placement:
    """Return a strongly verified copy at `destination`, rooted at its own name by default."""
    return Placement.build(
        content_object_id=content.id,
        destination_id=(identity or _at("acct-1", f"/{destination}")).id,
        destination_name=destination,
        locator=f"/{destination}/x.jpg",
        template_version="seed-1",
    ).model_copy(
        update={
            "state": PlacementState.PRESENT,
            "verification_level": VerificationLevel.STRONG_COMMON_HASH,
            "verified_at": _AT,
        }
    )


def test_history_keeps_the_recorded_order_and_names_verification_levels() -> None:
    """Events as recorded; placements by destination with their rung by name."""
    content = _content()
    entries = [
        _entry(AuditEntryType.CLASSIFICATION_DECIDED, 0, content),
        _entry(AuditEntryType.PLACEMENT_REGISTERED, 5, content),
        _entry(AuditEntryType.RETENTION_DECIDED, 6, content),
    ]
    history = history_of(
        content,
        [_placement(content, "offsite"), _placement(content, "library")],
        entries,
        _KNOWN,
    )
    assert [e.entry_type for e in history.events] == [
        AuditEntryType.CLASSIFICATION_DECIDED,
        AuditEntryType.PLACEMENT_REGISTERED,
        AuditEntryType.RETENTION_DECIDED,
    ]
    assert [p.destination for p in history.placements] == ["library", "offsite"]
    assert history.placements[0].verification_level == "strong_common_hash"
    assert history.fingerprint == f"sha256:{'ab' * 32}"


def test_one_name_at_two_accounts_shows_two_placements_by_location() -> None:
    """A name moved accounts: each copy shows its own identity and location."""
    content = _content()
    moved = _at("acct-2", "/library")
    history = history_of(
        content,
        [_placement(content, "library", moved), _placement(content, "library", _LIBRARY)],
        [],
        {**_KNOWN, moved.id: moved},
    )
    shown = {(p.destination, p.destination_id, p.location) for p in history.placements}
    assert shown == {
        ("library", str(_LIBRARY.id), "pcloud:acct-1:/library"),
        ("library", str(moved.id), "pcloud:acct-2:/library"),
    }
    assert [p.destination_id for p in history.placements] == sorted(
        [str(_LIBRARY.id), str(moved.id)]
    )


def test_the_endpoint_serves_a_history() -> None:
    """Served as recorded."""
    content = _content()
    history = history_of(
        content, [], [_entry(AuditEntryType.CLASSIFICATION_DECIDED, 0, content)], {}
    )
    fake = FakeAudit(histories={content.id: history})
    with client(replace(backends(), audit=fake)) as http:
        response = http.get(f"/content/{content.id}/history")
    assert response.status_code == 200
    assert response.json()["events"][0]["entry_type"] == "classification_decided"


def test_an_unknown_content_object_is_reported_as_such() -> None:
    """404 naming the identifier."""
    missing = uuid4()
    with client() as http:
        response = http.get(f"/content/{missing}/history")
    assert response.status_code == 404
    assert response.json() == {"detail": f"no content object {missing}"}


class _Engine:
    """Hands out a connection that the fakes ignore."""

    def connect(self) -> "_Engine":
        """Itself, as a context manager."""
        return self

    async def __aenter__(self) -> object:
        """A connection."""
        return object()

    async def __aexit__(self, *_exc: object) -> None:
        """Nothing to release."""


async def test_the_ledger_backend_refuses_an_unknown_content_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Checked before anything else is read."""

    class Contents:
        def __init__(self, _connection: object) -> None:
            """Nothing to bind."""

        async def get(self, _content_object_id: ContentObjectId) -> ContentObject | None:
            """Nothing exists."""
            return None

    monkeypatch.setattr(module, "ContentObjectRepository", Contents)
    missing = ContentObjectId(uuid4())
    with pytest.raises(NotFoundError, match=str(missing)):
        await LedgerAudit(cast(AsyncEngine, _Engine())).history(missing)
