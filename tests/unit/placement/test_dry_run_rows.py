"""Dry-run headers and items survive their rows, rules select what is evaluated, and rclone
clients are kept per event loop."""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncConnection

from curate_d.domain import (
    ContentObject,
    ContentObjectId,
    DestinationConfig,
    DryRunOutcome,
    Facts,
    HashAlgorithm,
    MediaKind,
    PlacementPolicy,
    PlacementRule,
    PolicySource,
    Provider,
    RejectedValue,
    StrongFingerprint,
    TaxonomyField,
    VerificationLevel,
)
from curate_d.placement import dry_run
from curate_d.placement.dry_run import (
    NO_DESTINATION,
    DryRun,
    DryRunItem,
    DryRunRequest,
    Evaluator,
    SelectingRule,
    _item_values,
    _run_values,
    _to_item,
    _to_run,
    selections,
)
from curate_d.placement.remotes import RcloneAccess, remote_name
from curate_d.rendering.planning import RenderFailure
from curate_d.rendering.templates import SEED_TEMPLATES


def test_a_proposed_item_round_trips_through_its_row() -> None:
    """Facts, level and paths come back as they went in."""
    item = DryRunItem(
        run_id=uuid4(),
        content_object_id=ContentObjectId(uuid4()),
        destination_name="lib",
        outcome=DryRunOutcome.PROPOSED,
        paths=("/a/x.jpg", "/b/x.jpg"),
        facts=Facts(
            media_kind=MediaKind.PHOTO,
            category="Photos",
            captured_at=datetime(2024, 5, 1, 7, 30, tzinfo=UTC),
        ),
        locator="/lib/Photos/2024/2024-05/x.jpg",
        expected_level=VerificationLevel.STRONG_COMMON_HASH,
    )
    assert _to_item(_item_values(item)) == item


def test_an_unresolved_item_keeps_its_reason_and_rejected_values() -> None:
    """What the taxonomy refused is exported for refinement."""
    item = DryRunItem(
        run_id=uuid4(),
        content_object_id=ContentObjectId(uuid4()),
        outcome=DryRunOutcome.UNCLASSIFIED,
        reason="awaiting review (rejected_value): Pets",
        rejected_values=(RejectedValue(field=TaxonomyField.CATEGORY, value="Pets"),),
    )
    assert _to_item(_item_values(item)) == item


def test_an_item_keeps_the_rules_that_selected_it() -> None:
    """Each rule comes back with its index and name, in the order they were recorded."""
    item = DryRunItem(
        run_id=uuid4(),
        content_object_id=ContentObjectId(uuid4()),
        destination_name="lib",
        outcome=DryRunOutcome.PROPOSED,
        rules=(SelectingRule(index=0, name="everything"), SelectingRule(index=2, name="photos")),
    )
    assert _item_values(item)["rules"] == [
        {"index": 0, "name": "everything"},
        {"index": 2, "name": "photos"},
    ]
    assert _to_item(_item_values(item)) == item


def test_an_unselected_item_round_trips_without_a_destination() -> None:
    """Its facts come back, and it names no destination and no rules."""
    item = DryRunItem(
        run_id=uuid4(),
        content_object_id=ContentObjectId(uuid4()),
        outcome=DryRunOutcome.UNSELECTED,
        paths=("/a/x.pdf",),
        facts=Facts(media_kind=MediaKind.DOCUMENT, category="Receipts"),
        reason="no placement rule selects it",
    )
    assert _item_values(item)["outcome"] == "unselected"
    assert _to_item(_item_values(item)) == item


def test_a_header_round_trips_through_its_row() -> None:
    """The policy source, version, rules and draft documents come back as they went in."""
    header = DryRun(
        id=uuid4(),
        started_at=datetime(2026, 10, 11, 8, tzinfo=UTC),
        template_version="draft-2",
        taxonomy_version=3,
        policy_source=PolicySource.DRAFT,
        policy_rules=("photos",),
        templates_draft=True,
        policy_document={"destinations": [], "rules": [], "retention": []},
        templates_document={"version": "draft-2", "templates": []},
    )
    row = _run_values(header)
    assert row["policy_source"] == "draft"
    assert _to_run(row) == header


