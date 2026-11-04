"""Keep the staging evidence matrix's S3 SDK evidence in step with the locked SDK.

`ops/staging-compatibility.md` names the SDK versions its rows were earned under, and
rows 32-35 hold evidence that only holds for one SDK. Two subcommands:

    python -m curate_d.staging.sdk_evidence check
    python -m curate_d.staging.sdk_evidence record --backend rustfs [--dry-run]

`check` fails when `uv.lock` and the matrix's `SDK` row disagree. `record` runs the shared
contract suite on each selected backend and rewrites the `SDK` row and those rows' backend
cells from the run's JUnit report. A backend not run keeps its previous evidence, marked
PENDING when the SDK moved: evidence earned under one SDK is not evidence about another.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import tomllib
import xml.etree.ElementTree as ElementTree
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from importlib import metadata
from pathlib import Path

EXIT_OK = 0
EXIT_CHECK_FAILED = 1
EXIT_NOT_RUN = 2

SDK_PACKAGES = ("aioboto3", "aiobotocore", "botocore")
SDK_SENSITIVE_ROWS = ("32", "33", "34", "35")
CHECKSUM_TEST = "test_default_sdk_checksum_headers_are_accepted"
# Recorded by CHECKSUM_TEST in tests/integration/test_staging_contract.py.
CHECKSUM_PROPERTY = "sdk_checksum_headers"
CONTRACT_SUITE = "tests/integration/test_staging_contract.py"
DEFAULT_LOCK = Path("uv.lock")
DEFAULT_MATRIX = Path("ops/staging-compatibility.md")

_PENDING_PREFIX = "PENDING — "
_PREVIOUS_EVIDENCE = "; previous evidence: "
_SDK_VERSION = re.compile(r"`(aioboto3|aiobotocore|botocore)`\s+(\S+)")


class Backend(StrEnum):
    """Name a staging backend as the contract suite's `--staging-backend` option does."""

    RUSTFS = "rustfs"
    OVH = "ovh"

    @property
    def column(self) -> str:
        """Return the matrix column holding this backend's evidence."""
        return {Backend.RUSTFS: "RustFS", Backend.OVH: "OVH"}[self]


class EvidenceError(Exception):
    """Report a lock, matrix or report that cannot be read as the command needs."""


SdkVersions = Mapping[str, str]


@dataclass(frozen=True)
class MatrixRow:
    """Hold one parsed table row and the line it came from."""

    index: int
    cells: tuple[str, ...]


@dataclass(frozen=True)
class Matrix:
    """Hold the matrix lines and the parts of them the SDK evidence depends on."""

    lines: tuple[str, ...]
    sdk_index: int
    sdk: SdkVersions
    rustfs_server: str
    rustfs_endpoint: str
    columns: tuple[str, ...]
    rows: Mapping[str, MatrixRow]


@dataclass(frozen=True)
class CaseOutcome:
    """Record one test case's result from a JUnit report."""

    name: str
    passed: bool
    failed: bool
    properties: Mapping[str, str]


@dataclass(frozen=True)
class BackendRun:
    """Hold one backend's contract-suite outcomes and the identity it ran against."""

    backend: Backend
    identity: str
    region: str
    outcomes: tuple[CaseOutcome, ...]

    @property
    def failed(self) -> tuple[str, ...]:
        """Return the names of the failing tests."""
        return tuple(outcome.name for outcome in self.outcomes if outcome.failed)

    @property
    def passed(self) -> int:
        """Return how many tests passed."""
        return sum(outcome.passed for outcome in self.outcomes)

    @property
    def skipped(self) -> int:
        """Return how many tests neither passed nor failed."""
        return sum(not (o.passed or o.failed) for o in self.outcomes)

    def checksum_outcome(self) -> CaseOutcome:
        """Return the checksum test's outcome, refusing a report it is missing from."""
        for outcome in self.outcomes:
            if outcome.name.split("[", 1)[0] == CHECKSUM_TEST:
                if outcome.passed and CHECKSUM_PROPERTY not in outcome.properties:
                    raise EvidenceError(
                        f"{self.backend.value} report: {outcome.name} passed without "
                        f"recording {CHECKSUM_PROPERTY}"
                    )
                if not (outcome.passed or outcome.failed):
                    raise EvidenceError(f"{self.backend.value} report: {outcome.name} skipped")
                return outcome
        raise EvidenceError(f"{self.backend.value} report: {CHECKSUM_TEST} did not run")


