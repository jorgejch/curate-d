"""The committed staging evidence matrix names the S3 SDK the lock resolves."""

import re
from pathlib import Path

from curate_d.staging.sdk_evidence import (
    CHECKSUM_PROPERTY,
    CHECKSUM_TEST,
    CONTRACT_SUITE,
    check,
    read_locked_versions,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
LOCK = REPOSITORY_ROOT / "uv.lock"
MATRIX = REPOSITORY_ROOT / "ops/staging-compatibility.md"


def test_matrix_sdk_row_matches_the_lock() -> None:
    """The matrix's evidence was earned under the SDK versions uv.lock resolves."""
    drift = check(LOCK, MATRIX)

    assert drift == [], (
        "\n".join(drift) + "\nThe SDK moved: run `make staging-sdk-evidence` (RustFS) and "
        "record OVH, see ops/staging-compatibility.md, 'How to refresh this matrix'."
    )


def test_drift_check_names_a_moved_botocore(tmp_path: Path) -> None:
    """A copy of the lock with botocore edited fails the check, naming the package."""
    text = LOCK.read_text(encoding="utf-8")
    locked = read_locked_versions(text)["botocore"]
    edited = re.sub(rf'(name = "botocore"\nversion = "){re.escape(locked)}"', r'\g<1>0.0.1"', text)
    assert edited != text
    copy = tmp_path / "uv.lock"
    copy.write_text(edited, encoding="utf-8")

    assert check(copy, MATRIX) == [f"botocore: matrix {locked}, lock 0.0.1"]


def test_record_reads_the_property_the_contract_test_writes() -> None:
    """The checksum test still exists and records the JUnit property `record` reads."""
    source = (REPOSITORY_ROOT / CONTRACT_SUITE).read_text(encoding="utf-8")

    assert f"async def {CHECKSUM_TEST}(" in source
    assert f'record_property("{CHECKSUM_PROPERTY}",' in source