def test_an_active_header_keeps_its_policy_version() -> None:
    """The version in force comes back with the active source."""
    header = DryRun(
        id=uuid4(),
        started_at=datetime(2026, 10, 11, 8, tzinfo=UTC),
        template_version="seed-1",
        taxonomy_version=1,
        policy_source=PolicySource.ACTIVE,
        policy_version=4,
        policy_rules=("photos", "documents"),
    )
    assert _to_run(_run_values(header)) == header


def test_a_header_recorded_before_policy_sources_reads_back_without_one() -> None:
    """Older rows have no source, no rules and no drafts."""
    header = DryRun(
        id=uuid4(),
        started_at=datetime(2026, 9, 30, 8, tzinfo=UTC),
        template_version="seed-1",
        taxonomy_version=1,
    )
    row = _run_values(header) | {"policy_source": None, "policy_rules": None}
    assert _to_run(row) == header


_LIBRARY = DestinationConfig(name="lib", provider=Provider.PCLOUD, account="1", root_path="/Org")


def test_a_request_refuses_a_draft_policy_with_named_destinations() -> None:
    """Named destinations ignore the rules a draft would be tried for."""
    policy = PlacementPolicy(destinations=(_LIBRARY,))
    with pytest.raises(ValidationError, match="cannot be combined"):
        DryRunRequest(run_id=uuid4(), destinations=(_LIBRARY,), policy=policy)


_FACTS = Facts(media_kind=MediaKind.PHOTO, category="Photos")
_PHOTOS_TO_LIBRARY = PlacementPolicy(
    destinations=(_LIBRARY,),
    rules=(
        PlacementRule(name="documents", when={"media_kind": "document"}, destinations=("lib",)),
        PlacementRule(name="photos", when={"media_kind": "photo"}, destinations=("lib",)),
    ),
)


@dataclass
class _Classified:
    """A classification result, as far as the evaluator reads it."""

    facts: Facts


class _Ledger:
    """Every repository the evaluator reads: one classified content object, no review."""

    reads_classification = False
    content = ContentObject.build(
        fingerprint=StrongFingerprint(algorithm=HashAlgorithm.SHA256, digest="a" * 64),
        size_bytes=10,
        media_type="image/jpeg",
    )

    def __init__(self, connection: object) -> None:
        """Ignore the connection."""

    async def find_by_content_object(self, content_object_id: ContentObjectId) -> list[object]:
        """Return no source objects."""
        return []

    async def excluded(self, content_object_id: ContentObjectId) -> bool:
        """Return that nothing is excluded."""
        return False

    async def open_for(self, content_object_id: ContentObjectId) -> None:
        """Return that nothing awaits review."""

    async def get(self, content_object_id: ContentObjectId) -> object:
        """Return the classification or the content object; both are read through `get`."""
        return _Classified(_FACTS) if self.reads_classification else self.content


class _Classifications(_Ledger):
    """The classification repository."""

    reads_classification = True


async def _render_failed(*args: object, destination: DestinationConfig, **kwargs: object) -> object:
    """Fail every render, so an item is written without claiming a locator."""
    return RenderFailure(
        content_object_id=_Ledger.content.id, destination_name=destination.name, reason="no"
    )


