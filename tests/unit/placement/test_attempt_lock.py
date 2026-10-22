"""The placement row each attempt check reads is locked for the writing transaction.

The repository's statement is compiled against the PostgreSQL dialect; the pipeline's
checks run against a repository that only records how it was read. A start also locks
the placement itself before its paths and its row, which a missing row cannot lock.
"""

from datetime import UTC, datetime
from typing import ClassVar, cast
from uuid import uuid4

import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql import ClauseElement

from curate_d.domain import (
    AuditEntry,
    AuditEntryType,
    ContentObjectId,
    HashAlgorithm,
    Placement,
    PlacementId,
    PlacementState,
    Provider,
    SourceObject,
    StrongFingerprint,
    derive_placement_id,
)
from curate_d.ledger import PlacementRepository
from curate_d.placement import pipeline
from curate_d.placement.verification import Assessment, Verdict

_NOW = datetime(2026, 7, 1, 8, 0, tzinfo=UTC)
_LOCATOR = "/Photos/2026/IMG_0001.JPG"
_OTHER_LOCATOR = "/Photos/2026-07/IMG_0001.JPG"
_POSTGRESQL = make_url("postgresql+psycopg://").get_dialect()()


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


class _Journal:
    """A connection that journals each statement it executes and returns no rows.

    An advisory lock is journaled by the name it is keyed on; anything else as a
    statement.
    """

    def __init__(self, journal: list[str]) -> None:
        """Journal into `journal`."""
        self.journal = journal

    async def execute(self, statement: ClauseElement) -> _NoRows:
        """Journal `statement` and return an empty result."""
        compiled = statement.compile(dialect=_POSTGRESQL)
        if "pg_advisory_xact_lock(hashtextextended(" in str(compiled):
            [name] = [value for value in compiled.params.values() if isinstance(value, str)]
            self.journal.append(f"lock {name}")
        else:
            self.journal.append("statement")
        return _NoRows()


class _Ledger:
    """A placement repository that records each read and journals what it was asked."""

    reads: ClassVar[list[bool]] = []
    journal: ClassVar[list[str]] = []
    held: ClassVar[Placement | None] = None

    def __init__(self, connection: object) -> None:
        """Ignore the connection."""

    async def get(self, placement_id: PlacementId, *, for_update: bool = False) -> Placement | None:
        """Record the read and return the held placement."""
        self.reads.append(for_update)
        self.journal.append("read locked" if for_update else "read")
        return self.held

    async def find_at_locator(self, *, destination_name: str, locator: str) -> Placement | None:
        """Journal the look at a path and return nothing holding it."""
        self.journal.append("find")
        return None

    async def upsert(self, placement: Placement) -> Placement:
        """Journal the write and return `placement` as stored."""
        self.journal.append("upsert")
        return placement


class _Audit:
    """An audit repository that journals each entry appended."""

    def __init__(self, connection: object) -> None:
        """Ignore the connection."""

    async def append(self, entry: AuditEntry) -> int:
        """Journal the entry's type and return an id."""
        _Ledger.journal.append(f"audit {entry.entry_type.value}")
        return 1


@pytest.fixture
def ledger(monkeypatch: pytest.MonkeyPatch) -> type[_Ledger]:
    """Replace the pipeline's placement and audit repositories with ones holding nothing."""
    monkeypatch.setattr(_Ledger, "reads", [])
    monkeypatch.setattr(_Ledger, "journal", [])
    monkeypatch.setattr(_Ledger, "held", None)
    monkeypatch.setattr(pipeline, "PlacementRepository", _Ledger)
    monkeypatch.setattr(pipeline, "AuditRepository", _Audit)
    return _Ledger


def _connection() -> AsyncConnection:
    """Return a connection the replaced repository never touches."""
    return cast(AsyncConnection, None)


def _journaling(ledger: type[_Ledger]) -> AsyncConnection:
    """Return a connection journaling into the replaced repository's journal."""
    return cast(AsyncConnection, _Journal(ledger.journal))


def _source() -> SourceObject:
    """Return the source a start copies."""
    return SourceObject.build(
        provider=Provider.PCLOUD,
        account="source-account",
        native_id="f1",
        path="/Photos/IMG_0001.JPG",
        size_bytes=10,
        modified_at=_NOW,
    )


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
    started = await pipeline.start(
        _journaling(ledger),
        content_object_id=content_object_id,
        fingerprint=StrongFingerprint(algorithm=HashAlgorithm.SHA256, digest="a" * 64),
        destination_name="library",
        locator=_LOCATOR,
        template_version="seed-1",
        source=_source(),
        transformations={},
        attempt=1,
        now=_NOW,
    )
    assert started == (present, False)
    assert ledger.reads == [True]


