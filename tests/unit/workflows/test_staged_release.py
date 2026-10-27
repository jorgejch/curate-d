"""Releasing a consumed staged version, against in-memory staging and a stubbed DBOS."""

import io
from collections.abc import Coroutine, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from uuid import UUID, uuid4

import pytest
from dbos import DBOS, AsyncSQLAlchemyDatasource
from dbos import error as dbos_error
from structlog.testing import capture_logs

from curate_d.config import Settings
from curate_d.ingest.pipeline import StagedRef
from curate_d.runtime import services
from curate_d.runtime.queues import QueueName
from curate_d.runtime.services import ServicesNotInstalledError
from curate_d.staging.errors import StagingObjectStoreError
from curate_d.staging.factories import StagingReaderBackend, StagingWriterBackend
from curate_d.staging.keys import StagingKey
from curate_d.staging.publish import publish
from curate_d.workflows import staged_release
from curate_d.workflows.staged_release import ReleaseOutcome
from tests.stubs.staging import MemoryStaging

_KEY = StagingKey("ab" * 16)


@dataclass
class _Access:
    """Staging as a process sees it, counting the work handed to its loop."""

    memory: MemoryStaging
    has_writer: bool = True
    handed: list[str] = field(default_factory=list)

    @property
    def writer(self) -> StagingWriterBackend | None:
        """The in-memory writer, unless this process is a reader."""
        return self.memory.writer if self.has_writer else None

    @property
    def reader(self) -> StagingReaderBackend | None:
        """The in-memory reader."""
        return self.memory.reader

    async def run[T](self, work: Coroutine[None, None, T]) -> T:
        """Await `work`, recording that it went through the staging loop."""
        self.handed.append(getattr(work, "__qualname__", ""))
        return await work


@pytest.fixture
def memory(tmp_path: Path) -> MemoryStaging:
    """Empty in-memory staging."""
    return MemoryStaging(tmp_path / "snapshots")


@pytest.fixture
def access(memory: MemoryStaging) -> Iterator[_Access]:
    """Install services whose staging is `memory`, and remove them afterwards."""
    installed = _Access(memory)
    services.install(
        services.Services(
            settings=Settings(),
            datasource=cast(AsyncSQLAlchemyDatasource, object()),
            staging=installed,
        )
    )
    yield installed
    services.uninstall()


async def _stage(memory: MemoryStaging, content: bytes) -> UUID:
    """Publish `content` under the test key, returning the attempt that won it."""
    published = await publish(memory.writer, _KEY, io.BytesIO(content), size_bytes=len(content))
    return published.staged.attempt_id


def _ref(attempt_id: UUID | None) -> StagedRef:
    """A staged reference to the test key naming `attempt_id`."""
    return StagedRef(key=_KEY, sha256="0" * 64, sha1="0" * 40, size_bytes=1, attempt_id=attempt_id)


async def test_the_step_releases_the_attempt_through_the_staging_loop(
    access: _Access, memory: MemoryStaging
) -> None:
    """The consumed version's row and bytes go, on the loop that owns the writer."""
    attempt = await _stage(memory, b"consumed")

    with capture_logs() as events:
        outcome = await staged_release._release_step(_KEY, attempt)

    assert outcome is ReleaseOutcome.RELEASED
    assert memory.content(_KEY) is None
    assert memory.store.objects == {}
    assert access.handed == ["StagingOccupancy.release"]
    assert [e["event"] for e in events] == ["staging.released"]
    assert events[0]["deleted"] is True


async def test_a_repeated_release_leaves_nothing_to_do(
    access: _Access, memory: MemoryStaging
) -> None:
    """A replayed or doubled release finds the attempt gone and changes nothing."""
    attempt = await _stage(memory, b"consumed")
    await staged_release._release_step(_KEY, attempt)

    assert await staged_release._release_step(_KEY, attempt) is ReleaseOutcome.LEFT


async def test_a_release_of_a_replaced_attempt_leaves_the_newer_version(
    access: _Access, memory: MemoryStaging
) -> None:
    """The key was restaged after it was read: the newer version stays readable."""
    old = await _stage(memory, b"old")
    await _stage(memory, b"newer")

    assert await staged_release._release_step(_KEY, old) is ReleaseOutcome.LEFT
    assert memory.content(_KEY) == b"newer"


