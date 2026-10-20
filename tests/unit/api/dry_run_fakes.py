"""An in-memory dry-run backend, for the isolated endpoint tests."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from uuid import UUID

from curate_d.api.dry_runs import DryRunStatus
from curate_d.api.errors import NotFoundError
from curate_d.placement.dry_run import DryRunItem, DryRunRequest


@dataclass
class FakeDryRuns:
    """Runs by id; records what was started."""

    runs: dict[UUID, tuple[DryRunStatus, list[DryRunItem]]] = field(default_factory=dict)
    started: list[DryRunRequest] = field(default_factory=list)

    async def start(self, request: DryRunRequest) -> str:
        """Record the request."""
        self.started.append(request)
        return f"dry-run:{request.run_id}"

    async def status(self, run_id: UUID) -> DryRunStatus:
        """The run's status, or not found."""
        if run_id not in self.runs:
            raise NotFoundError("dry run", run_id)
        return self.runs[run_id][0]

    async def items(self, run_id: UUID) -> Sequence[DryRunItem]:
        """The run's items."""
        return self.runs[run_id][1] if run_id in self.runs else []