async def test_a_first_start_locks_the_placement_before_its_path_and_row(
    ledger: type[_Ledger],
) -> None:
    """With no row to lock yet, a start locks the placement, then the path, then reads."""
    content_object_id = ContentObjectId(uuid4())
    placement_id = derive_placement_id(content_object_id, "library")
    placement, copy = await pipeline.start(
        _journaling(ledger),
        content_object_id=content_object_id,
        fingerprint=StrongFingerprint(algorithm=HashAlgorithm.SHA256, digest="a" * 64),
        destination_name="library",
        locator=_LOCATOR,
        template_version="seed-1",
        source=_source(),
        transformations={},
        now=_NOW,
    )
    assert copy
    assert (placement.id, placement.state, placement.attempts) == (
        placement_id,
        PlacementState.COPYING,
        0,
    )
    assert ledger.journal == [
        f"lock placement:{placement_id}",
        f"lock library\n{_LOCATOR}",
        "find",
        "read locked",
        "upsert",
        "statement",
        f"audit {AuditEntryType.PLACEMENT_STARTED.value}",
    ]


def _held(
    content_object_id: ContentObjectId, *, state: PlacementState, attempts: int, locator: str
) -> Placement:
    """Return a placement of the content at `library` in `state`, recorded as `attempts`."""
    return Placement.build(
        content_object_id=content_object_id,
        destination_name="library",
        locator=locator,
        template_version="seed-1",
    ).model_copy(update={"state": state, "attempts": attempts})


async def _start_at_locator(
    ledger: type[_Ledger], content_object_id: ContentObjectId, *, attempt: int
) -> tuple[Placement, bool]:
    """Start a copy of the content at `library`, rendered to `_LOCATOR`, as `attempt`."""
    return await pipeline.start(
        _journaling(ledger),
        content_object_id=content_object_id,
        fingerprint=StrongFingerprint(algorithm=HashAlgorithm.SHA256, digest="a" * 64),
        destination_name="library",
        locator=_LOCATOR,
        template_version="seed-1",
        source=_source(),
        transformations={},
        attempt=attempt,
        now=_NOW,
    )


@pytest.mark.parametrize("state", [PlacementState.COPYING, PlacementState.VERIFYING])
async def test_a_start_leaves_its_attempt_to_one_under_way_at_another_locator(
    ledger: type[_Ledger], monkeypatch: pytest.MonkeyPatch, state: PlacementState
) -> None:
    """A start of the attempt already under way elsewhere copies nothing and writes nothing."""
    content_object_id = ContentObjectId(uuid4())
    held = _held(content_object_id, state=state, attempts=0, locator=_OTHER_LOCATOR)
    monkeypatch.setattr(ledger, "held", held)
    started = await _start_at_locator(ledger, content_object_id, attempt=0)
    assert started == (held, False)
    assert ledger.journal == [
        f"lock placement:{held.id}",
        f"lock library\n{_LOCATOR}",
        "find",
        "read locked",
    ]


@pytest.mark.parametrize(
    ("locator", "attempts", "attempt"),
    [(_LOCATOR, 0, 0), (_OTHER_LOCATOR, 2, 1)],
    ids=["same attempt run again at its locator", "older attempt at another locator"],
)
async def test_a_start_over_an_attempt_under_way_records_the_next_one(
    ledger: type[_Ledger],
    monkeypatch: pytest.MonkeyPatch,
    locator: str,
    attempts: int,
    attempt: int,
) -> None:
    """A rewound start, or an older attempt's, still records the attempt after the latest."""
    content_object_id = ContentObjectId(uuid4())
    held = _held(
        content_object_id, state=PlacementState.COPYING, attempts=attempts, locator=locator
    )
    monkeypatch.setattr(ledger, "held", held)
    placement, copy = await _start_at_locator(ledger, content_object_id, attempt=attempt)
    assert copy
    assert (placement.locator, placement.attempts) == (_LOCATOR, attempts + 1)
    assert "upsert" in ledger.journal


def test_a_path_lock_keeps_the_name_older_builds_take() -> None:
    """A path's lock name is unchanged, so starts of mixed builds still exclude each other."""
    assert pipeline.path_lock_name("library", _LOCATOR) == f"library\n{_LOCATOR}"


def test_no_path_lock_shares_a_placement_lock_name() -> None:
    """A placement's lock name is not a path's, even at a locator spelling one."""
    placement_id = PlacementId(uuid4())
    name = pipeline.placement_lock_name(placement_id)
    assert name not in {
        pipeline.path_lock_name("placement", str(placement_id)),
        pipeline.path_lock_name("placement", f":{placement_id}"),
    }