async def test_a_release_during_a_download_keeps_the_bytes(
    access: _Access, memory: MemoryStaging, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reader still holding the version keeps reading; the sweep collects the bytes."""
    attempt = await _stage(memory, b"held")

    async def held(key: StagingKey) -> None:
        """Refuse the eviction, as the index does for a key a download holds."""

    monkeypatch.setattr(memory.index, "evict", held)

    assert await staged_release._release_step(_KEY, attempt) is ReleaseOutcome.LEFT
    assert memory.index.registrations == {}
    assert len(memory.store.objects) == 1


async def test_a_refused_store_raises_from_the_step_for_dbos_to_retry(
    access: _Access, memory: MemoryStaging, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A staging failure is retryable; the unregistration stands and the bytes are an orphan."""
    attempt = await _stage(memory, b"refused")

    async def refuse(object_key: str) -> None:
        raise StagingObjectStoreError(f"delete of {object_key} refused")

    monkeypatch.setattr(memory.store, "delete", refuse)

    with pytest.raises(StagingObjectStoreError) as raised:
        await staged_release._release_step(_KEY, attempt)

    assert staged_release._retryable(raised.value) is True
    assert memory.index.registrations == {}


async def test_a_process_without_a_writer_refuses_and_is_not_retried(
    access: _Access, memory: MemoryStaging
) -> None:
    """Only the I/O profile releases; a reader running the step is a deployment fault."""
    access.has_writer = False

    with pytest.raises(ServicesNotInstalledError, match="writer") as raised:
        await staged_release._release_step(_KEY, uuid4())

    assert staged_release._retryable(raised.value) is False


async def test_exhausted_retries_end_the_release_as_failed_and_log_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed release never raises into the workflow; the sweep is the retry from here."""
    attempt = uuid4()

    async def exhausted(key: str, attempt_id: UUID) -> ReleaseOutcome:
        raise dbos_error.DBOSMaxStepRetriesExceeded(
            "_release_step", 3, [StagingObjectStoreError("index unreachable")]
        )

    monkeypatch.setattr(staged_release, "_release_step", exhausted)

    with capture_logs() as events:
        outcome = await staged_release._release_reporting_failure(_KEY, attempt)

    assert outcome is ReleaseOutcome.FAILED
    assert events == [
        {
            "event": "staging.release_failed",
            "log_level": "warning",
            "staging_key": _KEY,
            "attempt_id": str(attempt),
            "attempts": staged_release.RELEASE_ATTEMPTS,
            "error_type": "StagingObjectStoreError",
        }
    ]


async def test_a_deployment_fault_is_raised_rather_than_reported_as_failed() -> None:
    """Only exhausted retries become FAILED; a process with no staging fails visibly."""
    with pytest.raises(ServicesNotInstalledError):
        await staged_release._release_reporting_failure(_KEY, uuid4())


def test_the_release_identity_is_the_key_and_attempt() -> None:
    """A replayed or doubled enqueue of the same attempt collapses onto one release."""
    attempt = uuid4()

    assert staged_release.release_workflow_id(_ref(attempt)) == (
        f"release-staged:{_KEY}:{attempt.hex}"
    )
    with pytest.raises(ValueError, match="names no attempt"):
        staged_release.release_workflow_id(_ref(None))


@pytest.fixture
def enqueued(monkeypatch: pytest.MonkeyPatch) -> list[tuple[object, ...]]:
    """Record every enqueue instead of reaching DBOS."""
    calls: list[tuple[object, ...]] = []

    async def enqueue(queue: str, fn: object, *args: object) -> None:
        calls.append((queue, fn, *args))

    monkeypatch.setattr(DBOS, "enqueue_workflow_async", enqueue)
    return calls


async def test_a_release_is_handed_to_the_maintenance_queue(
    enqueued: list[tuple[object, ...]],
) -> None:
    """The CPU profile cannot release, so it enqueues the release for the I/O profile."""
    attempt = uuid4()

    await staged_release.enqueue_staged_release(_ref(attempt))

    assert enqueued == [
        (QueueName.MAINTENANCE.value, staged_release.release_staged_entry, _KEY, attempt)
    ]


async def test_a_reference_naming_no_attempt_enqueues_nothing(
    enqueued: list[tuple[object, ...]],
) -> None:
    """A ledger-built or pre-deploy reference may name a key another reader still needs."""
    await staged_release.enqueue_staged_release(_ref(None))

    assert enqueued == []


def _status(*args: object, **kwargs: object) -> SimpleNamespace:
    """A workflow status whose recorded inputs are `args` and `kwargs`."""
    return SimpleNamespace(input={"args": args, "kwargs": kwargs})


@pytest.mark.parametrize(
    ("status", "reads"),
    [
        pytest.param(_status("content", _ref(UUID(int=1))), True, id="handed-positionally"),
        pytest.param(_status("content", staged=_ref(UUID(int=1))), True, id="handed-by-name"),
        pytest.param(_status("content", _ref(UUID(int=2))), False, id="another-attempt"),
        pytest.param(_status("content", _ref(None)), False, id="ledger-built"),
        pytest.param(_status("content"), False, id="no-staged-input"),
        pytest.param(SimpleNamespace(input=None), False, id="inputs-not-loaded"),
        pytest.param(None, False, id="pruned"),
    ],
)
async def test_a_workflow_reads_a_copy_only_when_it_was_handed_that_attempt(
    monkeypatch: pytest.MonkeyPatch, status: SimpleNamespace | None, reads: bool
) -> None:
    """Classification handed another source's copy, or long finished, leaves this one free."""
    asked: list[str] = []

    async def get_status(workflow_id: str) -> SimpleNamespace | None:
        asked.append(workflow_id)
        return status

    monkeypatch.setattr(DBOS, "get_workflow_status_async", get_status)

    assert await staged_release.reads_staged("classify:sha256:x", _ref(UUID(int=1))) is reads
    assert asked == ["classify:sha256:x"]


async def test_a_reference_naming_no_attempt_is_never_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without an attempt there is nothing to match, so nothing is released for it either."""

    async def get_status(workflow_id: str) -> SimpleNamespace:
        return _status("content", _ref(None))

    monkeypatch.setattr(DBOS, "get_workflow_status_async", get_status)

    assert await staged_release.reads_staged("classify:sha256:x", _ref(None)) is False
