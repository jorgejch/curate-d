"""SDK evidence check and record against a fixture matrix, lock and JUnit reports."""

import json
import subprocess
from collections.abc import Callable, Sequence
from datetime import date
from pathlib import Path
from xml.sax.saxutils import quoteattr

import pytest

from curate_d.staging import sdk_evidence
from curate_d.staging.sdk_evidence import (
    CHECKSUM_PROPERTY,
    CHECKSUM_TEST,
    EXIT_CHECK_FAILED,
    EXIT_NOT_RUN,
    EXIT_OK,
    Backend,
    EvidenceError,
    check,
    parse_matrix,
    read_locked_versions,
    split_row,
)

OLD = {"aioboto3": "15.5.0", "aiobotocore": "2.25.1", "botocore": "1.40.61"}
NEW = {"aioboto3": "15.5.1", "aiobotocore": "2.25.2", "botocore": "1.40.70"}
SERVER = (
    "rustfs/rustfs:1.0.0@sha256:8cc9801755448b71a786705ce76692c77e14936cccd87cf2fc31842e58f4d1ff"
)
RUSTFS_ID = "`rustfs/rustfs:1.0.0@sha256:8cc98017…`"
TODAY = date(2026, 11, 1)
HEADERS = {
    "content-encoding": "aws-chunked",
    "x-amz-content-sha256": "STREAMING-UNSIGNED-PAYLOAD-TRAILER",
    "x-amz-sdk-checksum-algorithm": "CRC32",
    "x-amz-trailer": "x-amz-checksum-crc32",
}
MATRIX_HEADER = "| # | Behavior | Check | RustFS | MinIO (superseded) | OVH | Design response |"
SEPARATOR = "| --- | --- | --- | --- | --- | --- | --- |"
PROSE = "Prose: the SDK's default checksum under `botocore` 1.40.61 is accepted."


def _sdk_row(versions: dict[str, str]) -> str:
    """Render a matrix SDK row naming `versions`."""
    return "| SDK | " + " / ".join(f"`{name}` {versions[name]}" for name in versions) + " |"


def _row(number: str, rustfs: str, ovh: str, design: str = "Design.") -> str:
    """Render one seven-column matrix row."""
    return (
        f"| {number} | Behavior {number} | `check_{number}` | {rustfs} | PASS | {ovh} | {design} |"
    )


def _matrix(sdk_row: str | None = None, extra_rows: Sequence[str] = ()) -> str:
    """Render a fixture matrix with a header table, one leading table and rows 32-35."""
    lines = [
        "# Matrix",
        "",
        "| Target | Identity |",
        "| --- | --- |",
        f"| RustFS server | `{SERVER}` (multi-arch index) |",
        "| RustFS endpoint | `http://127.0.0.1:9100`, path style, `s3v4`, region `us-east-1` |",
    ]
    if sdk_row is not None:
        lines.append(sdk_row)
    lines += [
        "",
        PROSE,
        "",
        MATRIX_HEADER,
        SEPARATOR,
        _row("31", "PASS — old", "PASS — old ovh"),
        "",
        MATRIX_HEADER,
        SEPARATOR,
        _row("32", "PASS — r32", "PASS — o32", "Default botocore 1.40.61 behavior."),
        _row("33", "PASS — r33", "PASS — o33"),
        _row("34", "PASS — r34", "PASS — o34"),
        _row("35", "PASS — r35", "PASS — o35"),
        *extra_rows,
        "",
        "Trailer line.",
        "",
    ]
    return "\n".join(lines)


def _lock(versions: dict[str, str], *, drop: str | None = None) -> str:
    """Render a minimal uv lock resolving `versions`, optionally without one package."""
    entries = [
        f'[[package]]\nname = "{name}"\nversion = "{version}"\n'
        for name, version in {**versions, "pydantic": "2.14.2"}.items()
        if name != drop
    ]
    return "version = 1\n\n" + "\n".join(entries)


