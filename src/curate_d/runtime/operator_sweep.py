"""Operator entry point: run the gated recovery sweep without launching DBOS.

For a scale-down after a hard kill, or whenever the scheduled sweep cannot run. It drives
DBOS through `DBOSClient` only: no startup recovery, no queue or scheduler threads, no
version registration. It writes nothing unless `--target-version` is both DBOS's latest
version and this build's own, and holds the target's standalone version lease for the run.

    python -m curate_d.runtime.operator_sweep --target-version <version>
"""

import sys
from collections.abc import Sequence
from typing import Final

EXIT_FAILED_ROWS: Final = 1
"""The sweep completed but reported at least one row as failed."""
EXIT_TARGET_MISMATCH: Final = 2
"""`--target-version` is missing or is not both DBOS's latest and this build's version."""


def main(argv: Sequence[str] | None = None) -> int:
    """Run one operator sweep and return the process exit status.

    Returns:
        0 when no row failed, `EXIT_FAILED_ROWS` when any did, `EXIT_TARGET_MISMATCH`
        when the target is refused before anything is written.
    """
    raise NotImplementedError


if __name__ == "__main__":
    sys.exit(main())
