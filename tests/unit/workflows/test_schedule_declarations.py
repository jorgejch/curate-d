"""The persisted schedules the application declares, and the workflow names they start.

A stored schedule starts its workflow by name, and the deployment command writes that
name as a string the client cannot check against DBOS's registry. So the names are
pinned here: each must be the name its workflow registers under, and the name stored
rows already carry, or firing history and in-flight workflows lose their workflow.
"""

import pytest

from curate_d.config import Settings
from curate_d.runtime.queues import QueueName
from curate_d.workflows import (
    classify,
    connections,
    grouping,
    reconcile,
    rescan,
    retention,
    scan,
)
from curate_d.workflows.registry import declared_schedule_names, declared_schedules

# Schedule name -> the workflow name stored rows carry: DBOS's default name for each
# function (its __qualname__) when schedules were first written by workers.
_STORED_WORKFLOW_NAMES = {
    "connections-refresh": "refresh_connections",
    "scan-all": "scan_all",
    "full-rescan-all": "full_rescan_all",
    "classification-holds": "release_holds",
    "group-events": "group_events",
    "reconcile": "reconcile_all",
    "check-destinations": "check_destinations",
    "prune-workflow-history": "prune_history",
}

_WORKFLOWS: dict[str, object] = {
    connections.SCHEDULE_NAME: connections.refresh_connections,
    scan.SCHEDULE_NAME: scan.scan_all,
    rescan.SCHEDULE_NAME: rescan.full_rescan_all,
    classify.HOLDS_SCHEDULE_NAME: classify.release_holds,
    grouping.SCHEDULE_NAME: grouping.group_events,
    reconcile.SCHEDULE_NAME: reconcile.reconcile_all,
    reconcile.CHECK_SCHEDULE_NAME: reconcile.check_destinations,
    retention.SCHEDULE_NAME: retention.prune_history,
}


def test_every_declared_workflow_name_is_registered_and_unchanged() -> None:
    """The stored name, the declared name and DBOS's registered name are one string."""
    declared = {
        entry["schedule_name"]: entry["workflow_name"] for entry in declared_schedules(Settings())
    }
    assert declared == _STORED_WORKFLOW_NAMES
    registered = {name: vars(fn)["dbos_function_name"] for name, fn in _WORKFLOWS.items()}
    assert registered == declared


def test_no_two_declared_schedules_share_a_name() -> None:
    """DBOS upserts by name, so a duplicate would silently replace another pass."""
    names = [entry["schedule_name"] for entry in declared_schedules(Settings())]
    assert len(names) == len(set(names))
    assert declared_schedule_names(Settings()) == frozenset(names)


def test_every_schedule_runs_on_a_declared_queue_with_no_owner_of_its_own() -> None:
    """The client owns them all, and workers register every queue they name."""
    for entry in declared_schedules(Settings()):
        assert entry.get("queue_name") == QueueName.MAINTENANCE
        assert "application_name" not in entry
        assert entry["context"] is None


@pytest.mark.parametrize(
    ("setting", "schedule_name"),
    [
        ("scan_schedule", scan.SCHEDULE_NAME),
        ("full_rescan_schedule", rescan.SCHEDULE_NAME),
        ("reconcile_schedule", reconcile.SCHEDULE_NAME),
        ("destination_check_schedule", reconcile.CHECK_SCHEDULE_NAME),
        ("workflow_retention_schedule", retention.SCHEDULE_NAME),
    ],
)
def test_configured_crons_reach_their_declarations(setting: str, schedule_name: str) -> None:
    """A cron set in configuration is the one the deployment command writes."""
    settings = Settings().model_copy(update={setting: "7 7 * * *"})
    [entry] = [e for e in declared_schedules(settings) if e["schedule_name"] == schedule_name]
    assert entry["schedule"] == "7 7 * * *"
