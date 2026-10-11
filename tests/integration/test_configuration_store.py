"""Versioned configuration against a real database, inside a rolled-back transaction."""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine

from curate_d.configuration.documents import parse_taxonomy
from curate_d.configuration.store import ConfigurationError, ConfigurationRepository
from curate_d.domain import SEED_TAXONOMY, ConfigurationKind, TaxonomyField
from curate_d.ledger.tables import configurations

pytestmark = pytest.mark.integration

_KIND = ConfigurationKind.TAXONOMY


@pytest.fixture
async def repository(ledger_engine: AsyncEngine) -> AsyncIterator[ConfigurationRepository]:
    """A repository over an empty taxonomy history, rolled back afterwards."""
    async with ledger_engine.connect() as connection:
        transaction = await connection.begin()
        try:
            await connection.execute(delete(configurations).where(configurations.c.kind == _KIND))
            yield ConfigurationRepository(connection)
        finally:
            await transaction.rollback()


async def test_the_seed_is_stored_once_as_version_one(
    repository: ConfigurationRepository,
) -> None:
    """Ensuring twice leaves one version."""
    assert await repository.ensure(_KIND, SEED_TAXONOMY, parse_taxonomy, created_by="t") == 1
    assert await repository.ensure(_KIND, SEED_TAXONOMY, parse_taxonomy, created_by="t") == 1
    active = await repository.active(_KIND)
    assert active is not None
    assert parse_taxonomy(active[1]) == SEED_TAXONOMY


async def test_a_valid_change_becomes_the_next_active_version(
    repository: ConfigurationRepository,
) -> None:
    """The new version is active and the old one kept in the history."""
    await repository.ensure(_KIND, SEED_TAXONOMY, parse_taxonomy, created_by="t")
    promoted = SEED_TAXONOMY.promote(TaxonomyField.CATEGORY, "Books")

    version = await repository.activate(
        _KIND, promoted.model_dump(mode="json"), parse_taxonomy, created_by="operator"
    )

    assert version == 2
    active = await repository.active(_KIND)
    assert active is not None
    assert parse_taxonomy(active[1]).accepts(TaxonomyField.CATEGORY, "Books")
    history = await repository.history(_KIND)
    assert [(v.version, v.active) for v in history] == [(2, True), (1, False)]
    assert history[0].created_by == "operator"


async def test_an_invalid_change_is_rejected_and_the_prior_version_stays(
    repository: ConfigurationRepository,
) -> None:
    """Nothing is written for a document that fails validation."""
    await repository.ensure(_KIND, SEED_TAXONOMY, parse_taxonomy, created_by="t")

    with pytest.raises(ConfigurationError, match="taxonomy"):
        await repository.activate(
            _KIND, {"categories": {"A/B": []}}, parse_taxonomy, created_by="operator"
        )

    active = await repository.active(_KIND)
    assert active is not None
    assert active[0] == 1
    assert len(await repository.history(_KIND)) == 1


async def test_nothing_is_active_before_anything_is_stored(
    repository: ConfigurationRepository,
) -> None:
    """A kind never configured reports no active version."""
    assert await repository.active(_KIND) is None


async def test_only_one_version_is_active_at_a_time(
    repository: ConfigurationRepository,
) -> None:
    """Activation deactivates the previous version in the same transaction."""
    for label in ("A", "B", "C"):
        await repository.activate(
            _KIND,
            SEED_TAXONOMY.promote(TaxonomyField.PLACE_LABEL, label).model_dump(mode="json"),
            parse_taxonomy,
            created_by="t",
        )
    assert [v.version for v in await repository.history(_KIND) if v.active] == [3]
