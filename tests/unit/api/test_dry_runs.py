"""Dry runs: starting one, its status, and the bulk export of what it proposes."""

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

import pytest
from dbos import DBOSClient, EnqueueOptions, WorkflowStatus
from sqlalchemy.ext.asyncio import AsyncEngine

from curate_d.api import dry_runs as module
from curate_d.api.dry_runs import DryRunStatus, LedgerDryRuns, RunState, export_of, status_of
from curate_d.api.errors import NotFoundError
from curate_d.domain import (
    ContentObjectId,
    DryRunOutcome,
    Facts,
    MediaKind,
    RejectedValue,
    TaxonomyField,
    VerificationLevel,
    derive_destination_id,
)
from curate_d.placement.dry_run import DryRun, DryRunItem, DryRunRequest
from tests.unit.api.fakes import FakeDryRuns, backends, client

_AT = datetime(2026, 9, 30, 8, tzinfo=UTC)
_LIBRARY = derive_destination_id("pcloud", "1", "/library")
_RECEIPTS = RejectedValue(field=TaxonomyField.SUBCATEGORY, value="Receipts", category="Documents")


def _items(run_id: UUID) -> list[DryRunItem]:
    """A proposal and an unresolved item with a refused value."""
    return [
        DryRunItem(
            run_id=run_id,
            content_object_id=ContentObjectId(uuid4()),
            destination_name="library",
            destination_id=_LIBRARY,
            outcome=DryRunOutcome.PROPOSED,
            paths=("/Camera/IMG_0001.JPG",),
            facts=Facts(media_kind=MediaKind.PHOTO, category="Photos"),
            locator="/library/Photos/2024/2024-05/IMG_0001.JPG",
            expected_level=VerificationLevel.STRONG_COMMON_HASH,
        ),
        DryRunItem(
            run_id=run_id,
            content_object_id=ContentObjectId(uuid4()),
            outcome=DryRunOutcome.UNCLASSIFIED,
            paths=("/docs/receipt.pdf",),
            reason="awaiting review (rejected_value): subcategory outside the taxonomy",
            rejected_values=(_RECEIPTS,),
        ),
    ]


def _header(run_id: UUID, *, finished: bool = True) -> DryRun:
    """A run's recorded header."""
    return DryRun(
        id=run_id,
        started_at=_AT,
        finished_at=_AT if finished else None,
        template_version="seed-1",
        taxonomy_version=2,
    )


def _finished(run_id: UUID) -> DryRunStatus:
    """A finished run's status."""
    return status_of(run_id, _header(run_id), None)


def _workflow(state: str, error: Exception | None = None) -> WorkflowStatus:
    """A dry run's workflow in `state`."""
    status = WorkflowStatus()
    # DBOS types the state as a literal; the parametrized cases name it as a string.
    setattr(status, "status", state)  # noqa: B010
    status.error = error
    return status


def test_a_finished_header_is_final_whatever_the_workflow_says() -> None:
    """Finished, with the configuration it ran with."""
    run_id = uuid4()
    status = status_of(run_id, _header(run_id), _workflow("ERROR", ValueError("late")))
    assert (status.state, status.template_version) == (RunState.FINISHED, "seed-1")


@pytest.mark.parametrize(
    ("header", "workflow", "state", "detail"),
    [
        (False, _workflow("ENQUEUED"), RunState.QUEUED, None),
        (False, _workflow("PENDING"), RunState.RUNNING, None),
        (True, _workflow("PENDING"), RunState.RUNNING, None),
        (
            True,
            _workflow("ERROR", ValueError("ledger gone")),
            RunState.FAILED,
            "ERROR: ledger gone",
        ),
        (
            False,
            _workflow("MAX_RECOVERY_ATTEMPTS_EXCEEDED"),
            RunState.FAILED,
            "MAX_RECOVERY_ATTEMPTS_EXCEEDED",
        ),
        (True, _workflow("CANCELLED"), RunState.CANCELLED, "cancelled"),
        (False, _workflow("CANCELLED"), RunState.CANCELLED, "cancelled"),
    ],
)
def test_an_unfinished_run_takes_its_workflow_state(
    header: bool, workflow: WorkflowStatus, state: RunState, detail: str | None
) -> None:
    """Waiting, running, or stopped with the reason a caller needs to stop polling."""
    run_id = uuid4()
    status = status_of(run_id, _header(run_id, finished=False) if header else None, workflow)
    assert (status.state, status.detail) == (state, detail)


