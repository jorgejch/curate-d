"""The staging error hierarchy callers branch on."""

from curate_d.staging.errors import (
    StagingObjectStoreError,
    StagingReservationLostError,
    StagingTransientError,
)


def test_a_lost_reservation_is_a_retryable_object_store_failure() -> None:
    """A lost reservation is retried like any transient staging failure, never as capacity."""
    error = StagingReservationLostError("reservation expired before registration")
    assert isinstance(error, StagingTransientError)
    assert isinstance(error, StagingObjectStoreError)