def split_row(line: str) -> tuple[str, ...]:
    """Split a Markdown table row into stripped cells.

    A `|` inside a backtick-quoted span or escaped as `\\|` stays in its cell.

    Raises:
        EvidenceError: The line is not a table row.
    """
    stripped = line.strip()
    if not (stripped.startswith("|") and stripped.endswith("|") and len(stripped) > 1):
        raise EvidenceError(f"not a table row: {line!r}")
    cells: list[str] = []
    current: list[str] = []
    in_code = False
    body = stripped[1:-1]
    position = 0
    while position < len(body):
        char = body[position]
        if char == "\\" and body[position + 1 : position + 2] == "|":
            current.append("\\|")
            position += 2
            continue
        if char == "`":
            in_code = not in_code
        if char == "|" and not in_code:
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(char)
        position += 1
    cells.append("".join(current).strip())
    return tuple(cells)


def join_row(cells: Sequence[str]) -> str:
    """Render cells as a Markdown table row."""
    return "| " + " | ".join(cells) + " |"


def read_locked_versions(lock_text: str, *, source: Path = DEFAULT_LOCK) -> dict[str, str]:
    """Return the version the lock resolves for each S3 SDK package.

    Raises:
        EvidenceError: The lock is not TOML or does not resolve every SDK package.
    """
    try:
        lock = tomllib.loads(lock_text)
    except tomllib.TOMLDecodeError as error:
        raise EvidenceError(f"{source}: not a TOML lock file: {error}") from error
    packages = lock.get("package")
    versions: dict[str, str] = {}
    if isinstance(packages, list):
        for package in packages:
            if isinstance(package, dict):
                name, version = package.get("name"), package.get("version")
                if name in SDK_PACKAGES and isinstance(name, str) and isinstance(version, str):
                    versions[name] = version
    missing = [name for name in SDK_PACKAGES if name not in versions]
    if missing:
        raise EvidenceError(f"{source}: no locked version for {', '.join(missing)}")
    return versions


def _header_cell(lines: Sequence[str], target: str, source: Path) -> tuple[int, str]:
    """Return the line index and value of the two-column header-table row `target`."""
    for index, line in enumerate(lines):
        if line.startswith(f"| {target} |"):
            cells = split_row(line)
            if len(cells) != 2:
                raise EvidenceError(f"{source}:{index + 1}: `{target}` row needs 2 cells")
            return index, cells[1]
    raise EvidenceError(f"{source}: no `| {target} |` row in the header table")


def _parse_sdk(value: str, line_number: int, source: Path) -> dict[str, str]:
    """Parse the SDK row's value into one version per package."""
    versions = dict(_SDK_VERSION.findall(value))
    missing = [name for name in SDK_PACKAGES if name not in versions]
    if missing:
        raise EvidenceError(f"{source}:{line_number}: SDK row does not name {', '.join(missing)}")
    return versions


def _first_code_span(value: str, what: str, source: Path) -> str:
    """Return the first backtick-quoted span of a header-table value."""
    match = re.search(r"`([^`]+)`", value)
    if match is None:
        raise EvidenceError(f"{source}: `{what}` row quotes no value")
    return match.group(1)


