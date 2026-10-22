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
from curate_d.api.dry_runs import (
    DryRunDrafts,
    DryRunStatus,
    ExportedRule,
    LedgerDryRuns,
    RuleCount,
    RunState,
    drafts_of,
    export_of,
    status_of,
)
from curate_d.api.errors import NotFoundError, RejectedError
from curate_d.configuration.documents import parse_policy
from curate_d.configuration.store import ConfigurationError
from curate_d.domain import (
    SEED_TAXONOMY,
    ConfigurationKind,
    ContentObjectId,
    DryRunOutcome,
    Facts,
    MediaKind,
    PolicySource,
    RejectedValue,
    Taxonomy,
    TaxonomyField,
    VerificationLevel,
)
from curate_d.placement.dry_run import (
    NO_DESTINATION,
    DryRun,
    DryRunItem,
    DryRunRequest,
    SelectingRule,
)
from curate_d.rendering.templates import SEED_TEMPLATES
from tests.unit.api.fakes import FakeDryRuns, backends, client

_AT = datetime(2026, 9, 30, 8, tzinfo=UTC)
_RECEIPTS = RejectedValue(field=TaxonomyField.SUBCATEGORY, value="Receipts", category="Documents")


def _items(run_id: UUID) -> list[DryRunItem]:
    """A proposal and an unresolved item with a refused value."""
    return [
        DryRunItem(
            run_id=run_id,
            content_object_id=ContentObjectId(uuid4()),
            destination_name="library",
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


_LIBRARY = {"name": "library", "provider": "pcloud", "account": "1", "root_path": "/Org"}
_DRAFT_POLICY: dict[str, object] = {
    "destinations": [_LIBRARY],
    "rules": [{"name": "photos", "when": {"media_kind": "photo"}, "destinations": ["library"]}],
}


def test_starting_a_run_takes_a_draft_policy_and_template_set_as_json() -> None:
    """Accepted, with both drafts parsed as `PUT /configuration` parses them."""
    fake = FakeDryRuns()
    templates = SEED_TEMPLATES.model_dump(mode="json") | {"version": "draft-2"}
    with client(replace(backends(), dry_runs=fake)) as http:
        response = http.post("/dry-runs", json={"policy": _DRAFT_POLICY, "templates": templates})
    assert response.status_code == 202
    [request] = fake.started
    assert request.policy == parse_policy(_DRAFT_POLICY)
    assert request.templates is not None
    assert request.templates.version == "draft-2"
    assert request.destinations == ()


@pytest.mark.parametrize(
    ("order", "location"),
    [
        (
            {"policy": _DRAFT_POLICY | {"rules": [{"name": "x", "destinations": ["nowhere"]}]}},
            ["body", "policy"],
        ),
        (
            {
                "policy": _DRAFT_POLICY
                | {"rules": [{"name": "x", "when": {"mood": "calm"}, "destinations": ["library"]}]}
            },
            ["body", "policy"],
        ),
        (
            {
                "policy": _DRAFT_POLICY
                | {
                    "rules": [
                        {"name": "x", "when": {"media_kind": "photos"}, "destinations": ["library"]}
                    ]
                }
            },
            ["body", "policy"],
        ),
        ({"policy": _DRAFT_POLICY | {"destinatons": []}}, ["body", "policy"]),
        (
            {"templates": SEED_TEMPLATES.model_dump(mode="json") | {"templates": []}},
            ["body", "templates"],
        ),
    ],
)
def test_an_invalid_draft_is_refused_and_no_run_starts(
    order: dict[str, object], location: list[str]
) -> None:
    """Undefined destinations, unknown fields, kinds and keys are located; nothing starts."""
    fake = FakeDryRuns()
    with client(replace(backends(), dry_runs=fake)) as http:
        response = http.post("/dry-runs", json=order)
    assert response.status_code == 422
    assert any(e["loc"][:2] == location for e in response.json()["detail"])
    assert fake.started == []


def test_a_draft_policy_with_named_destinations_is_refused() -> None:
    """Named destinations ignore rules, so the draft would decide nothing."""
    fake = FakeDryRuns()
    with client(replace(backends(), dry_runs=fake)) as http:
        response = http.post(
            "/dry-runs", json={"policy": _DRAFT_POLICY, "destinations": [_LIBRARY]}
        )
    assert response.status_code == 422
    assert "draft policy" in response.text
    assert fake.started == []


def test_named_destinations_with_a_draft_template_set_are_accepted() -> None:
    """A draft template set renders at named destinations as well."""
    fake = FakeDryRuns()
    templates = SEED_TEMPLATES.model_dump(mode="json") | {"version": "draft-3"}
    with client(replace(backends(), dry_runs=fake)) as http:
        response = http.post("/dry-runs", json={"destinations": [_LIBRARY], "templates": templates})
    assert response.status_code == 202
    [request] = fake.started
    assert request.policy is None
    assert request.templates is not None
    assert request.templates.version == "draft-3"


def test_a_draft_the_backend_refuses_is_answered_422() -> None:
    """A refusal raised while starting is reported as a problem."""
    fake = FakeDryRuns(refusal=RejectedError("invalid policy configuration: rules: no"))
    with client(replace(backends(), dry_runs=fake)) as http:
        response = http.post("/dry-runs", json={"policy": _DRAFT_POLICY})
    assert response.status_code == 422
    assert response.json() == {"detail": "invalid policy configuration: rules: no"}


def test_the_status_says_what_the_run_evaluated_with() -> None:
    """The policy source and version, its rules, and whether the templates were a draft."""
    run_id = uuid4()
    header = _header(run_id).model_copy(
        update={
            "policy_source": PolicySource.ACTIVE,
            "policy_version": 4,
            "policy_rules": ("photos", "documents"),
        }
    )
    status = status_of(run_id, header, None)
    assert (status.policy_source, status.policy_version, status.policy_rules) == (
        PolicySource.ACTIVE,
        4,
        ["photos", "documents"],
    )
    assert not status.templates_draft

    drafted = status_of(
        run_id,
        _header(run_id).model_copy(
            update={"policy_source": PolicySource.DRAFT, "templates_draft": True}
        ),
        None,
    )
    assert (drafted.policy_source, drafted.policy_version, drafted.templates_draft) == (
        PolicySource.DRAFT,
        None,
        True,
    )


_EVERYTHING = SelectingRule(index=0, name="everything")
_PHOTOS = SelectingRule(index=1, name="photos")


def test_the_export_names_the_selecting_rules_and_counts_every_rule() -> None:
    """Each item carries its rules; a rule that selected nothing is counted as zero."""
    run_id = uuid4()
    photo, other = ContentObjectId(uuid4()), ContentObjectId(uuid4())
    items = [
        DryRunItem(
            run_id=run_id,
            content_object_id=photo,
            destination_name=name,
            outcome=DryRunOutcome.PROPOSED,
            rules=(_EVERYTHING, _PHOTOS),
        )
        for name in ("backup", "library")
    ] + [
        DryRunItem(
            run_id=run_id,
            content_object_id=other,
            destination_name="library",
            outcome=DryRunOutcome.RENDER_FAILED,
            rules=(_EVERYTHING,),
        )
    ]
    status = _finished(run_id).model_copy(
        update={
            "policy_source": PolicySource.DRAFT,
            "policy_rules": ["everything", "photos", "receipts"],
        }
    )
    fake = FakeDryRuns(
        runs={run_id: (status, items)},
        drafted={run_id: DryRunDrafts(policy=_DRAFT_POLICY)},
    )
    with client(replace(backends(), dry_runs=fake)) as http:
        exported = http.get(f"/dry-runs/{run_id}/export").json()
    everything, photos = {"index": 0, "name": "everything"}, {"index": 1, "name": "photos"}
    assert [item["rules"] for item in exported["items"]] == [
        [everything, photos],
        [everything, photos],
        [everything],
    ]
    assert exported["rule_counts"] == [
        everything | {"count": 2},
        photos | {"count": 1},
        {"index": 2, "name": "receipts", "count": 0},
    ]
    assert exported["run"]["policy_source"] == "draft"
    assert exported["drafts"] == {"policy": _DRAFT_POLICY, "templates": None}


def test_rules_sharing_a_name_are_counted_apart() -> None:
    """Two rules named coverage that selected one object and none show both counts."""
    run_id = uuid4()
    item = DryRunItem(
        run_id=run_id,
        content_object_id=ContentObjectId(uuid4()),
        destination_name="library",
        outcome=DryRunOutcome.PROPOSED,
        rules=(SelectingRule(index=0, name="coverage"),),
    )
    status = _finished(run_id).model_copy(
        update={"policy_source": PolicySource.DRAFT, "policy_rules": ["coverage", "coverage"]}
    )
    export = export_of(status, [item])
    assert export.rule_counts == [
        RuleCount(index=0, name="coverage", count=1),
        RuleCount(index=1, name="coverage", count=0),
    ]
    assert export.items[0].rules == [ExportedRule(index=0, name="coverage")]


def test_unselected_content_is_counted_under_its_outcome_and_by_no_rule() -> None:
    """An unselected item has no destination and no rules; every rule still shows."""
    run_id = uuid4()
    unselected = DryRunItem(
        run_id=run_id,
        content_object_id=ContentObjectId(uuid4()),
        outcome=DryRunOutcome.UNSELECTED,
        paths=("/docs/receipt.pdf",),
        facts=Facts(media_kind=MediaKind.DOCUMENT, category="Receipts"),
    )
    status = _finished(run_id).model_copy(
        update={"policy_source": PolicySource.ACTIVE, "policy_rules": ["photos"]}
    )
    export = export_of(status, [unselected])
    assert export.counts[DryRunOutcome.UNSELECTED] == 1
    assert export.rule_counts == [RuleCount(index=0, name="photos", count=0)]
    [item] = export.items
    assert (item.destination, item.rules, item.locator) == (NO_DESTINATION, [], None)
    assert item.facts == unselected.facts


def test_a_run_with_named_destinations_counts_no_rules() -> None:
    """Its items name no rules, and its rule counts are empty."""
    run_id = uuid4()
    status = _finished(run_id).model_copy(update={"policy_source": PolicySource.DESTINATIONS})
    export = export_of(status, _items(run_id))
    assert export.rule_counts == []
    assert all(item.rules == [] for item in export.items)
    assert export.counts[DryRunOutcome.UNSELECTED] == 0


def test_drafts_come_from_the_header() -> None:
    """None without a header or without drafts; the documents as recorded otherwise."""
    run_id = uuid4()
    assert drafts_of(None) == DryRunDrafts()
    assert drafts_of(_header(run_id)) == DryRunDrafts()
    templates = SEED_TEMPLATES.model_dump(mode="json")
    recorded = _header(run_id).model_copy(
        update={"policy_document": _DRAFT_POLICY, "templates_document": templates}
    )
    assert drafts_of(recorded) == DryRunDrafts(policy=_DRAFT_POLICY, templates=templates)


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
        """Return itself, as a context manager."""
        return self

    def begin(self) -> "_Engine":
        """Return itself, as a context manager."""
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


@pytest.fixture(autouse=True)
def taxonomy(monkeypatch: pytest.MonkeyPatch) -> list[ConfigurationKind]:
    """Serve the seed taxonomy as the one in force, recording the shared locks taken."""
    locked: list[ConfigurationKind] = []

    class Repository:
        def __init__(self, _connection: object) -> None:
            """Nothing to bind."""

        async def lock_shared(self, kind: ConfigurationKind) -> None:
            """Record the lock."""
            locked.append(kind)

    async def in_force(_configurations: object) -> Taxonomy:
        """Return the seed."""
        return SEED_TAXONOMY

    monkeypatch.setattr(module, "ConfigurationRepository", Repository)
    monkeypatch.setattr(module, "taxonomy_in_force", in_force)
    return locked


async def test_a_run_without_drafts_reads_no_taxonomy(taxonomy: list[ConfigurationKind]) -> None:
    """Only drafts are checked against the taxonomy in force."""
    backend = LedgerDryRuns(cast(AsyncEngine, _Engine()), cast(DBOSClient, _Workflows()))
    await backend.start(DryRunRequest(run_id=uuid4()))
    assert taxonomy == []


async def test_a_draft_is_checked_under_the_taxonomy_lock(
    taxonomy: list[ConfigurationKind],
) -> None:
    """The taxonomy in force is read under its shared lock before the enqueue."""
    workflows = _Workflows()
    backend = LedgerDryRuns(cast(AsyncEngine, _Engine()), cast(DBOSClient, workflows))
    await backend.start(DryRunRequest(run_id=uuid4(), templates=SEED_TEMPLATES))
    assert taxonomy == [ConfigurationKind.TAXONOMY]
    assert len(workflows.enqueued) == 1


@pytest.mark.parametrize(
    ("when", "message"),
    [
        ({"category": "Fotos"}, "category 'Fotos'"),
        ({"category": "Documents", "subcategory": "Camera"}, "subcategory 'Camera'"),
    ],
)
async def test_a_draft_outside_the_taxonomy_is_refused_before_the_enqueue(
    when: dict[str, str], message: str
) -> None:
    """A category or subcategory the taxonomy in force does not list is never enqueued."""
    workflows = _Workflows()
    backend = LedgerDryRuns(cast(AsyncEngine, _Engine()), cast(DBOSClient, workflows))
    rule = {"name": "x", "when": when, "destinations": ["library"]}
    policy = parse_policy(_DRAFT_POLICY | {"rules": [rule]})
    request = DryRunRequest(run_id=uuid4(), policy=policy)
    with pytest.raises(RejectedError, match=message):
        await backend.start(request)
    assert workflows.enqueued == []


async def test_an_unreadable_taxonomy_in_force_refuses_a_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A draft cannot be checked against a taxonomy that does not read, so none starts."""

    async def unreadable(_configurations: object) -> Taxonomy:
        """Fail as a stored taxonomy that no longer reads."""
        raise ConfigurationError(ConfigurationKind.TAXONOMY, ["categories: Field required"])

    monkeypatch.setattr(module, "taxonomy_in_force", unreadable)
    workflows = _Workflows()
    backend = LedgerDryRuns(cast(AsyncEngine, _Engine()), cast(DBOSClient, workflows))
    request = DryRunRequest(run_id=uuid4(), templates=SEED_TEMPLATES)
    with pytest.raises(RejectedError, match="categories"):
        await backend.start(request)
    assert workflows.enqueued == []


async def test_valid_drafts_are_enqueued_with_the_request() -> None:
    """The drafts travel in the workflow input."""
    workflows = _Workflows()
    backend = LedgerDryRuns(cast(AsyncEngine, _Engine()), cast(DBOSClient, workflows))
    request = DryRunRequest(
        run_id=uuid4(), policy=parse_policy(_DRAFT_POLICY), templates=SEED_TEMPLATES
    )
    await backend.start(request)
    [(_, enqueued)] = workflows.enqueued
    assert enqueued is request


async def test_a_run_waiting_to_start_is_queued(no_headers: None) -> None:
    """Enqueued but without a header yet."""
    run_id = uuid4()
    backend = LedgerDryRuns(cast(AsyncEngine, _Engine()), cast(DBOSClient, _Workflows()))
    workflows = _Workflows(existing=[await backend.start(DryRunRequest(run_id=run_id))])
    backend = LedgerDryRuns(cast(AsyncEngine, _Engine()), cast(DBOSClient, workflows))
    status = await backend.status(run_id)
    assert (status.state, status.started_at) == (RunState.QUEUED, None)


async def test_a_run_without_a_header_has_no_drafts(no_headers: None) -> None:
    """Nothing recorded, nothing to export."""
    backend = LedgerDryRuns(cast(AsyncEngine, _Engine()), cast(DBOSClient, _Workflows()))
    drafts = await backend.drafts(uuid4())
    assert (drafts.policy, drafts.templates) == (None, None)


async def test_a_run_that_was_never_started_is_not_found(no_headers: None) -> None:
    """Neither a header nor a workflow."""
    backend = LedgerDryRuns(cast(AsyncEngine, _Engine()), cast(DBOSClient, _Workflows()))
    with pytest.raises(NotFoundError, match="no dry run"):
        await backend.status(uuid4())