def _write(tmp_path: Path, matrix: str, lock: str) -> tuple[Path, Path]:
    """Write the fixture matrix and lock, returning their paths."""
    matrix_path, lock_path = tmp_path / "matrix.md", tmp_path / "uv.lock"
    matrix_path.write_text(matrix, encoding="utf-8")
    lock_path.write_text(lock, encoding="utf-8")
    return matrix_path, lock_path


# --- check -----------------------------------------------------------------------------


def test_split_row_keeps_a_backtick_quoted_pipe_in_its_cell() -> None:
    """A `|` inside a code span or escaped does not split the cell."""
    assert split_row("| 32 | `a | b` | c \\| d |") == ("32", "`a | b`", "c \\| d")


def test_split_row_rejects_a_line_that_is_not_a_row() -> None:
    """A line without the table's outer pipes is refused."""
    with pytest.raises(EvidenceError, match="not a table row"):
        split_row("prose | with a pipe")


def test_check_agrees_when_lock_and_matrix_name_the_same_sdk(tmp_path: Path) -> None:
    """Lock and matrix agreeing is a pass that leaves the matrix untouched."""
    matrix_path, lock_path = _write(tmp_path, _matrix(_sdk_row(OLD)), _lock(OLD))
    before = matrix_path.read_bytes()

    assert check(lock_path, matrix_path) == []
    assert sdk_evidence.main(["check", "--lock", str(lock_path), "--matrix", str(matrix_path)]) == 0
    assert matrix_path.read_bytes() == before


