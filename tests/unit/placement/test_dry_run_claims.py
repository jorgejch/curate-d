"""The locator a dry run proposes, given the ledger and what the run already proposed.

The ledger and the run's recorded items are replaced by dictionaries, so only the claim
policy is under test.
"""

from typing import ClassVar, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection

from curate_d.domain import (
    SEED_POLICY,
    ContentObject,
    ContentObjectId,
    DestinationConfig,
    DestinationId,
    HashAlgorithm,
    Placement,
    Provider,
    StrongFingerprint,
)
from curate_d.placement import dry_run
from curate_d.placement.dry_run import Evaluator
from curate_d.rendering.collision import disambiguate
from curate_d.rendering.planning import PlacementPlan
from curate_d.rendering.render import Rendered
from curate_d.rendering.templates import SEED_TEMPLATES

_DESTINATION = DestinationConfig(
    name="library", provider=Provider.PCLOUD, account="acct-1", root_path="/library"
)
# The same name, root and template in another account: another destination.
_ELSEWHERE = _DESTINATION.model_copy(update={"name": "mirror", "account": "acct-2"})
_LOCATOR = "/library/Photos/2024/2024-05/IMG_0001.JPG"
_RUN = uuid4()


def _content(digit: str) -> ContentObject:
    """A content object whose fingerprint is `digit` repeated."""
    return ContentObject.build(
        fingerprint=StrongFingerprint(algorithm=HashAlgorithm.SHA256, digest=digit * 64),
        size_bytes=10,
        media_type="image/jpeg",
    )


def _plan(
    content: ContentObject, *, locator: str = _LOCATOR, present: bool = False
) -> PlacementPlan:
    """A plan as `plan_at` returns it."""
    return PlacementPlan(
        content_object_id=content.id,
        destination_name=_DESTINATION.name,
        rendered=Rendered(locator=_LOCATOR, template_name="photos", template_version="seed-1"),
        locator=locator,
        already_present=present,
    )


class _Ledger:
    """Placements by destination identity and locator."""

    held: ClassVar[dict[tuple[DestinationId, str], Placement]] = {}

    def __init__(self, connection: object) -> None:
        """Ignore the connection."""

    async def find_at_locator(
        self, *, destination_id: DestinationId, locator: str
    ) -> Placement | None:
        """Return what the ledger has at `locator` of the destination."""
        return self.held.get((destination_id, locator))


class _Store:
    """Earlier pages' proposals by destination identity and locator."""

    proposed: ClassVar[dict[tuple[DestinationId, str], ContentObjectId]] = {}

    def __init__(self, connection: object) -> None:
        """Ignore the connection."""

    async def proposed_for(
        self, run_id: UUID, destination_id: DestinationId, locator: str
    ) -> ContentObjectId | None:
        """Return the content an earlier page proposed `locator` for at the destination."""
        return self.proposed.get((destination_id, locator))


@pytest.fixture
def evaluator(monkeypatch: pytest.MonkeyPatch) -> Evaluator:
    """An evaluator over an empty ledger and a run with no earlier pages."""
    monkeypatch.setattr(_Ledger, "held", {})
    monkeypatch.setattr(_Store, "proposed", {})
    monkeypatch.setattr(dry_run, "PlacementRepository", _Ledger)
    monkeypatch.setattr(dry_run, "DryRunStore", _Store)
    return Evaluator(
        cast(AsyncConnection, None), run_id=_RUN, templates=SEED_TEMPLATES, policy=SEED_POLICY
    )


def _held(content: ContentObject, locator: str = _LOCATOR) -> Placement:
    """A placement of `content` at `locator`."""
    return Placement.build(
        content_object_id=content.id,
        destination_id=_DESTINATION.id,
        destination_name=_DESTINATION.name,
        locator=locator,
        template_version="seed-1",
    )


async def test_a_free_locator_is_proposed_as_planned(evaluator: Evaluator) -> None:
    """Nothing there and nothing proposed."""
    content = _content("a")
    assert await evaluator._claim(_plan(content), content, _DESTINATION) == (_LOCATOR, False)


async def test_content_already_there_is_reported_present(evaluator: Evaluator) -> None:
    """The plan found it; nothing is proposed."""
    content = _content("a")
    assert await evaluator._claim(_plan(content, present=True), content, _DESTINATION) == (
        _LOCATOR,
        True,
    )


async def test_a_locator_proposed_on_this_page_is_taken(evaluator: Evaluator) -> None:
    """The second content on one page is proposed its suffixed locator."""
    first, second = _content("a"), _content("b")
    await evaluator._claim(_plan(first), first, _DESTINATION)
    claimed = await evaluator._claim(_plan(second), second, _DESTINATION)
    assert claimed == (disambiguate(_LOCATOR, second.fingerprint), False)


async def test_a_locator_an_earlier_page_proposed_is_taken(evaluator: Evaluator) -> None:
    """Proposals recorded by earlier pages count as well."""
    content = _content("b")
    _Store.proposed[_DESTINATION.id, _LOCATOR] = ContentObjectId(uuid4())
    claimed = await evaluator._claim(_plan(content), content, _DESTINATION)
    assert claimed == (disambiguate(_LOCATOR, content.fingerprint), False)


async def test_a_suffixed_locator_the_content_already_holds_is_present(
    evaluator: Evaluator,
) -> None:
    """Pushed aside by a proposal, the content finds its own earlier copy."""
    content = _content("b")
    suffixed = disambiguate(_LOCATOR, content.fingerprint)
    _Store.proposed[_DESTINATION.id, _LOCATOR] = ContentObjectId(uuid4())
    _Ledger.held[_DESTINATION.id, suffixed] = _held(content, suffixed)
    assert await evaluator._claim(_plan(content), content, _DESTINATION) == (suffixed, True)


async def test_the_contents_own_proposal_does_not_push_it_aside(evaluator: Evaluator) -> None:
    """Evaluating the same content again keeps its locator."""
    content = _content("a")
    _Store.proposed[_DESTINATION.id, _LOCATOR] = content.id
    assert await evaluator._claim(_plan(content), content, _DESTINATION) == (_LOCATOR, False)


async def test_one_template_in_two_accounts_proposes_the_plain_locator_at_both(
    evaluator: Evaluator,
) -> None:
    """A locator taken at one destination identity is free at another, whatever the names."""
    first, second = _content("a"), _content("b")
    assert await evaluator._claim(_plan(first), first, _DESTINATION) == (_LOCATOR, False)
    assert await evaluator._claim(_plan(second), second, _ELSEWHERE) == (_LOCATOR, False)


async def test_a_ledger_placement_at_another_identity_takes_nothing(evaluator: Evaluator) -> None:
    """What another account holds under the same path is not in the way."""
    content = _content("b")
    _Ledger.held[_ELSEWHERE.id, _LOCATOR] = _held(_content("a"))
    assert await evaluator._claim(_plan(content), content, _DESTINATION) == (_LOCATOR, False)
