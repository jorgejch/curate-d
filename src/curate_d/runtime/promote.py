"""Operator entry point: make an already-registered version DBOS's latest.

DBOS registers a version again without retiming it, so a build deployed a second time, by
rollback or by rolling forward to it, stays non-latest: schedule ticks and version-less
enqueues then go to a version nothing runs. Run this before the target build's workers
start, so they apply their schedules at startup. Workers never promote themselves.

    python -m curate_d.runtime.promote <version>
"""

import sys
from collections.abc import Sequence
from typing import Final

MAX_CLOCK_SKEW_SECONDS: Final = 1.0
"""DBOS stamps a promotion with the caller's clock, so a slow clock may promote too early."""


def main(argv: Sequence[str] | None = None) -> int:
    """Promote one version and return the process exit status.

    Returns:
        0 once the version reads back as latest; non-zero on any refusal or mismatch,
        having written nothing on a refusal.
    """
    raise NotImplementedError


if __name__ == "__main__":
    sys.exit(main())
