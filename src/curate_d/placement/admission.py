"""Size-class routing and a byte budget for concurrent transfers.

Transfers are routed to `transfer-small` or `transfer-large` by size, so a run of large
files never queues in front of small ones. Within a worker, each class draws from its own
share of the scratch budget: two large transfers whose combined size would exceed the
large share are not run together — the second is deferred until the first finishes — and
because small transfers draw from a separate share, deferring large ones never holds a
small one back.

A transfer larger than its whole share can never be admitted; that is an explicit
resource error rather than an indefinite wait.
"""

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from enum import StrEnum

from curate_d.runtime.queues import QueueName


class SizeClass(StrEnum):
    """Which transfer queue and budget share a file belongs to."""

    SMALL = "small"
    LARGE = "large"

    @property
    def queue(self) -> QueueName:
        """The queue transfers of this class are enqueued on."""
        return QueueName.TRANSFER_SMALL if self is SizeClass.SMALL else QueueName.TRANSFER_LARGE


class AdmissionDeferredError(RuntimeError):
    """The transfer does not fit alongside those in flight; try again shortly."""


class TransferTooLargeError(RuntimeError):
    """The transfer exceeds its class's whole share; no amount of waiting admits it."""


def size_class(size_bytes: int, *, large_threshold: int) -> SizeClass:
    """The class of a transfer of `size_bytes`."""
    return SizeClass.LARGE if size_bytes >= large_threshold else SizeClass.SMALL


class TransferAdmission:
    """A per-process byte budget, split between the two size classes."""

    def __init__(
        self, budget_bytes: int, *, large_threshold: int, small_share: float = 0.25
    ) -> None:
        """Split `budget_bytes` into the small and large shares.

        Raises:
            ValueError: a non-positive budget, or a share outside (0, 1).
        """
        if budget_bytes <= 0 or not 0 < small_share < 1:
            raise ValueError("the budget must be positive and the small share within (0, 1)")
        small = int(budget_bytes * small_share)
        self._shares = {SizeClass.SMALL: small, SizeClass.LARGE: budget_bytes - small}
        self._in_flight = dict.fromkeys(SizeClass, 0)
        self._threshold = large_threshold
        self._guard = threading.Lock()

    def in_flight(self, size_class: SizeClass) -> int:
        """Bytes currently admitted in a class."""
        with self._guard:
            return self._in_flight[size_class]

    @contextmanager
    def admit(self, size_bytes: int) -> Iterator[SizeClass]:
        """Hold `size_bytes` of the file's class share for the duration of the block.

        Raises:
            TransferTooLargeError: the file exceeds its class's whole share.
            AdmissionDeferredError: it does not fit beside what is in flight now.
        """
        cls = size_class(size_bytes, large_threshold=self._threshold)
        share = self._shares[cls]
        with self._guard:
            if size_bytes > share:
                raise TransferTooLargeError(
                    f"a {size_bytes}-byte transfer exceeds the whole {cls.value} share of "
                    f"{share} bytes; raise the transfer scratch budget"
                )
            if self._in_flight[cls] + size_bytes > share:
                raise AdmissionDeferredError(
                    f"{self._in_flight[cls]} of {share} {cls.value}-class bytes are in flight"
                )
            self._in_flight[cls] += size_bytes
        try:
            yield cls
        finally:
            with self._guard:
                self._in_flight[cls] -= size_bytes
