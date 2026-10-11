"""The event-grouping pass over photos scanned, ingested and classified for real."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from dbos import SetWorkflowID
from sqlalchemy.ext.asyncio import AsyncEngine

from curate_d.classification.store import ClassificationRepository
from curate_d.config import Settings
from curate_d.domain import Provider
from curate_d.workflows.grouping import group_events
from curate_d.workflows.scan import scan_root
from tests.integration.pipeline_harness import running_pipeline
from tests.stubs import media

pytestmark = pytest.mark.integration

_PARIS = (48.8566, 2.3522)
_LYON = (45.7640, 4.8357)


async def test_geotagged_photos_are_grouped_into_their_events(
    settings: Settings, ledger_engine: AsyncEngine, tmp_path: Path
) -> None:
    """Three Paris photos and three Lyon photos a week later are two events."""
    async with running_pipeline(settings, ledger_engine, tmp_path) as pipeline:
        start = datetime(2026, 7, 10, 10, 0)  # noqa: DTZ001 - EXIF is naive; offset given
        seed = int(pipeline.account)
        for index in range(3):
            pipeline.pcloud.file(
                f"/paris/IMG_{index}.JPG",
                media.photo(
                    taken=start + timedelta(hours=index),
                    offset="+01:00",
                    gps=_PARIS,
                    seed=seed + index,
                ),
            )
            pipeline.pcloud.file(
                f"/lyon/IMG_{index}.JPG",
                media.photo(
                    taken=start + timedelta(days=7, hours=index),
                    offset="+01:00",
                    gps=_LYON,
                    seed=seed + 10 + index,
                ),
            )
        cycle = uuid4().hex[:8]
        with SetWorkflowID(f"test-scan-{pipeline.account}-{cycle}"):
            await scan_root(Provider.PCLOUD.value, pipeline.account, "/", cycle)
        await pipeline.settle()

        with SetWorkflowID(f"test-group-{uuid4()}"):
            await group_events(datetime.now(UTC), None)

        groups: dict[str, set[str]] = {}
        async with ledger_engine.connect() as connection:
            repository = ClassificationRepository(connection)
            for source in await pipeline.sources():
                assert source.content_object_id is not None
                result = await repository.get(source.content_object_id)
                assert result is not None
                assert result.facts.event_group is not None
                groups.setdefault(result.facts.event_group, set()).add(source.path.split("/")[1])
        assert sorted(groups.values(), key=sorted) == [{"paris"}, {"lyon"}]
        assert any(g.startswith("2026-07-10-") for g in groups)
        assert any(g.startswith("2026-07-17-") for g in groups)