def _table_rows(
    lines: Sequence[str], start: int, width: int, source: Path
) -> tuple[dict[str, MatrixRow], int]:
    """Return a table's body rows by their first cell, and the index after the table.

    Raises:
        EvidenceError: A row's cell count differs from `width`, its header's.
    """
    rows: dict[str, MatrixRow] = {}
    index = start
    while index < len(lines) and lines[index].startswith("|"):
        cells = split_row(lines[index])
        if len(cells) != width:
            raise EvidenceError(
                f"{source}:{index + 1}: row has {len(cells)} cells, its table header has {width}"
            )
        rows[cells[0]] = MatrixRow(index, cells)
        index += 1
    return rows, index


def _sdk_sensitive_rows(
    lines: Sequence[str], source: Path
) -> tuple[tuple[str, ...], dict[str, MatrixRow]]:
    """Return the column order and rows 32-35 of the table that holds them.

    Raises:
        EvidenceError: The rows are missing, or a row of their table has the wrong cell count.
    """
    index = 0
    while index < len(lines):
        if not lines[index].startswith("| # |"):
            index += 1
            continue
        columns = split_row(lines[index])
        # Skip the header and its `| --- |` separator.
        rows, index = _table_rows(lines, index + 2, len(columns), source)
        if all(number in rows for number in SDK_SENSITIVE_ROWS):
            for name in (backend.column for backend in Backend):
                if name not in columns:
                    raise EvidenceError(f"{source}: the rows 32-35 table has no {name} column")
            return columns, {number: rows[number] for number in SDK_SENSITIVE_ROWS}
    raise EvidenceError(f"{source}: no matrix table holds rows {', '.join(SDK_SENSITIVE_ROWS)}")


def parse_matrix(text: str, *, source: Path = DEFAULT_MATRIX) -> Matrix:
    """Parse the parts of the evidence matrix the SDK evidence reads and writes.

    Raises:
        EvidenceError: A row the command needs is missing or malformed.
    """
    lines = tuple(text.split("\n"))
    sdk_index, sdk_value = _header_cell(lines, "SDK", source)
    _, server = _header_cell(lines, "RustFS server", source)
    _, endpoint = _header_cell(lines, "RustFS endpoint", source)
    columns, rows = _sdk_sensitive_rows(lines, source)
    return Matrix(
        lines=lines,
        sdk_index=sdk_index,
        sdk=_parse_sdk(sdk_value, sdk_index + 1, source),
        rustfs_server=_first_code_span(server, "RustFS server", source),
        rustfs_endpoint=endpoint,
        columns=columns,
        rows=rows,
    )


def sdk_drift(locked: SdkVersions, recorded: SdkVersions) -> list[str]:
    """Return one `<package>: matrix <a>, lock <b>` line per package that differs."""
    return [
        f"{name}: matrix {recorded[name]}, lock {locked[name]}"
        for name in SDK_PACKAGES
        if recorded[name] != locked[name]
    ]


def check(lock_path: Path, matrix_path: Path) -> list[str]:
    """Compare the lock's SDK versions with the matrix's SDK row.

    Returns:
        One line per differing package; empty when the two agree.

    Raises:
        EvidenceError: Either file cannot be read or parsed.
    """
    try:
        lock_text = lock_path.read_text(encoding="utf-8")
        matrix_text = matrix_path.read_text(encoding="utf-8")
    except OSError as error:
        raise EvidenceError(f"reading {error.filename}: {error.strerror}") from error
    locked = read_locked_versions(lock_text, source=lock_path)
    return sdk_drift(locked, parse_matrix(matrix_text, source=matrix_path).sdk)


