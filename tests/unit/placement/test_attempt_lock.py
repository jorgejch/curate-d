"""The placement row each attempt check reads is locked for the writing transaction.

The repository's statement is compiled against the PostgreSQL dialect; the pipeline's
checks run against a repository that only records how it was read.
"""

from datetime import UTC, datetime
from typing import ClassVar, cast
from uuid import uuid4

import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql import ClauseElement

from curate_d.domain import (
    ContentObjectId,
    HashAlgorithm,
    Placement,
    PlacementId,
    PlacementState,
    Provider,
    SourceObject,
    StrongFingerprint,
)
from curate_d.ledger import PlacementRepository
from curate_d.placement import pipeline
from curate_d.placement.verification import Assessment, Verdict

_NOW = datetime(2026, 7, 1, 8, 0, tzinfo=UTC)
_LOCATOR = "/Photos/2026/IMG_0001.JPG"


class _NoRows:
    """A result with no rows."""

    def mappings(self) -> "_NoRows":
        """Return this result."""
        return self

    def one_or_none(self) -> None:
        """Return no row."""
        return None


class _Recording:
    """A connection that records the statements it executes and returns no rows."""

    def __init__(self) -> None:
        """Start with nothing executed."""
        self.executed: list[ClauseElement] = []

    async def execute(self, statement: ClauseElement) -> _NoRows:
        """Record `statement` and return an empty result."""
        self.executed.append(statement)
        return _NoRows()


class _Ledger:
    """A placement repository that records whether each read locked the row."""

    reads: ClassVar[list[bool]] = []
    held: ClassVar[Placement | None] = None

    def __init__(self, connection: object) -> None:
        """Ignore the connection."""

    async def get(self, placement_id: PlacementId, *, for_update: bool = False) -> Placement | None:
        """Record the read and return the held placement."""
        self.reads.append(for_update)
        return self.held


@pytest.fixture
def ledger(monkeypatch: pytest.MonkeyPatch) -> type[_Ledger]:
    """Replace the pipeline's placement repository with one holding nothing."""
    monkeypatch.setattr(_Ledger, "reads", [])
    monkeypatch.setattr(_Ledger, "held", None)
    monkeypatch.setattr(pipeline, "PlacementRepository", _Ledger)
    return _Ledger


def _connection() -> AsyncConnection:
    """Return a connection the replaced repository never touches."""
    return cast(AsyncConnection, None)


async def _compiled(*, for_update: bool) -> str:
    """Return the SQL `PlacementRepository.get` issues, compiled for PostgreSQL."""
    recording = _Recording()
    repository = PlacementRepository(cast(AsyncConnection, recording))
    await repository.get(PlacementId(uuid4()), for_update=for_update)
    [statement] = recording.executed
    return str(statement.compile(dialect=make_url("postgresql+psycopg://").get_dialect()()))


async def test_a_locking_read_selects_the_row_for_update() -> None:
    """`for_update=True` issues SELECT ... FOR UPDATE on PostgreSQL."""
    assert (await _compiled(for_update=True)).rstrip().endswith("FOR UPDATE")


async def test_a_plain_read_takes_no_lock() -> None:
    """The default read carries no FOR UPDATE."""
    assert "FOR UPDATE" not in await _compiled(for_update=False)


async def test_a_conclusion_checks_the_locked_row(ledger: type[_Ledger]) -> None:
    """`conclude` reads the placement for update before checking its attempt."""
    assessment = Assessment(verdict=Verdict.VERIFIED)
    concluded = await pipeline.conclude(
        _connection(), PlacementId(uuid4()), assessment, attempt=1, now=_NOW
    )
    assert concluded is None
    assert ledger.reads == [True]


async def test_a_failure_checks_the_locked_row(ledger: type[_Ledger]) -> None:
    """`fail` reads the placement for update before checking its attempt."""
    failed = await pipeline.fail(_connection(), PlacementId(uuid4()), "gone", attempt=1, now=_NOW)
    assert failed is None
    assert ledger.reads == [True]


async def test_an_absence_checks_the_locked_row(ledger: type[_Ledger]) -> None:
    """`mark_absent` reads the placement for update before checking it is present."""
    absent = await pipeline.mark_absent(
        _connection(), PlacementId(uuid4()), checked="audit", now=_NOW
    )
    assert absent is None
    assert ledger.reads == [True]


async def test_a_start_checks_the_locked_row(
    ledger: type[_Ledger], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`start` reads an existing placement for update before deciding the attempt."""
    content_object_id = ContentObjectId(uuid4())
    present = Placement.build(
        content_object_id=content_object_id,
        destination_name="library",
        locator=_LOCATOR,
        template_version="seed-1",
    ).model_copy(update={"state": PlacementState.PRESENT, "verified_at": _NOW})
    monkeypatch.setattr(ledger, "held", present)

    async def reserve(connection: AsyncConnection, **_: object) -> tuple[str, None]:
        """Return the rendered locator, free."""
        return _LOCATOR, None

    monkeypatch.setattr(pipeline, "reserve", reserve)
    source = SourceObject.build(
        provider=Provider.PCLOUD,
        account="source-account",
        native_id="f1",
        path="/Photos/IMG_0001.JPG",
        size_bytes=10,
        modified_at=_NOW,
    )
    started = await pipeline.start(
        _connection(),
        content_object_id=content_object_id,
        fingerprint=StrongFingerprint(algorithm=HashAlgorithm.SHA256, digest="a" * 64),
        destination_name="library",
        locator=_LOCATOR,
        template_version="seed-1",
        source=source,
        transformations={},
        attempt=1,
        now=_NOW,
    )
    assert started == (present, False)
    assert ledger.reads == [True]
