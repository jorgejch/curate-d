"""The placement policy in force."""

from typing import Final

from pydantic import Field

from curate_d.configuration.documents import parse_policy, parse_stored_policy
from curate_d.configuration.store import ConfigurationRepository
from curate_d.domain import SEED_POLICY, ConfigurationKind, DomainModel, PlacementPolicy

SEEDED_BY: Final = "seed"


class ActivePolicy(DomainModel):
    """The active placement policy and its configuration version."""

    version: int = Field(ge=1)
    policy: PlacementPolicy


async def active_policy(configurations: ConfigurationRepository) -> ActivePolicy:
    """The active policy, storing the empty seed as version 1 if none was ever stored."""
    version = await configurations.ensure(
        ConfigurationKind.POLICY, SEED_POLICY, parse_policy, created_by=SEEDED_BY
    )
    stored = await configurations.active(ConfigurationKind.POLICY)
    assert stored is not None
    return ActivePolicy(version=version, policy=parse_stored_policy(stored[1]))