def test_an_unfinished_run_whose_workflow_is_gone_has_failed() -> None:
    """It will never finish; say so rather than report it running forever."""
    run_id = uuid4()
    status = status_of(run_id, _header(run_id, finished=False), None)
    assert status.state is RunState.FAILED
    assert status.detail is not None


def test_a_run_with_neither_header_nor_workflow_is_not_found() -> None:
    """Never started."""
    with pytest.raises(NotFoundError, match="no dry run"):
        status_of(uuid4(), None, None)


def test_starting_a_run_takes_destinations_as_json() -> None:
    """Accepted, with the destinations parsed from their JSON forms."""
    fake = FakeDryRuns()
    with client(replace(backends(), dry_runs=fake)) as http:
        response = http.post(
            "/dry-runs",
            json={
                "destinations": [
                    {"name": "library", "provider": "pcloud", "account": "1", "root_path": "/Org"}
                ]
            },
        )
    assert response.status_code == 202
    [request] = fake.started
    assert response.json() == {
        "run_id": str(request.run_id),
        "workflow_id": f"dry-run:{request.run_id}",
    }
    assert [d.name for d in request.destinations] == ["library"]


def test_a_malformed_destination_is_refused() -> None:
    """Destination names are lowercase slugs."""
    fake = FakeDryRuns()
    with client(replace(backends(), dry_runs=fake)) as http:
        response = http.post(
            "/dry-runs",
            json={
                "destinations": [
                    {"name": "My Library", "provider": "pcloud", "account": "1", "root_path": "/"}
                ]
            },
        )
    assert response.status_code == 422
    assert fake.started == []


@pytest.mark.parametrize(
    ("second", "problem"),
    [
        ({"name": "mirror", "account": "1", "root_path": "/Org/"}, "same place"),
        ({"name": "mirror", "account": "1", "root_path": "/Org/Photos"}, "overlap"),
        ({"name": "library", "account": "2", "root_path": "/Other"}, "unique"),
    ],
)
def test_destinations_a_policy_would_refuse_are_refused(
    second: dict[str, str], problem: str
) -> None:
    """One place under two names, nested roots in one account, or a repeated name: 422."""
    fake = FakeDryRuns()
    library = {"name": "library", "provider": "pcloud", "account": "1", "root_path": "/Org"}
    with client(replace(backends(), dry_runs=fake)) as http:
        response = http.post(
            "/dry-runs", json={"destinations": [library, {"provider": "pcloud", **second}]}
        )
    assert response.status_code == 422
    assert problem in response.text
    assert fake.started == []


def test_one_root_in_two_accounts_is_accepted() -> None:
    """Two accounts' copies of one template are two destinations."""
    fake = FakeDryRuns()
    destinations = [
        {"name": name, "provider": "pcloud", "account": account, "root_path": "/Org"}
        for name, account in (("library", "1"), ("mirror", "2"))
    ]
    with client(replace(backends(), dry_runs=fake)) as http:
        response = http.post("/dry-runs", json={"destinations": destinations})
    assert response.status_code == 202
    [request] = fake.started
    assert len({d.id for d in request.destinations}) == 2


def test_the_export_carries_facts_locators_and_what_could_not_be_resolved() -> None:
    """Counted by outcome; unresolved items keep their reason and refused values."""
    run_id = uuid4()
    fake = FakeDryRuns(runs={run_id: (_finished(run_id), _items(run_id))})
    with client(replace(backends(), dry_runs=fake)) as http:
        exported = http.get(f"/dry-runs/{run_id}/export").json()
    assert exported["run"]["state"] == "finished"
    assert exported["counts"]["proposed"] == 1
    assert exported["counts"]["unclassified"] == 1
    proposed, unresolved = exported["items"]
    assert proposed["facts"]["category"] == "Photos"
    assert proposed["locator"] == "/library/Photos/2024/2024-05/IMG_0001.JPG"
    assert proposed["expected_verification"] == "strong_common_hash"
    assert proposed["destination_id"] == str(_LIBRARY)
    assert unresolved["destination_id"] is None
    assert unresolved["reason"].startswith("awaiting review")
    assert unresolved["rejected_values"] == [_RECEIPTS.model_dump(mode="json")]