def parse_junit(report: str, backend: Backend, *, source: Path) -> tuple[CaseOutcome, ...]:
    """Return the test outcomes of a pytest JUnit XML report.

    Raises:
        EvidenceError: The report is not XML or holds no test case.
    """
    try:
        root = ElementTree.fromstring(report)
    except ElementTree.ParseError as error:
        raise EvidenceError(f"{source}: not a JUnit XML report: {error}") from error
    outcomes: list[CaseOutcome] = []
    for case in root.iter("testcase"):
        failed = case.find("failure") is not None or case.find("error") is not None
        skipped = case.find("skipped") is not None
        properties = {prop.get("name", ""): prop.get("value", "") for prop in case.iter("property")}
        outcomes.append(
            CaseOutcome(
                name=case.get("name", ""),
                passed=not (failed or skipped),
                failed=failed,
                properties=properties,
            )
        )
    if not outcomes:
        raise EvidenceError(f"{source}: the {backend.value} report holds no test case")
    return tuple(outcomes)


def _code(value: str) -> str:
    """Quote a value as a code span, escaping any `|` that would split the cell."""
    return "`" + value.replace("|", "\\|") + "`"


def _checksum_cell(run: BackendRun, today: date, botocore: str) -> str:
    """Return a backend's row 32/33 cell from its checksum test."""
    outcome = run.checksum_outcome()
    if outcome.failed:
        return f"FAIL — {today}, {run.identity}: {_code(outcome.name)} under botocore {botocore}"
    headers: dict[str, str] = json.loads(outcome.properties[CHECKSUM_PROPERTY])
    shown = ", ".join(_code(f"{key}: {headers[key]}") for key in sorted(headers))
    return (
        f"PASS — {today}, {run.identity}: {_code(CHECKSUM_TEST)} under botocore {botocore}; "
        f"headers {shown}"
    )


def _suite_cell(run: BackendRun, today: date, signed: str) -> str:
    """Return a backend's row 34/35 cell from the whole contract suite."""
    skipped = f" ({run.skipped} skipped, not counted)" if run.skipped else ""
    if run.failed:
        names = ", ".join(_code(name) for name in run.failed)
        return (
            f"FAIL — {today}, {run.identity}: {len(run.failed)} of {len(run.outcomes)} "
            f"tests in {_code(CONTRACT_SUITE)} failed{skipped}: {names}"
        )
    return (
        f"PASS — {today}, {run.identity}: {run.passed} tests in {_code(CONTRACT_SUITE)} "
        f"signed {signed}{skipped}"
    )


def _pending_cell(previous: str, today: date, botocore: str) -> str:
    """Mark a backend's cell pending, keeping the evidence earned under the last SDK."""
    if previous.startswith(_PENDING_PREFIX) and _PREVIOUS_EVIDENCE in previous:
        previous = previous.split(_PREVIOUS_EVIDENCE, 1)[1]
    moved = f"{_PENDING_PREFIX}{today}: SDK moved to botocore {botocore}"
    return f"{moved}{_PREVIOUS_EVIDENCE}{previous}"


def _sdk_row(locked: SdkVersions) -> str:
    """Render the header table's SDK row."""
    return join_row(("SDK", " / ".join(f"`{name}` {locked[name]}" for name in SDK_PACKAGES)))


@dataclass(frozen=True)
class Recording:
    """Hold the rewritten matrix and what a person must still review in it."""

    text: str
    changed_lines: tuple[int, ...]
    review: tuple[str, ...]
    failed: bool


def rewrite_matrix(
    matrix: Matrix, locked: SdkVersions, runs: Sequence[BackendRun], today: date
) -> Recording:
    """Rewrite the SDK row and rows 32-35 from the backends' runs.

    Raises:
        EvidenceError: A run's report cannot support the cells it must fill.
    """
    lines = list(matrix.lines)
    moved = bool(sdk_drift(locked, matrix.sdk))
    botocore = locked["botocore"]
    lines[matrix.sdk_index] = _sdk_row(locked)
    by_backend = {run.backend: run for run in runs}
    generated: set[tuple[int, int]] = set()
    for number, row in matrix.rows.items():
        cells = list(row.cells)
        for backend in Backend:
            column = matrix.columns.index(backend.column)
            run = by_backend.get(backend)
            if run is not None:
                if number in ("32", "33"):
                    cells[column] = _checksum_cell(run, today, botocore)
                elif number == "34":
                    cells[column] = _suite_cell(run, today, "`s3v4`")
                else:
                    cells[column] = _suite_cell(run, today, f"with region {_code(run.region)}")
            elif moved:
                cells[column] = _pending_cell(cells[column], today, botocore)
            else:
                continue
            generated.add((row.index, column))
        lines[row.index] = join_row(cells)
    changed = tuple(index for index, line in enumerate(lines) if line != matrix.lines[index])
    return Recording(
        text="\n".join(lines),
        changed_lines=changed,
        review=_review_lines(matrix, lines, locked, generated),
        failed=any(run.failed for run in runs),
    )


