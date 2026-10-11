"""Worker profile selection."""

import pytest

from curate_d.runtime.profiles import (
    PROFILE_ENV,
    UnknownWorkerProfileError,
    WorkerProfile,
    resolve_profile,
)
from curate_d.runtime.queues import QueueName


def test_each_profile_dequeues_its_own_stages() -> None:
    """Assign IO-facing and CPU-facing queues to their respective profiles."""
    assert QueueName.SCAN in WorkerProfile.IO.queues
    assert QueueName.FINGERPRINT in WorkerProfile.CPU.queues


def test_the_io_profile_verifies_and_the_cpu_profile_does_not() -> None:
    """Dequeue verify on the I/O profile, which collects the digests it compares."""
    assert QueueName.VERIFY in WorkerProfile.IO.queues
    assert QueueName.VERIFY not in WorkerProfile.CPU.queues


def test_the_profiles_partition_the_topology() -> None:
    """An unlistened queue accumulates work with no error to show for it."""
    io_queues = set(WorkerProfile.IO.queues)
    cpu_queues = set(WorkerProfile.CPU.queues)
    assert io_queues | cpu_queues == set(QueueName)
    assert not io_queues & cpu_queues


def test_the_credential_holding_profile_does_not_decode_untrusted_content() -> None:
    """The reason the split exists: a decoder compromise must miss the credentials."""
    assert WorkerProfile.IO.holds_provider_credentials
    assert not WorkerProfile.IO.handles_untrusted_content
    assert WorkerProfile.CPU.handles_untrusted_content
    assert not WorkerProfile.CPU.holds_provider_credentials


@pytest.mark.parametrize(
    ("raw", "expected"), [("io", WorkerProfile.IO), ("CPU", WorkerProfile.CPU)]
)
def test_a_configured_profile_is_resolved_case_insensitively(
    raw: str, expected: WorkerProfile
) -> None:
    """Resolve a profile name regardless of its input casing."""
    assert resolve_profile(raw) == expected


def test_an_unset_profile_fails_rather_than_defaulting() -> None:
    """A wrong default drains no queue and raises no error; that is the worst outcome."""
    with pytest.raises(UnknownWorkerProfileError, match=PROFILE_ENV):
        resolve_profile("")


def test_an_unknown_profile_names_the_valid_ones() -> None:
    """List the valid profile names in the error for an unrecognised one."""
    with pytest.raises(UnknownWorkerProfileError, match="io, cpu"):
        resolve_profile("gpu")


def test_the_environment_is_read_when_no_value_is_passed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resolve the profile from the environment variable when no argument is given."""
    monkeypatch.setenv(PROFILE_ENV, "io")
    assert resolve_profile() is WorkerProfile.IO


def test_a_missing_environment_variable_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Raise when no profile is passed and the environment variable is unset."""
    monkeypatch.delenv(PROFILE_ENV, raising=False)
    with pytest.raises(UnknownWorkerProfileError):
        resolve_profile()
