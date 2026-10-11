"""Size-class routing and the transfer byte budget."""

import pytest

from curate_d.placement.admission import (
    AdmissionDeferredError,
    SizeClass,
    TransferAdmission,
    TransferTooLargeError,
    size_class,
)
from curate_d.runtime.queues import QueueName

_MB = 1024 * 1024


def _admission() -> TransferAdmission:
    """100 MB, a quarter for small transfers, large from 10 MB."""
    return TransferAdmission(100 * _MB, large_threshold=10 * _MB, small_share=0.25)


def test_files_are_routed_to_their_size_class_queue() -> None:
    """At or above the threshold is large."""
    assert size_class(10 * _MB, large_threshold=10 * _MB) is SizeClass.LARGE
    assert size_class(10 * _MB - 1, large_threshold=10 * _MB).queue is QueueName.TRANSFER_SMALL
    assert SizeClass.LARGE.queue is QueueName.TRANSFER_LARGE


def test_oversized_concurrent_large_transfers_are_deferred() -> None:
    """Two 40 MB files fit in the 75 MB large share one at a time, not together."""
    admission = _admission()
    with admission.admit(40 * _MB):
        with pytest.raises(AdmissionDeferredError), admission.admit(40 * _MB):
            pass
        assert admission.in_flight(SizeClass.LARGE) == 40 * _MB
    with admission.admit(40 * _MB) as cls:
        assert cls is SizeClass.LARGE


def test_small_transfers_are_not_starved_behind_large_ones() -> None:
    """With the large share full, small transfers still go through."""
    admission = _admission()
    with admission.admit(70 * _MB), admission.admit(5 * _MB) as cls:
        assert cls is SizeClass.SMALL
        assert admission.in_flight(SizeClass.SMALL) == 5 * _MB


def test_a_transfer_larger_than_its_share_is_an_explicit_error() -> None:
    """Waiting would never admit it."""
    with (
        pytest.raises(TransferTooLargeError, match="exceeds the whole large share"),
        _admission().admit(80 * _MB),
    ):
        pass


def test_the_budget_is_returned_even_when_the_transfer_fails() -> None:
    """A failed copy does not leak its reservation."""
    admission = _admission()
    with pytest.raises(RuntimeError), admission.admit(50 * _MB):
        raise RuntimeError("copy failed")
    assert admission.in_flight(SizeClass.LARGE) == 0


@pytest.mark.parametrize(("budget", "share"), [(0, 0.5), (10, 0.0), (10, 1.0)])
def test_an_unusable_budget_is_rejected(budget: int, share: float) -> None:
    """Configuration errors fail at construction."""
    with pytest.raises(ValueError):
        TransferAdmission(budget, large_threshold=1, small_share=share)