def _review_lines(
    matrix: Matrix, lines: Sequence[str], locked: SdkVersions, generated: set[tuple[int, int]]
) -> tuple[str, ...]:
    """Return every other line that still names an SDK version the lock moved away from."""
    patterns = [
        re.compile(rf"(?<![\w-]){name}`?\s+{re.escape(matrix.sdk[name])}(?![\w.])")
        for name in SDK_PACKAGES
        if matrix.sdk[name] != locked[name]
    ]
    if not patterns:
        return ()
    review: list[str] = []
    for index, line in enumerate(lines):
        if index == matrix.sdk_index:
            continue
        text = line
        if any(row == index for row, _ in generated):
            cells = split_row(line)
            text = " ".join(
                cell for column, cell in enumerate(cells) if (index, column) not in generated
            )
        if any(pattern.search(text) for pattern in patterns):
            review.append(f"{index + 1}: {line[:160]}")
    return tuple(review)


def _rustfs_identity(matrix: Matrix, environ: Mapping[str, str]) -> tuple[str, str]:
    """Return the RustFS identity and signing region the contract suite targets."""
    image, _, digest = matrix.rustfs_server.partition("@sha256:")
    identity = f"`{image}@sha256:{digest[:8]}…`" if digest else _code(image)
    endpoint = environ.get("CURATE_D_TEST_RUSTFS_ENDPOINT")
    if endpoint:
        identity += f" at {_code(endpoint)}"
    region_match = re.search(r"region `([^`]+)`", matrix.rustfs_endpoint)
    default_region = region_match.group(1) if region_match else "us-east-1"
    return identity, environ.get("CURATE_D_TEST_RUSTFS_REGION", default_region)


def _ovh_identity(environ: Mapping[str, str]) -> tuple[str, str]:
    """Return the OVH bucket and endpoint the contract fixtures read, and the region.

    Raises:
        EvidenceError: The bucket or endpoint is not set, so the suite cannot reach OVH.
    """
    bucket = environ.get("CURATE_D_TEST_OVH_BUCKET")
    endpoint = environ.get("CURATE_D_TEST_OVH_ENDPOINT")
    if not bucket or not endpoint:
        raise EvidenceError(
            "ovh: CURATE_D_TEST_OVH_BUCKET and CURATE_D_TEST_OVH_ENDPOINT must be set; "
            "the contract suite reads its OVH fixture from them"
        )
    return (
        f"{_code(bucket)} at {_code(endpoint)}",
        environ.get("CURATE_D_TEST_OVH_REGION", "us-east-va"),
    )


SuiteRunner = Callable[[Backend, Path], int]


def run_contract_suite(backend: Backend, report: Path) -> int:
    """Run the shared contract suite on one backend, writing a JUnit report.

    Returns:
        pytest's exit status.
    """
    # The caller's PYTEST_ADDOPTS could deselect tests (`-k`); a deselected test leaves no
    # trace in the report, so the run would pass as the whole suite.
    environment = {name: value for name, value in os.environ.items() if name != "PYTEST_ADDOPTS"}
    command = [
        sys.executable,
        "-m",
        "pytest",
        CONTRACT_SUITE,
        "-m",
        "integration",
        "--no-cov",
        f"--staging-backend={backend.value}",
        f"--junitxml={report}",
        "-p",
        "no:cacheprovider",
    ]
    return subprocess.run(command, check=False, env=environment).returncode