def test_check_names_a_drifted_package_with_both_versions(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A botocore patch in the lock fails the check, naming both versions."""
    lock = _lock({**OLD, "botocore": "1.40.62"})
    matrix_path, lock_path = _write(tmp_path, _matrix(_sdk_row(OLD)), lock)

    assert check(lock_path, matrix_path) == ["botocore: matrix 1.40.61, lock 1.40.62"]
    status = sdk_evidence.main(["check", "--lock", str(lock_path), "--matrix", str(matrix_path)])
    assert status == EXIT_CHECK_FAILED
    error = capsys.readouterr().err
    assert "botocore: matrix 1.40.61, lock 1.40.62" in error
    assert "make staging-sdk-evidence" in error


def test_check_fails_naming_the_matrix_when_the_sdk_row_is_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No SDK row is a failure naming the file, never agreement."""
    matrix_path, lock_path = _write(tmp_path, _matrix(None), _lock(OLD))

    with pytest.raises(EvidenceError, match=r"matrix\.md: no `\| SDK \|` row"):
        check(lock_path, matrix_path)
    status = sdk_evidence.main(["check", "--lock", str(lock_path), "--matrix", str(matrix_path)])
    assert status == EXIT_CHECK_FAILED
    assert "matrix.md" in capsys.readouterr().err


def test_check_fails_when_the_sdk_row_omits_a_package(tmp_path: Path) -> None:
    """A partial SDK row names the file, its line and the package it lacks."""
    partial = "| SDK | `aioboto3` 15.5.0 / `botocore` 1.40.61 |"
    matrix_path, lock_path = _write(tmp_path, _matrix(partial), _lock(OLD))

    with pytest.raises(EvidenceError, match=r"matrix\.md:7: SDK row does not name aiobotocore"):
        check(lock_path, matrix_path)


def test_check_fails_when_the_lock_omits_a_package(tmp_path: Path) -> None:
    """A lock without an SDK package cannot be compared and fails."""
    matrix_path, lock_path = _write(tmp_path, _matrix(_sdk_row(OLD)), _lock(OLD, drop="botocore"))

    with pytest.raises(EvidenceError, match=r"uv\.lock: no locked version for botocore"):
        check(lock_path, matrix_path)


def test_check_fails_on_a_lock_that_is_not_toml() -> None:
    """An unparsable lock is reported with its path."""
    with pytest.raises(EvidenceError, match="not a TOML lock file"):
        read_locked_versions("[[package", source=Path("uv.lock"))


def test_check_fails_on_an_unreadable_file(tmp_path: Path) -> None:
    """A missing input is reported with its path."""
    lock_path = tmp_path / "uv.lock"
    lock_path.write_text(_lock(OLD), encoding="utf-8")

    with pytest.raises(EvidenceError, match=r"absent\.md"):
        check(lock_path, tmp_path / "absent.md")


def test_matrix_parser_keeps_a_cell_holding_a_backtick_quoted_pipe() -> None:
    """A row whose Check cell quotes a `|` keeps the table's cell count."""
    quoted = "| 36 | Behavior | `a | b` | PASS | PASS | PASS | Design. |"
    matrix = parse_matrix(_matrix(_sdk_row(OLD), [quoted]))

    assert matrix.rows["32"].cells[0] == "32"
    assert split_row(quoted)[2] == "`a | b`"


def test_matrix_parser_rejects_a_row_with_the_wrong_cell_count() -> None:
    """A row that does not match its header's column count is an error naming the line."""
    broken = "| 36 | Behavior | a | b | PASS | PASS | PASS | Design. |"

    with pytest.raises(EvidenceError, match=r"matrix\.md:21: row has 8 cells"):
        parse_matrix(_matrix(_sdk_row(OLD), [broken]), source=Path("matrix.md"))


def test_matrix_parser_requires_rows_32_to_35() -> None:
    """A matrix without the SDK-sensitive rows cannot be recorded into."""
    text = _matrix(_sdk_row(OLD)).replace(_row("34", "PASS — r34", "PASS — o34") + "\n", "")

    with pytest.raises(EvidenceError, match="no matrix table holds rows 32, 33, 34, 35"):
        parse_matrix(text)


def test_matrix_parser_requires_both_backend_columns() -> None:
    """A rows 32-35 table without an OVH column is refused."""
    text = _matrix(_sdk_row(OLD)).replace(
        MATRIX_HEADER, MATRIX_HEADER.replace("| OVH |", "| Other |")
    )

    with pytest.raises(EvidenceError, match="has no OVH column"):
        parse_matrix(text)


# --- record ----------------------------------------------------------------------------


def _case(name: str, *, outcome: str = "pass", headers: dict[str, str] | None = None) -> str:
    """Render one JUnit test case."""
    full = f"{name}[rustfs]"
    properties = ""
    if headers is not None:
        value = quoteattr(json.dumps(headers, sort_keys=True))
        properties = (
            f'<properties><property name="{CHECKSUM_PROPERTY}" value={value}/></properties>'
        )
    body = {"pass": "", "fail": '<failure message="boom"/>', "skip": '<skipped message="s"/>'}
    return (
        f'<testcase classname="tests.integration.test_staging_contract" name="{full}">'
        f"{properties}{body[outcome]}</testcase>"
    )


def _report(*cases: str) -> str:
    """Render a pytest JUnit report holding `cases`."""
    return (
        '<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest">'
        + "".join(cases)
        + "</testsuite></testsuites>"
    )


PASSING = _report(
    _case(CHECKSUM_TEST, headers=HEADERS), _case("test_listing_paginates"), _case("test_other")
)

Runner = Callable[[Backend, Path], int]


def _runner(reports: dict[Backend, str], status: int = 0) -> Runner:
    """Return a suite runner that writes the given report per backend."""

    def run(backend: Backend, report: Path) -> int:
        """Write the fixture report for `backend`, if any, and return `status`."""
        if backend in reports:
            report.write_text(reports[backend], encoding="utf-8")
        return status

    return run


def _record(
    tmp_path: Path,
    reports: dict[Backend, str],
    *,
    lock: dict[str, str] = OLD,
    matrix: str | None = None,
    status: int = 0,
    extra: Sequence[str] = (),
) -> tuple[int, Path, str]:
    """Run `record` on the fixture files; return its status, the matrix path and old text."""
    text = matrix if matrix is not None else _matrix(_sdk_row(OLD))
    matrix_path, lock_path = _write(tmp_path, text, _lock(lock))
    backends = [arg for backend in reports for arg in ("--backend", backend.value)]
    argv = [
        "record",
        *(backends or ["--backend", "rustfs"]),
        "--date",
        TODAY.isoformat(),
        "--lock",
        str(lock_path),
        "--matrix",
        str(matrix_path),
        *extra,
    ]
    return sdk_evidence.main(argv, runner=_runner(reports, status)), matrix_path, text


def _cells(text: str, number: str) -> tuple[str, str]:
    """Return a row's RustFS and OVH cells."""
    for line in text.split("\n"):
        if line.startswith(f"| {number} |"):
            cells = split_row(line)
            return cells[3], cells[5]
    raise AssertionError(f"row {number} missing")


def _unchanged_except(before: str, after: str, numbers: Sequence[str], sdk: bool) -> None:
    """Assert every line other than the named rows (and the SDK row) is byte-identical."""
    for old, new in zip(before.split("\n"), after.split("\n"), strict=True):
        rewritten = any(old.startswith(f"| {number} |") for number in numbers)
        if not rewritten and not (sdk and old.startswith("| SDK |")):
            assert new == old


def test_record_rewrites_the_run_backend_and_leaves_ovh_when_the_sdk_did_not_move(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A green RustFS run under an unchanged SDK rewrites only RustFS's cells."""
    status, matrix_path, before = _record(tmp_path, {Backend.RUSTFS: PASSING})
    after = matrix_path.read_text(encoding="utf-8")

    assert status == EXIT_OK
    headers = (
        "`content-encoding: aws-chunked`, "
        "`x-amz-content-sha256: STREAMING-UNSIGNED-PAYLOAD-TRAILER`, "
        "`x-amz-sdk-checksum-algorithm: CRC32`, `x-amz-trailer: x-amz-checksum-crc32`"
    )
    checksum = (
        f"PASS — 2026-11-01, {RUSTFS_ID}: `{CHECKSUM_TEST}` under botocore 1.40.61; "
        f"headers {headers}"
    )
    assert _cells(after, "32") == (checksum, "PASS — o32")
    assert _cells(after, "33") == (checksum, "PASS — o33")
    suite = (
        f"PASS — 2026-11-01, {RUSTFS_ID}: 3 tests in `tests/integration/test_staging_contract.py`"
    )
    assert _cells(after, "34") == (f"{suite} signed `s3v4`", "PASS — o34")
    assert _cells(after, "35") == (f"{suite} signed with region `us-east-1`", "PASS — o35")
    assert after.split("\n")[6] == _sdk_row(OLD)
    _unchanged_except(before, after, ["32", "33", "34", "35"], sdk=False)
    output = capsys.readouterr().out
    assert "rewrote 4 line(s)" in output
    assert "Review by hand" not in output


def test_record_marks_ovh_pending_and_lists_prose_when_the_sdk_moved(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A RustFS-only run after an SDK move leaves OVH pending with its old evidence."""
    status, matrix_path, before = _record(tmp_path, {Backend.RUSTFS: PASSING}, lock=NEW)
    after = matrix_path.read_text(encoding="utf-8")

    assert status == EXIT_OK
    assert after.split("\n")[6] == _sdk_row(NEW)
    rustfs, ovh = _cells(after, "32")
    assert "under botocore 1.40.70" in rustfs
    assert (
        ovh == "PENDING — 2026-11-01: SDK moved to botocore 1.40.70; previous evidence: PASS — o32"
    )
    assert _cells(after, "35")[1].endswith("previous evidence: PASS — o35")
    assert _cells(after, "31") == ("PASS — old", "PASS — old ovh")
    _unchanged_except(before, after, ["32", "33", "34", "35"], sdk=True)
    output = capsys.readouterr().out
    assert f"9: {PROSE}" in output
    assert "Default botocore 1.40.61 behavior." in output
    assert "previous evidence" not in output.split("Review by hand", 1)[1]


def test_record_does_not_nest_pending_evidence_across_two_moves(tmp_path: Path) -> None:
    """A second SDK move keeps the evidence earned under the last SDK that ran, once."""
    pending = "PENDING — 2026-10-01: SDK moved to botocore 1.40.65; previous evidence: PASS — o32"
    matrix = _matrix(_sdk_row(OLD)).replace("PASS — o32", pending)

    _, matrix_path, _ = _record(tmp_path, {Backend.RUSTFS: PASSING}, lock=NEW, matrix=matrix)

    assert _cells(matrix_path.read_text(encoding="utf-8"), "32")[1] == (
        "PENDING — 2026-11-01: SDK moved to botocore 1.40.70; previous evidence: PASS — o32"
    )


def test_record_both_backends_writes_each_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An OVH run records the bucket and endpoint the fixtures read, and its region."""
    monkeypatch.setenv("CURATE_D_TEST_OVH_BUCKET", "curate-d-test-contract")
    monkeypatch.setenv("CURATE_D_TEST_OVH_ENDPOINT", "https://s3.example.test")
    reports = {Backend.RUSTFS: PASSING, Backend.OVH: PASSING}

    status, matrix_path, _ = _record(tmp_path, reports, lock=NEW)

    assert status == EXIT_OK
    after = matrix_path.read_text(encoding="utf-8")
    ovh = _cells(after, "35")[1]
    assert ovh.startswith(
        "PASS — 2026-11-01, `curate-d-test-contract` at `https://s3.example.test`"
    )
    assert ovh.endswith("signed with region `us-east-va`")
    assert "PENDING" not in after


def test_record_writes_fail_cells_and_exits_one_on_a_failing_test(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failing suite test fails rows 34/35 and leaves the checksum rows to their test."""
    report = _report(
        _case(CHECKSUM_TEST, headers=HEADERS),
        _case("test_listing_paginates", outcome="fail"),
        _case("test_other", outcome="skip"),
    )

    status, matrix_path, _ = _record(tmp_path, {Backend.RUSTFS: report}, status=1)

    assert status == EXIT_CHECK_FAILED
    after = matrix_path.read_text(encoding="utf-8")
    assert _cells(after, "32")[0].startswith("PASS — ")
    assert _cells(after, "34")[0] == (
        f"FAIL — 2026-11-01, {RUSTFS_ID}: 1 of 3 tests in "
        "`tests/integration/test_staging_contract.py` failed (1 skipped, not counted): "
        "`test_listing_paginates[rustfs]`"
    )
    assert "contract tests failed" in capsys.readouterr().err


def test_record_fails_the_checksum_rows_when_the_checksum_test_fails(tmp_path: Path) -> None:
    """A failing checksum test is recorded as FAIL on rows 32 and 33, never PASS."""
    report = _report(_case(CHECKSUM_TEST, outcome="fail"), _case("test_other"))

    status, matrix_path, _ = _record(tmp_path, {Backend.RUSTFS: report}, status=1)

    assert status == EXIT_CHECK_FAILED
    after = matrix_path.read_text(encoding="utf-8")
    expected = f"FAIL — 2026-11-01, {RUSTFS_ID}: `{CHECKSUM_TEST}[rustfs]` under botocore 1.40.61"
    assert _cells(after, "32")[0] == expected
    assert _cells(after, "33")[0] == expected
    assert _cells(after, "34")[0].startswith("FAIL — ")


@pytest.mark.parametrize(
    "report",
    [
        _report(_case(CHECKSUM_TEST), _case("test_other")),
        _report(_case(CHECKSUM_TEST, outcome="skip", headers=HEADERS)),
        _report(_case("test_other")),
        _report(),
        "not xml",
    ],
    ids=["missing-property", "checksum-skipped", "checksum-absent", "no-cases", "not-xml"],
)
def test_record_writes_nothing_when_the_report_cannot_support_the_cells(
    tmp_path: Path, report: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """A report without the checksum evidence exits 2 and leaves the matrix unchanged."""
    status, matrix_path, before = _record(tmp_path, {Backend.RUSTFS: report})

    assert status == EXIT_NOT_RUN
    assert matrix_path.read_text(encoding="utf-8") == before
    assert "unchanged" in capsys.readouterr().err


def test_record_writes_nothing_when_the_suite_produced_no_report(tmp_path: Path) -> None:
    """A suite that could not run (no report) exits 2 without a write."""
    status, matrix_path, before = _record(tmp_path, {}, status=4)

    assert status == EXIT_NOT_RUN
    assert matrix_path.read_text(encoding="utf-8") == before


def test_record_writes_nothing_when_pytest_was_interrupted(tmp_path: Path) -> None:
    """A report from an interrupted or errored session is not evidence."""
    status, matrix_path, before = _record(tmp_path, {Backend.RUSTFS: PASSING}, status=2)

    assert status == EXIT_NOT_RUN
    assert matrix_path.read_text(encoding="utf-8") == before


def test_record_writes_nothing_when_one_of_two_backends_has_no_report(tmp_path: Path) -> None:
    """One backend without a report stops the whole recording, including the other one."""
    matrix_path, lock_path = _write(tmp_path, _matrix(_sdk_row(OLD)), _lock(NEW))
    argv = ["record", "--backend", "rustfs", "--backend", "ovh", "--lock", str(lock_path)]
    argv += ["--matrix", str(matrix_path), "--date", TODAY.isoformat()]

    status = sdk_evidence.main(argv, runner=_runner({Backend.RUSTFS: PASSING}))

    assert status == EXIT_NOT_RUN
    assert matrix_path.read_text(encoding="utf-8") == _matrix(_sdk_row(OLD))


def test_record_dry_run_prints_the_rows_and_leaves_the_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A dry run prints the rewritten rows and review list without writing."""
    status, matrix_path, before = _record(
        tmp_path, {Backend.RUSTFS: PASSING}, lock=NEW, extra=["--dry-run"]
    )

    assert status == EXIT_OK
    assert matrix_path.read_text(encoding="utf-8") == before
    output = capsys.readouterr().out
    assert "would rewrite 5 line(s)" in output
    assert f"7: {_sdk_row(NEW)}" in output
    assert "Review by hand" in output


def test_record_rejects_a_malformed_date(tmp_path: Path) -> None:
    """`--date` takes an ISO date only."""
    with pytest.raises(SystemExit) as raised:
        _record(tmp_path, {Backend.RUSTFS: PASSING}, extra=["--date", "01/11/2026"])
    assert raised.value.code == 2


def test_record_names_the_rustfs_endpoint_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run against a non-default RustFS endpoint records that endpoint."""
    monkeypatch.setenv("CURATE_D_TEST_RUSTFS_ENDPOINT", "http://127.0.0.1:9110")

    _, matrix_path, _ = _record(tmp_path, {Backend.RUSTFS: PASSING})

    rustfs = _cells(matrix_path.read_text(encoding="utf-8"), "34")[0]
    assert rustfs.startswith(f"PASS — 2026-11-01, {RUSTFS_ID} at `http://127.0.0.1:9110`:")


def test_record_reports_an_unreadable_matrix(tmp_path: Path) -> None:
    """A missing matrix is a run that recorded nothing."""
    lock_path = tmp_path / "uv.lock"
    lock_path.write_text(_lock(OLD), encoding="utf-8")
    argv = ["record", "--backend", "rustfs", "--lock", str(lock_path)]
    argv += ["--matrix", str(tmp_path / "absent.md")]

    assert sdk_evidence.main(argv, runner=_runner({Backend.RUSTFS: PASSING})) == EXIT_NOT_RUN


def test_run_contract_suite_invokes_pytest_on_the_contract_suite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real runner selects one backend, the integration marker and a JUnit report."""
    calls: list[list[str]] = []

    class Completed:
        """Stand in for subprocess.CompletedProcess."""

        returncode = 1

    def fake_run(command: list[str], *, check: bool) -> Completed:
        """Record the command instead of running pytest."""
        assert check is False
        calls.append(command)
        return Completed()

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert sdk_evidence.run_contract_suite(Backend.OVH, tmp_path / "ovh.xml") == 1
    command = calls[0]
    assert command[1:4] == ["-m", "pytest", "tests/integration/test_staging_contract.py"]
    assert "--staging-backend=ovh" in command
    assert f"--junitxml={tmp_path / 'ovh.xml'}" in command
    assert "--no-cov" in command
