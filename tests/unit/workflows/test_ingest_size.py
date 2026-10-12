"""The size check ingest makes before reading a file: refuse what can never be staged."""

from curate_d.config import Settings
from curate_d.workflows.ingest import size_refusal


def _settings(**overrides: object) -> Settings:
    """Return default settings with `overrides` applied."""
    return Settings().model_copy(update=overrides)


def test_a_file_within_every_bound_is_not_refused() -> None:
    """Exactly at the tightest bound still fits."""
    settings = _settings(staging_max_bytes=100, staging_local_scratch_max_bytes=200)
    assert size_refusal(100, settings) is None


def test_the_reason_names_the_size_and_the_tightest_bound() -> None:
    """The failure reason says how large the file is and which setting refused it."""
    settings = _settings(staging_max_bytes=1000, staging_local_scratch_max_bytes=500)
    assert size_refusal(501, settings) == (
        "file is 501 bytes; the bound is 500 bytes (staging_local_scratch_max_bytes)"
    )


def test_the_per_file_ceiling_applies_when_set() -> None:
    """`ingest_max_bytes` refuses what the capacity bounds would admit."""
    settings = _settings(ingest_max_bytes=10)
    assert (
        size_refusal(11, settings) == "file is 11 bytes; the bound is 10 bytes (ingest_max_bytes)"
    )
    assert size_refusal(11, _settings()) is None


def test_staging_capacity_refuses_a_file_larger_than_staging() -> None:
    """With no per-file ceiling, the staged-bytes bound is still a ceiling."""
    settings = _settings(staging_max_bytes=64, staging_local_scratch_max_bytes=1024)
    reason = size_refusal(65, settings)
    assert reason is not None
    assert reason.endswith("(staging_max_bytes)")
