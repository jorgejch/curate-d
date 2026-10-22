"""The placement workflow's ledger transactions, against a faked datasource."""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from curate_d.domain import PlacementId
from curate_d.workflows import placement


@pytest.mark.parametrize("recorded", [True, False])
async def test_a_deferral_reports_whether_the_attempt_was_still_copying(
    monkeypatch: pytest.MonkeyPatch, recorded: bool
) -> None:
    """The deferral's result reaches the workflow, so a superseded wait ends it."""
    connection = object()
    seen: list[tuple[object, PlacementId, str, int | None]] = []

    async def session_connection() -> object:
        """Return the transaction's connection."""
        return connection

    async def defer(
        given: object, given_id: PlacementId, reason: str, *, attempt: int | None = None
    ) -> bool:
        """Record the call and report `recorded`."""
        seen.append((given, given_id, reason, attempt))
        return recorded

    datasource = SimpleNamespace(sql_session=lambda: SimpleNamespace(connection=session_connection))
    monkeypatch.setattr(placement, "current", lambda: SimpleNamespace(datasource=datasource))
    monkeypatch.setattr(placement, "defer", defer)
    placement_id = PlacementId(uuid4())

    assert await placement._defer_tx(placement_id, "scratch exhausted", 3) is recorded

    assert seen == [(connection, placement_id, "scratch exhausted", 3)]
