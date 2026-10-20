"""Reconciliation's audit entries name the policy version they applied.

Content arrives through the real scan, ingest and classification workflows and is placed
through the local stack's rclone daemon, as in `test_reconcile_workflow`, whose fixtures
and helpers this module borrows.
"""

from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from curate_d.config import Settings
from curate_d.configuration.store import ConfigurationRepository
from curate_d.domain import (
    AuditEntryType,
    DestinationConfig,
    PlacementPolicy,
    PlacementRule,
    Provider,
    RetentionRule,
)
from curate_d.placement.policy import active_policy
from curate_d.placement.rclone import RcloneClient
from tests.integration.test_reconcile_workflow import (
    _DATED,
    Activate,
    _audit,
    _high_water,
    _ingest,
    _reconcile,
    activate,  # noqa: F401 - fixture
    daemon,  # noqa: F401 - fixture
    names,  # noqa: F401 - fixture
)
from tests.integration.transfer_harness import (
    DESTINATION_ACCOUNT,
    ScriptedAccess,
    WorkspaceRemotes,
    transfer_pipeline,
)

pytestmark = pytest.mark.integration


def _policy(library: str) -> PlacementPolicy:
    """Return a policy that keeps every original and sends photos to `library`."""
    return PlacementPolicy(
        destinations=(
            DestinationConfig(
                name=library,
                provider=Provider.PCLOUD,
                account=DESTINATION_ACCOUNT,
                root_path=f"/{library}",
            ),
        ),
        rules=(PlacementRule(name="photos", when={"category": "Photos"}, destinations=(library,)),),
        retention=(RetentionRule(name="keep"),),
    )


async def test_decisions_record_the_policy_version_they_applied(
    settings: Settings,
    ledger_engine: AsyncEngine,
    tmp_path: Path,
    daemon: tuple[RcloneClient, str],  # noqa: F811 - fixture
    activate: Activate,  # noqa: F811 - fixture
    names: tuple[str, str],  # noqa: F811 - fixture
) -> None:
    """The retention decision and the copy a run starts name the policy version in force."""
    library, _ = names
    await activate(_policy(library))
    async with ledger_engine.begin() as connection:
        version = (await active_policy(ConfigurationRepository(connection))).version
    access = ScriptedAccess()
    remotes = WorkspaceRemotes(daemon[1])
    async with transfer_pipeline(settings, ledger_engine, tmp_path, remotes, access) as pipeline:
        content_id = (await _ingest(pipeline, daemon))[_DATED]
        since = await _high_water(ledger_engine)

        await _reconcile(pipeline, content_id)

        [decided] = await _audit(ledger_engine, content_id, AuditEntryType.RETENTION_DECIDED, since)
        assert (decided.details["rule"], decided.details["policy_version"]) == ("keep", version)
        [started] = await _audit(ledger_engine, content_id, AuditEntryType.PLACEMENT_STARTED, since)
        assert started.details["policy_version"] == version
        assert started.details["destination_provider"] == Provider.PCLOUD.value
        assert started.details["destination_account"] == DESTINATION_ACCOUNT
        assert started.details["destination_root"] == f"/{library}"