@pytest.fixture
def ledger(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the ledger the evaluator reads and the renderer it plans with."""
    monkeypatch.setattr(dry_run, "SourceObjectRepository", _Ledger)
    monkeypatch.setattr(dry_run, "ReviewRepository", _Ledger)
    monkeypatch.setattr(dry_run, "ClassificationRepository", _Classifications)
    monkeypatch.setattr(dry_run, "ContentObjectRepository", _Ledger)
    monkeypatch.setattr(dry_run, "plan_at", _render_failed)


def _evaluator(
    policy: PlacementPolicy, destinations: tuple[DestinationConfig, ...] = ()
) -> Evaluator:
    """Return an evaluator over the fake ledger."""
    return Evaluator(
        cast(AsyncConnection, None),
        run_id=uuid4(),
        templates=SEED_TEMPLATES,
        policy=policy,
        destinations=destinations,
    )


@pytest.mark.usefixtures("ledger")
async def test_content_no_rule_selects_is_one_unselected_item() -> None:
    """No destination, no rules, and the facts it was classified with."""
    policy = PlacementPolicy(
        destinations=(_LIBRARY,),
        rules=(PlacementRule(name="docs", when={"media_kind": "document"}, destinations=("lib",)),),
    )
    [item] = await _evaluator(policy).evaluate(_Ledger.content.id)
    assert item.outcome is DryRunOutcome.UNSELECTED
    assert (item.destination_name, item.rules, item.locator) == (NO_DESTINATION, (), None)
    assert item.facts == _FACTS


@pytest.mark.usefixtures("ledger")
async def test_a_selected_destination_names_its_rules() -> None:
    """Only the matching rule is recorded, and nothing is unselected."""
    [item] = await _evaluator(_PHOTOS_TO_LIBRARY).evaluate(_Ledger.content.id)
    assert (item.destination_name, item.rules) == ("lib", (SelectingRule(index=1, name="photos"),))
    assert item.outcome is DryRunOutcome.RENDER_FAILED


_TWO_COVERAGE_RULES = PlacementPolicy(
    destinations=(_LIBRARY,),
    rules=(
        PlacementRule(name="coverage", when={"media_kind": "photo"}, destinations=("lib",)),
        PlacementRule(name="coverage", when={"media_kind": "document"}, destinations=("lib",)),
        PlacementRule(name="coverage", when={"category": "Photos"}, destinations=("lib", "lib")),
    ),
)


@pytest.mark.usefixtures("ledger")
async def test_rules_sharing_a_name_are_recorded_apart() -> None:
    """Each matching rule is named by its index, and one listing a destination twice once."""
    [item] = await _evaluator(_TWO_COVERAGE_RULES).evaluate(_Ledger.content.id)
    assert item.rules == (
        SelectingRule(index=0, name="coverage"),
        SelectingRule(index=2, name="coverage"),
    )


@pytest.mark.parametrize(
    "facts",
    [
        _FACTS,
        Facts(media_kind=MediaKind.DOCUMENT, category="Receipts"),
        Facts(media_kind=MediaKind.VIDEO),
    ],
)
def test_selections_select_the_destinations_the_policy_does(facts: Facts) -> None:
    """The destinations and rule names agree with the policy's own selections."""
    for policy in (_PHOTOS_TO_LIBRARY, _TWO_COVERAGE_RULES, PlacementPolicy()):
        assert {
            name: tuple(dict.fromkeys(rule.name for rule in rules))
            for name, rules in selections(policy, facts).items()
        } == policy.selections(facts)


@pytest.mark.usefixtures("ledger")
async def test_named_destinations_never_produce_an_unselected_item() -> None:
    """The rules are bypassed: each named destination is evaluated, with no rules."""
    [item] = await _evaluator(PlacementPolicy(), (_LIBRARY,)).evaluate(_Ledger.content.id)
    assert (item.destination_name, item.rules) == ("lib", ())
    assert item.outcome is not DryRunOutcome.UNSELECTED


async def test_each_event_loop_gets_its_own_rclone_client() -> None:
    """One HTTP client per loop; the same one on the same loop."""
    access = RcloneAccess("http://rclone:5572")
    here = access.client()
    assert access.client()._http is here._http

    async def elsewhere() -> object:
        """A client made on another loop."""
        return access.client()._http

    other = await asyncio.to_thread(asyncio.run, elsewhere())
    assert other is not here._http
    assert remote_name(Provider.PCLOUD, "4242") == "pcloud-4242"