def collect_runs(
    backends: Sequence[Backend],
    matrix: Matrix,
    runner: SuiteRunner,
    environ: Mapping[str, str],
    workdir: Path,
) -> list[BackendRun]:
    """Run the suite on each backend and read back its report.

    Raises:
        EvidenceError: A backend's settings are missing, or a run produced no usable report
            or no passing test; nothing may be recorded from it.
    """
    runs: list[BackendRun] = []
    for backend in backends:
        identity, region = (
            _rustfs_identity(matrix, environ)
            if backend is Backend.RUSTFS
            else _ovh_identity(environ)
        )
        report = workdir / f"{backend.value}.xml"
        status = runner(backend, report)
        # 0 all passed, 1 some failed; anything else (interrupted, usage or collection
        # error, no tests) is a run that produced no evidence.
        if status not in (0, 1) or not report.is_file():
            raise EvidenceError(
                f"{backend.value}: the contract suite did not run (pytest exit {status}, "
                f"report {'present' if report.is_file() else 'missing'})"
            )
        outcomes = parse_junit(report.read_text(encoding="utf-8"), backend, source=report)
        # A stack that is down or credentials the fixture refuses error every test: that
        # says nothing about the SDK, and must not replace the evidence with FAIL cells.
        if not any(outcome.passed for outcome in outcomes):
            raise EvidenceError(
                f"{backend.value}: no contract test passed; check the stack and the "
                "backend's settings before recording"
            )
        runs.append(BackendRun(backend, identity, region, outcomes))
    return runs


def _installed_version(package: str) -> str:
    """Return the version of a package installed in this environment, or "not installed"."""
    try:
        return metadata.version(package)
    except metadata.PackageNotFoundError:
        return "not installed"


def environment_drift(
    locked: Mapping[str, str], installed_version: Callable[[str], str]
) -> list[str]:
    """Return one line per SDK package whose installed version differs from the lock."""
    return [
        f"{package}: installed {installed}, lock {locked[package]}"
        for package in SDK_PACKAGES
        if (installed := installed_version(package)) != locked[package]
    ]


def record(
    backends: Sequence[Backend],
    *,
    lock_path: Path,
    matrix_path: Path,
    today: date,
    dry_run: bool,
    runner: SuiteRunner = run_contract_suite,
    environ: Mapping[str, str] | None = None,
    installed_version: Callable[[str], str] = _installed_version,
) -> Recording:
    """Run the contract suite on the backends and rewrite the matrix from the results.

    Returns:
        The rewritten matrix; written to `matrix_path` unless `dry_run`.

    Raises:
        EvidenceError: An input cannot be read, the installed SDK is not the locked one, or
            a run produced no usable report; the matrix is left unchanged.
    """
    try:
        lock_text = lock_path.read_text(encoding="utf-8")
        matrix_text = matrix_path.read_text(encoding="utf-8")
    except OSError as error:
        raise EvidenceError(f"reading {error.filename}: {error.strerror}") from error
    locked = read_locked_versions(lock_text, source=lock_path)
    # The suite runs on this interpreter's SDK, and the cells name the lock's: the two
    # must be the same, or a PASS would be credited to an SDK that never ran.
    if mismatch := environment_drift(locked, installed_version):
        raise EvidenceError(
            "the installed SDK is not the locked one ("
            + "; ".join(mismatch)
            + "); run `make staging-sdk-evidence`, whose `uv run` syncs the environment"
        )
    matrix = parse_matrix(matrix_text, source=matrix_path)
    with tempfile.TemporaryDirectory(prefix="sdk-evidence-") as workdir:
        runs = collect_runs(
            backends, matrix, runner, os.environ if environ is None else environ, Path(workdir)
        )
    recording = rewrite_matrix(matrix, locked, runs, today)
    if not dry_run:
        matrix_path.write_text(recording.text, encoding="utf-8")
    return recording