def test_an_unknown_run_is_reported_as_such() -> None:
    """Status and export both answer 404."""
    missing = uuid4()
    with client() as http:
        responses = [http.get(f"/dry-runs/{missing}"), http.get(f"/dry-runs/{missing}/export")]
    assert [r.status_code for r in responses] == [404, 404]
    assert responses[0].json() == {"detail": f"no dry run {missing}"}


def test_counts_include_every_outcome_even_when_none_occurred() -> None:
    """A reader can rely on every key being there."""
    export = export_of(_finished(uuid4()), [])
    assert set(export.counts) == set(DryRunOutcome)
    assert sum(export.counts.values()) == 0


class _Workflows:
    """Records enqueues and reports which workflows exist."""

    def __init__(self, existing: Sequence[str] = ()) -> None:
        """The workflow ids that exist."""
        self.existing = list(existing)
        self.enqueued: list[tuple[EnqueueOptions, DryRunRequest]] = []

    async def enqueue_async(self, options: EnqueueOptions, request: DryRunRequest) -> None:
        """Record the enqueue."""
        self.enqueued.append((options, request))

    async def list_workflows_async(
        self, *, workflow_ids: list[str], **_: object
    ) -> list[WorkflowStatus]:
        """The requested workflows that exist, waiting in the queue."""
        return [_workflow("ENQUEUED") for i in workflow_ids if i in self.existing]


class _Engine:
    """Hands out a connection the fakes ignore."""

    def connect(self) -> "_Engine":
        """Itself, as a context manager."""
        return self

    async def __aenter__(self) -> object:
        """A connection."""
        return object()

    async def __aexit__(self, *_exc: object) -> None:
        """Nothing to release."""


@pytest.fixture
def no_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    """No run has recorded its header."""

    class Store:
        def __init__(self, _connection: object) -> None:
            """Nothing to bind."""

        async def get(self, _run_id: UUID) -> DryRun | None:
            """No header."""
            return None

    monkeypatch.setattr(module, "DryRunStore", Store)


async def test_a_run_is_enqueued_on_maintenance_under_its_own_identity() -> None:
    """The workflow name the workers registered, and an id a repeat collapses onto."""
    workflows = _Workflows()
    backend = LedgerDryRuns(cast(AsyncEngine, _Engine()), cast(DBOSClient, workflows))
    request = DryRunRequest(run_id=uuid4())
    workflow_id = await backend.start(request)
    [(options, enqueued)] = workflows.enqueued
    assert options == {
        "workflow_name": "dry_run",
        "queue_name": "maintenance",
        "workflow_id": workflow_id,
    }
    assert enqueued is request


async def test_a_run_waiting_to_start_is_queued(no_headers: None) -> None:
    """Enqueued but without a header yet."""
    run_id = uuid4()
    backend = LedgerDryRuns(cast(AsyncEngine, _Engine()), cast(DBOSClient, _Workflows()))
    workflows = _Workflows(existing=[await backend.start(DryRunRequest(run_id=run_id))])
    backend = LedgerDryRuns(cast(AsyncEngine, _Engine()), cast(DBOSClient, workflows))
    status = await backend.status(run_id)
    assert (status.state, status.started_at) == (RunState.QUEUED, None)


async def test_a_run_that_was_never_started_is_not_found(no_headers: None) -> None:
    """Neither a header nor a workflow."""
    backend = LedgerDryRuns(cast(AsyncEngine, _Engine()), cast(DBOSClient, _Workflows()))
    with pytest.raises(NotFoundError, match="no dry run"):
        await backend.status(uuid4())
