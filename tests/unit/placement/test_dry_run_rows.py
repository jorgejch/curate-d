"""Dry-run items survive their row, and rclone clients are kept per event loop."""

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

from curate_d.domain import (
    ContentObjectId,
    DestinationIdentity,
    DryRunOutcome,
    Facts,
    MediaKind,
    Provider,
    RejectedValue,
    TaxonomyField,
    VerificationLevel,
)
from curate_d.placement.dry_run import DryRunItem, _item_values, _to_item
from curate_d.placement.remotes import RcloneAccess, remote_name


def test_a_proposed_item_round_trips_through_its_row() -> None:
    """Facts, level and paths come back as they went in."""
    item = DryRunItem(
        run_id=uuid4(),
        content_object_id=ContentObjectId(uuid4()),
        destination_name="lib",
        destination_id=DestinationIdentity(
            provider=Provider.PCLOUD, account="acct-1", root_path="/lib"
        ).id,
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