def _parse_date(value: str) -> date:
    """Parse a `--date` value as an ISO date."""
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"not a YYYY-MM-DD date: {value!r}") from error


def _parser() -> argparse.ArgumentParser:
    """Build the command-line parser for both subcommands."""
    parser = argparse.ArgumentParser(
        prog="python -m curate_d.staging.sdk_evidence",
        description="Check or re-record the S3 SDK evidence in the staging matrix.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name, summary in (
        ("check", "Fail when uv.lock and the matrix's SDK row disagree."),
        ("record", "Run the contract suite and rewrite the SDK row and rows 32-35."),
    ):
        command = commands.add_parser(name, help=summary, description=summary)
        if name == "record":
            command.add_argument(
                "--backend",
                action="append",
                required=True,
                type=Backend,
                choices=list(Backend),
                help="Backend to run and record; repeat for several.",
            )
            command.add_argument(
                "--date", type=_parse_date, help="Evidence date (default: today, local time)."
            )
            command.add_argument(
                "--dry-run",
                action="store_true",
                help="Print the rewritten rows and the review list; do not write the matrix.",
            )
    return parser


def _write(stream: str, text: str) -> None:
    """Write one line to standard output or standard error."""
    (sys.stdout if stream == "out" else sys.stderr).write(text + "\n")


def main(
    argv: Sequence[str] | None = None,
    *,
    runner: SuiteRunner = run_contract_suite,
    lock_path: Path = DEFAULT_LOCK,
    matrix_path: Path = DEFAULT_MATRIX,
    installed_version: Callable[[str], str] = _installed_version,
) -> int:
    """Run a subcommand and return its process exit status.

    The lock and the matrix are the repository's, relative to the working directory; the
    command line takes no path, so it cannot point the matrix write at another file.

    Returns:
        `check`: 0 when the lock and matrix agree, 1 on drift or an unreadable input.
        `record`: 0 when every test passed and the matrix was written, 1 when a test
        failed (the FAIL cells are written), 2 when nothing could be recorded.
    """
    invocation = _parser().parse_args(argv)
    if invocation.command == "check":
        try:
            drift = check(lock_path, matrix_path)
        except EvidenceError as error:
            _write("err", f"sdk evidence check failed: {error}")
            return EXIT_CHECK_FAILED
        for line in drift:
            _write("err", line)
        if drift:
            _write("err", f"{matrix_path} is stale; run `make staging-sdk-evidence`.")
            return EXIT_CHECK_FAILED
        _write("out", f"{matrix_path}: SDK row matches {lock_path}.")
        return EXIT_OK
    backends: list[Backend] = list(dict.fromkeys(invocation.backend))
    today: date = invocation.date or datetime.now().astimezone().date()
    try:
        recording = record(
            backends,
            lock_path=lock_path,
            matrix_path=matrix_path,
            today=today,
            dry_run=invocation.dry_run,
            runner=runner,
            installed_version=installed_version,
        )
    except EvidenceError as error:
        _write("err", f"sdk evidence not recorded, {matrix_path} unchanged: {error}")
        return EXIT_NOT_RUN
    lines = recording.text.split("\n")
    verb = "would rewrite" if invocation.dry_run else "rewrote"
    _write("out", f"{verb} {len(recording.changed_lines)} line(s) of {matrix_path}:")
    for index in recording.changed_lines:
        _write("out", f"{index + 1}: {lines[index]}")
    if recording.review:
        _write("out", "Review by hand; these lines still name the previous SDK versions:")
        for line in recording.review:
            _write("out", line)
    if recording.failed:
        _write("err", "contract tests failed; the FAIL cells record them.")
        return EXIT_CHECK_FAILED
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
