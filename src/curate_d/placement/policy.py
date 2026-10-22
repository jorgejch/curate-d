"""The placement policy in force."""

from typing import Final

from pydantic import Field

from curate_d.configuration.documents import parse_policy, parse_stored_policy
from curate_d.configuration.store import ConfigurationRepository, check_document
from curate_d.domain import SEED_POLICY, ConfigurationKind, DomainModel, PlacementPolicy

SEEDED_BY: Final = "seed"


class ActivePolicy(DomainModel):
    """The active placement policy and its configuration version."""

    version: int = Field(ge=1)
    policy: PlacementPolicy


async def active_policy(configurations: ConfigurationRepository) -> ActivePolicy:
    """Return the active policy, storing the empty seed as version 1 if none was ever stored.

    Raises:
        ConfigurationError: the stored policy in force does not read; it names the kind and
            each problem.
    """
    await configurations.ensure(
        ConfigurationKind.POLICY, SEED_POLICY, parse_policy, created_by=SEEDED_BY
    )
    # Take the version and the document from one read so a concurrent activation cannot split them.
    stored = await configurations.active(ConfigurationKind.POLICY)
    assert stored is not None
    version, document = stored
    policy = check_document(ConfigurationKind.POLICY, document, parse_stored_policy)
    assert isinstance(policy, PlacementPolicy)
    return ActivePolicy(version=version, policy=policy)
