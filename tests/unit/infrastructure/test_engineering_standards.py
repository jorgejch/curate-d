"""The standards' precedence, the Twelve-Factor record, the skill link and the DBOS version.

Each check is a function over text or a path that returns what is wrong, so one test runs it
on the real file and others run it on bad input, without editing a tracked file.
"""

import re
import tomllib
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import NamedTuple, cast

import pytest
import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
VENDORED_SKILL = REPOSITORY_ROOT / ".agents/skills/dbos-python"
SKILL_LINK = REPOSITORY_ROOT / ".claude/skills/dbos-python"
SKILL_LINK_TARGET = "../../.agents/skills/dbos-python"
LINK_REPAIR = 'README.md, "Coding agents and DBOS", gives the command that restores it'

RECORD_NAME = "docs/twelve-factor.md"
RECORD = REPOSITORY_ROOT / RECORD_NAME
CONFIG_NAME = "openspec/config.yaml"
CONFIG = REPOSITORY_ROOT / CONFIG_NAME
AGENT_CONVENTIONS_NAME = "CLAUDE.md"
AGENT_CONVENTIONS = REPOSITORY_ROOT / AGENT_CONVENTIONS_NAME
PRECEDENCE = ("Safety guarantees", "DBOS", "Twelve-Factor", "Coding standards")
README_NAME = "README.md"
README = REPOSITORY_ROOT / README_NAME
PYPROJECT_NAME = "pyproject.toml"
PYPROJECT = REPOSITORY_ROOT / PYPROJECT_NAME

DBOS_REQUIREMENT = "dbos[otel]"
_EXACT_DBOS_PIN = re.compile(r"dbos\[otel\]==(?P<version>(?P<minor>\d+\.\d+)\.\d+)")
# How each document names the DBOS version in use. A dated record, such as the assessment line
# of docs/twelve-factor.md, is not a current statement and is not compared.
CURRENT_DBOS_VERSION = {
    README_NAME: re.compile(r"\[DBOS\]\(https://docs\.dbos\.dev/\) (?P<minor>\d+\.\d+)\b"),
    CONFIG_NAME: re.compile(r"\*\*DBOS (?P<minor>\d+\.\d+)(?:\.\d+)?\*\*"),
}

MANIFESTO_REPOSITORY = "twelve-factor/twelve-factor"
LICENCE = "CC BY 4.0"
# The rules a status refers to are those of this commit, not of the head of `next`. Moving the
# pin is one change: re-read the text, review RULE_COUNTS and every row the new text touches,
# set the new read date, and move this constant and the record together.
PINNED_COMMIT = "3ad5a5f36312cc8ad876abae1bd691acd790d4d1"
RULE_COUNTS = {
    "I": 3,
    "II": 3,
    "III": 3,
    "IV": 3,
    "V": 3,
    "VI": 2,
    "VII": 2,
    "VIII": 4,
    "IX": 4,
    "X": 2,
    "XI": 2,
    "XII": 2,
}
RULES = tuple(
    f"{factor}.{number}" for factor, count in RULE_COUNTS.items() for number in range(1, count + 1)
)

CONFORMS = "Conforms"
LIMITED_BY_DBOS = "Limited by DBOS"
GAP = "Gap"
STATUSES = (CONFORMS, LIMITED_BY_DBOS, GAP)

_NUMBERED_BOLD_NAME = re.compile(r"^\d+\.\s+\*\*(?P<name>[^*]+)\*\*", re.MULTILINE)
_FULL_SHA = re.compile(r"\b[0-9a-f]{40}\b")
_SKILL_REFERENCE = re.compile(r"references/[A-Za-z0-9._-]+\.md")
_CELL_COUNT = 4


class _Row(NamedTuple):
    """One row of a rule table in the adoption record."""

    rule: str
    status: str
    statement: str
    authority: str


def _section(text: str, heading: str) -> str:
    """Return the body of the `## heading` section, or an empty string when there is none."""
    match = re.search(
        rf"^## {re.escape(heading)}[ \t]*$(?P<body>.*?)(?=^## |\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    return match["body"] if match else ""


def _rows(record: str) -> list[_Row]:
    """Return the rule rows of the record's `## Status` tables, without their header rows."""
    rows: list[_Row] = []
    for line in _section(record, "Status").splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if cells[0] == "Rule" or not cells[0].strip("-"):
            continue
        padded = (*cells, *[""] * _CELL_COUNT)[:_CELL_COUNT]
        rows.append(_Row(*padded))
    return rows


def precedence_problems(name: str, text: str, heading: str) -> list[str]:
    """Return what is wrong with the order of precedence that a file states under a heading."""
    found = _NUMBERED_BOLD_NAME.findall(_section(text, heading))
    if not found:
        return [f"{name} states no order of precedence under '## {heading}'"]
    if tuple(found) != PRECEDENCE:
        return [f"{name} states the order {found}, expected {list(PRECEDENCE)}"]
    return []


def pin_problems(record: str) -> list[str]:
    """Return what the record's pin of the manifesto text lacks, or where it moved."""
    pin = _section(record, "The pinned text")
    named = {
        f"the repository {MANIFESTO_REPOSITORY}": MANIFESTO_REPOSITORY,
        "the branch `next`": "`next`",
        f"the licence {LICENCE}": LICENCE,
    }
    problems = [
        f"{RECORD_NAME} does not name {label} under '## The pinned text'"
        for label, needle in named.items()
        if needle not in pin
    ]
    commits = sorted(set(_FULL_SHA.findall(record)))
    if not commits:
        problems.append(f"{RECORD_NAME} holds no 40-character commit SHA: no pinned commit")
    elif commits != [PINNED_COMMIT]:
        problems.append(
            f"{RECORD_NAME} names {commits}, this test pins {PINNED_COMMIT}: re-read the text, "
            "review RULE_COUNTS and every row, then move the record and the constant together"
        )
    return problems


def row_problems(rows: Sequence[_Row]) -> list[str]:
    """Return the rules with no row, those with several, and the unknown identifiers."""
    counts = Counter(row.rule for row in rows)
    problems = [f"missing rule: {rule}" for rule in RULES if rule not in counts]
    problems += [f"duplicated rule: {rule}" for rule, count in counts.items() if count > 1]
    problems += [f"unknown rule: {rule}" for rule in counts if rule not in RULES]
    return problems


def status_problems(rows: Sequence[_Row]) -> list[str]:
    """Return the rows whose status is not one of the three."""
    return [
        f"{row.rule}: unknown status {row.status!r}" for row in rows if row.status not in STATUSES
    ]


def dbos_reference_problems(rows: Sequence[_Row], skill: Path) -> list[str]:
    """Return the `Limited by DBOS` rows that name no reference file, or one the skill lacks."""
    problems: list[str] = []
    for row in rows:
        if row.status != LIMITED_BY_DBOS:
            continue
        references = _SKILL_REFERENCE.findall(row.authority)
        if not references:
            problems.append(f"{row.rule}: {LIMITED_BY_DBOS} names no reference file of the skill")
        problems += [
            f"{row.rule}: names {reference}, which the vendored skill does not hold"
            for reference in references
            if not (skill / reference).is_file()
        ]
    return problems


def gap_statement_problems(rows: Sequence[_Row]) -> list[str]:
    """Return the `Gap` rows that state nothing about what is unmet."""
    return [
        f"{row.rule}: {GAP} states nothing about what is unmet"
        for row in rows
        if row.status == GAP and not row.statement
    ]


def design_rule_problems(config: dict[str, object]) -> list[str]:
    """Return what is wrong when `rules.design` holds no rule about Twelve-Factor rules."""
    rules = config.get("rules")
    design = rules.get("design") if isinstance(rules, dict) else None
    entries = design if isinstance(design, list) else []
    if any(
        isinstance(entry, str) and "Twelve-Factor" in entry and RECORD_NAME in entry
        for entry in entries
    ):
        return []
    return ["openspec/config.yaml: missing design rule about Twelve-Factor rules in rules.design"]


def skill_link_problems(path: Path) -> list[str]:
    """Return what is wrong when `path` is not the link to the vendored skill."""
    if path.is_symlink():
        target = str(path.readlink())
        if target == SKILL_LINK_TARGET:
            return []
        return [f"{path} is a link to {target}, expected {SKILL_LINK_TARGET}"]
    if path.is_dir():
        return [f"{path} is a directory, not a link to the vendored skill; {LINK_REPAIR}"]
    if path.exists():
        return [f"{path} is a file, not a link to the vendored skill; {LINK_REPAIR}"]
    return [f"{path} is missing; {LINK_REPAIR}"]


def _exact_dbos_pins(project: dict[str, object]) -> list[re.Match[str]]:
    """Return the exact `dbos[otel]==X.Y.Z` pins among `project.dependencies`."""
    table = project.get("project")
    dependencies = table.get("dependencies") if isinstance(table, dict) else None
    entries = dependencies if isinstance(dependencies, list) else []
    return [
        match
        for entry in entries
        if isinstance(entry, str) and (match := _EXACT_DBOS_PIN.fullmatch(entry.replace(" ", "")))
    ]


def documented_dbos_version_problems(pyproject: str, documents: Mapping[str, str]) -> list[str]:
    """Return the documents that name a DBOS minor version other than the pin, or what it lacks.

    Args:
        pyproject: The text of `pyproject.toml`.
        documents: The text of each document, by a name that `CURRENT_DBOS_VERSION` holds.
    """
    try:
        project = tomllib.loads(pyproject)
    except tomllib.TOMLDecodeError as error:
        return [
            f"{PYPROJECT_NAME} cannot be parsed ({error}): no {DBOS_REQUIREMENT} pin to compare"
        ]
    pins = _exact_dbos_pins(project)
    if len(pins) != 1:
        return [
            f"{PYPROJECT_NAME} holds {len(pins)} exact {DBOS_REQUIREMENT}==X.Y.Z pins in "
            "project.dependencies, expected one: no pin to compare"
        ]
    pin = pins[0]
    problems: list[str] = []
    for name, text in documents.items():
        found = CURRENT_DBOS_VERSION[name].findall(text)
        if not found:
            problems.append(f"{name} names no current DBOS version")
        problems += [
            f"{name} names DBOS {minor}, {PYPROJECT_NAME} pins {DBOS_REQUIREMENT}=={pin['version']}"
            for minor in found
            if minor != pin["minor"]
        ]
    return problems


def _pyproject(*dependencies: str) -> str:
    """Return a `pyproject.toml` whose `project.dependencies` holds `dependencies`."""
    entries = ", ".join(f'"{dependency}"' for dependency in dependencies)
    return f'[project]\nname = "x"\ndependencies = [{entries}]\n'


def _documents(readme: str = "3.5", config: str = "3.5") -> dict[str, str]:
    """Return the two documents, each naming DBOS at the given version as they write it."""
    return {
        README_NAME: f"Durable execution is [DBOS](https://docs.dbos.dev/) {readme}, chosen as\n",
        CONFIG_NAME: f"  - **DBOS {config}** - durable execution as a library\n",
    }


def _section_with_order(*names: str) -> str:
    """Return a `## Standards` section that lists `names` as numbered bold entries."""
    entries = "\n".join(f"{rank}. **{name}** text" for rank, name in enumerate(names, 1))
    return f"# Title\n\n## Standards\n\n{entries}\n\n## Next\n\n1. **Not** an entry\n"


def _pin(*, commit: str = PINNED_COMMIT, omit: str = "") -> str:
    """Return a `## The pinned text` section, with the named part of the pin left out."""
    parts = {
        "repository": MANIFESTO_REPOSITORY,
        "branch": "branch `next`",
        "licence": LICENCE,
        "commit": commit,
    }
    kept = ", ".join(value for part, value in parts.items() if part != omit)
    return f"## The pinned text\n\n{kept}\n\n## Status\n"


def _record_rows(*rows: tuple[str, str, str, str]) -> list[_Row]:
    """Return the rows parsed from a record whose `## Status` table holds `rows`."""
    lines = "\n".join(f"| {' | '.join(row)} |" for row in rows)
    return _rows(
        "## Status\n\n### I. Codebase\n\n"
        "| Rule | Status | Evidence, or what is unmet | Authority or tracking |\n"
        f"| --- | --- | --- | --- |\n{lines}\n\n"
        "## Keeping this page true\n\n| Not | a | row | x |\n"
    )


def _all_rules_rows() -> list[_Row]:
    """Return one good row for each rule."""
    return _record_rows(*[(rule, CONFORMS, "Holds.", "-") for rule in RULES])


def test_documents_naming_the_pinned_minor_version_are_accepted() -> None:
    """Documents that name the minor version of the `dbos[otel]` pin have no problem."""
    pyproject = _pyproject("sqlalchemy~=2.1.1", "dbos[otel]==3.5.0")

    assert documented_dbos_version_problems(pyproject, _documents()) == []


def test_a_document_naming_an_older_version_names_the_document_and_the_version() -> None:
    """A document that names DBOS 3.4 under a 3.5.0 pin names the document and 3.4."""
    problems = documented_dbos_version_problems(
        _pyproject("dbos[otel]==3.5.0"), _documents(readme="3.4")
    )

    assert problems == [f"{README_NAME} names DBOS 3.4, {PYPROJECT_NAME} pins dbos[otel]==3.5.0"]


def test_a_document_naming_no_version_names_the_document() -> None:
    """A document with no current DBOS version fails rather than passing unchecked."""
    documents = {**_documents(), CONFIG_NAME: "  - **DBOS 3.x** - durable execution\n"}

    problems = documented_dbos_version_problems(_pyproject("dbos[otel]==3.5.0"), documents)

    assert problems == [f"{CONFIG_NAME} names no current DBOS version"]


@pytest.mark.parametrize(
    "text",
    [
        "Durable execution is DBOS 3.4, chosen as a library",
        "Durable execution is [DBOS](https://docs.dbos.dev/) 3.x, chosen as a library",
        "Assessed 2026-11-04 against DBOS 3.4.0 and `dbos-python` skill 2.2.0.",
        "dbos[otel]==3.4.0",
    ],
    ids=["no-link", "no-minor", "dated-record", "requirement"],
)
def test_other_mentions_of_dbos_are_not_the_readme_version(text: str) -> None:
    """Only the README's linked `DBOS X.Y` counts as the version it names as current."""
    assert CURRENT_DBOS_VERSION[README_NAME].findall(text) == []


@pytest.mark.parametrize(
    ("dependencies", "count"),
    [
        (("sqlalchemy~=2.1.1",), 0),
        (("dbos[otel]>=3.5.0",), 0),
        (("dbos[otel]==3.5",), 0),
        (("dbos==3.5.0",), 0),
        (("dbos[otel]==3.5.0", "dbos[otel]==3.4.0"), 2),
    ],
    ids=["absent", "range", "no-patch", "no-extra", "two-pins"],
)
def test_no_single_exact_pin_names_the_file_and_the_pin(
    dependencies: tuple[str, ...], count: int
) -> None:
    """A `pyproject.toml` without one exact `dbos[otel]` pin names the file and the pin."""
    problems = documented_dbos_version_problems(_pyproject(*dependencies), _documents(readme="3.4"))

    assert len(problems) == 1
    assert problems[0].startswith(f"{PYPROJECT_NAME} holds {count} exact ")
    assert f"{DBOS_REQUIREMENT}==X.Y.Z" in problems[0]


@pytest.mark.parametrize(
    "pyproject",
    ['[project\ndependencies = ["dbos[otel]==3.5.0"]\n', '[project]\ndependencies = "x"\n'],
    ids=["unparsable", "dependencies-not-a-list"],
)
def test_an_unreadable_pin_names_the_file_and_the_pin(pyproject: str) -> None:
    """A `pyproject.toml` that cannot be parsed, or holds no list, names the file and the pin."""
    problems = documented_dbos_version_problems(pyproject, _documents())

    assert len(problems) == 1
    assert problems[0].startswith(PYPROJECT_NAME)
    assert DBOS_REQUIREMENT in problems[0]


def test_the_order_of_precedence_is_accepted() -> None:
    """A section that lists the four standards in order has no problem."""
    text = _section_with_order(*PRECEDENCE)

    assert precedence_problems("CLAUDE.md", text, "Standards") == []


def test_another_order_of_precedence_names_the_file_and_the_order() -> None:
    """A section that lists the standards in another order names the file and that order."""
    text = _section_with_order("DBOS", "Safety guarantees", "Twelve-Factor", "Coding standards")

    problems = precedence_problems("CLAUDE.md", text, "Standards")

    assert len(problems) == 1
    assert "CLAUDE.md" in problems[0]
    assert "['DBOS', 'Safety guarantees', 'Twelve-Factor', 'Coding standards']" in problems[0]


@pytest.mark.parametrize(
    "text",
    ["# Title\n\nNo such section.\n", "## Standards\n\n- **DBOS** is a bullet\n\n## Next\n"],
    ids=["no-section", "no-numbered-entries"],
)
def test_no_order_of_precedence_names_the_file(text: str) -> None:
    """A file with no section, or no numbered entries in it, is named as stating no order."""
    assert precedence_problems("CLAUDE.md", text, "Standards") == [
        "CLAUDE.md states no order of precedence under '## Standards'"
    ]


def test_a_complete_pin_is_accepted() -> None:
    """A pin that names the repository, the branch, the licence and the commit has no problem."""
    assert pin_problems(_pin()) == []


@pytest.mark.parametrize(
    ("omit", "named"),
    [
        ("repository", MANIFESTO_REPOSITORY),
        ("branch", "`next`"),
        ("licence", LICENCE),
    ],
)
def test_a_pin_without_its_source_or_licence_names_what_is_missing(omit: str, named: str) -> None:
    """A pin that lacks the repository, the branch or the licence names the missing part."""
    problems = pin_problems(_pin(omit=omit))

    assert len(problems) == 1
    assert named in problems[0]


@pytest.mark.parametrize(
    "record",
    [_pin(omit="commit"), _pin(commit=PINNED_COMMIT[:12])],
    ids=["absent", "abbreviated"],
)
def test_a_pin_with_no_full_commit_names_the_missing_commit(record: str) -> None:
    """A record with no 40-character SHA names the missing pinned commit."""
    assert pin_problems(record) == [
        f"{RECORD_NAME} holds no 40-character commit SHA: no pinned commit"
    ]


def test_another_commit_names_both_and_asks_for_a_review_of_the_rules() -> None:
    """A commit other than the pinned one names both and asks to review the rule counts."""
    other = "0" * 40

    problems = pin_problems(_pin(commit=other))

    assert len(problems) == 1
    assert other in problems[0]
    assert PINNED_COMMIT in problems[0]
    assert "review RULE_COUNTS" in problems[0]


def test_a_second_commit_in_a_link_fails_the_pin() -> None:
    """A link through a commit other than the pinned one fails, wherever it sits."""
    record = _pin() + f"[I](https://example.invalid/{'0' * 40}/content/codebase.md)\n"

    assert len(pin_problems(record)) == 1


def test_there_are_thirty_three_rules_from_i1_to_xii2() -> None:
    """The per-factor counts build the 33 identifiers of the pinned text."""
    assert len(RULES) == 33
    assert RULES[0] == "I.1"
    assert RULES[-1] == "XII.2"


def test_one_good_row_for_each_rule_is_accepted() -> None:
    """A record that lists each rule once with a known status has no problem."""
    rows = _all_rules_rows()

    assert row_problems(rows) == []
    assert status_problems(rows) == []


def test_rows_outside_the_status_section_and_header_rows_are_not_read() -> None:
    """Only the rule rows of the `## Status` section count as rows."""
    assert [row.rule for row in _record_rows(("I.1", CONFORMS, "Holds.", "-"))] == ["I.1"]


def test_a_missing_rule_is_named() -> None:
    """A rule with no row is named."""
    assert row_problems(_all_rules_rows()[1:]) == ["missing rule: I.1"]


def test_a_duplicated_rule_is_named() -> None:
    """A rule with two rows is named."""
    rows = [*_all_rules_rows(), _Row("XII.2", CONFORMS, "Again.", "-")]

    assert row_problems(rows) == ["duplicated rule: XII.2"]


@pytest.mark.parametrize("identifier", ["XIII.1", "I.4", "codebase"])
def test_an_unknown_rule_is_named(identifier: str) -> None:
    """A row outside I.1 to XII.2 is named, with the identifier it carries."""
    rows = [*_all_rules_rows(), _Row(identifier, CONFORMS, "Holds.", "-")]

    assert row_problems(rows) == [f"unknown rule: {identifier}"]


def test_an_unknown_status_names_the_rule_and_the_value() -> None:
    """A status other than the three names the rule and the value found."""
    rows = _record_rows(("II.1", "Partial", "Half of it.", "-"))

    assert status_problems(rows) == ["II.1: unknown status 'Partial'"]


def test_a_dbos_limit_naming_an_existing_reference_is_accepted(tmp_path: Path) -> None:
    """A `Limited by DBOS` row whose reference files exist has no problem."""
    (tmp_path / "references").mkdir()
    (tmp_path / "references/queue-management.md").touch()
    authority = "[`queue-management`](../skill/references/queue-management.md)"
    rows = _record_rows(("III.2", LIMITED_BY_DBOS, "Queue limits.", authority))

    assert dbos_reference_problems(rows, tmp_path) == []


def test_a_dbos_limit_with_no_reference_names_the_rule(tmp_path: Path) -> None:
    """A `Limited by DBOS` row that names no reference file names the rule."""
    rows = _record_rows(("IX.1", LIMITED_BY_DBOS, "Workers stay.", "#74"))

    problems = dbos_reference_problems(rows, tmp_path)

    assert len(problems) == 1
    assert problems[0].startswith("IX.1:")


def test_a_dbos_limit_with_a_reference_the_skill_lacks_names_the_rule_and_the_file(
    tmp_path: Path,
) -> None:
    """A reference file that the skill no longer holds names the rule and the file."""
    rows = _record_rows(("IX.1", LIMITED_BY_DBOS, "Workers stay.", "references/gone.md"))

    assert dbos_reference_problems(rows, tmp_path) == [
        "IX.1: names references/gone.md, which the vendored skill does not hold"
    ]


def test_a_reference_on_a_row_of_another_status_is_not_checked(tmp_path: Path) -> None:
    """Only a `Limited by DBOS` row must name a reference file that exists."""
    rows = _record_rows(("V.3", GAP, "Runs at start.", "references/gone.md"))

    assert dbos_reference_problems(rows, tmp_path) == []


def test_a_gap_with_no_statement_names_the_rule() -> None:
    """A `Gap` row with an empty statement names the rule; one with a statement does not."""
    rows = _record_rows(("X.1", GAP, "", "release process"), ("X.2", GAP, "Differs.", "both"))

    assert gap_statement_problems(rows) == ["X.1: Gap states nothing about what is unmet"]


def test_the_design_rule_is_accepted() -> None:
    """A `rules.design` entry that names the rules and the record has no problem."""
    config: dict[str, object] = {
        "rules": {"design": [f"Name each Twelve-Factor rule and update {RECORD_NAME}."]}
    }

    assert design_rule_problems(config) == []


@pytest.mark.parametrize(
    "config",
    [
        {},
        {"rules": {"specs": ["Write requirements."]}},
        {"rules": {"design": ["An unrelated rule."]}},
    ],
    ids=["no-rules", "no-design-list", "another-design-rule"],
)
def test_no_design_rule_is_named(config: dict[str, object]) -> None:
    """A config with no design rule about Twelve-Factor rules names the missing rule."""
    problems = design_rule_problems(config)

    assert len(problems) == 1
    assert "missing design rule" in problems[0]


def test_a_link_to_the_vendored_skill_is_accepted(tmp_path: Path) -> None:
    """A symbolic link with the expected target has no problem."""
    link = tmp_path / "dbos-python"
    link.symlink_to(SKILL_LINK_TARGET)

    assert skill_link_problems(link) == []


def test_a_copy_of_the_skill_names_the_path_and_the_repair(tmp_path: Path) -> None:
    """A directory in place of the link names the path, says what it is, and gives the repair."""
    copy = tmp_path / "dbos-python"
    copy.mkdir()

    assert skill_link_problems(copy) == [
        f"{copy} is a directory, not a link to the vendored skill; {LINK_REPAIR}"
    ]


def test_a_file_in_place_of_the_link_names_the_path(tmp_path: Path) -> None:
    """A regular file in place of the link names the path and says what it is."""
    path = tmp_path / "dbos-python"
    path.touch()

    assert skill_link_problems(path) == [
        f"{path} is a file, not a link to the vendored skill; {LINK_REPAIR}"
    ]


def test_a_missing_path_is_named(tmp_path: Path) -> None:
    """A path that does not exist is named as missing."""
    path = tmp_path / "dbos-python"

    assert skill_link_problems(path) == [f"{path} is missing; {LINK_REPAIR}"]


def test_a_link_to_another_target_names_the_target(tmp_path: Path) -> None:
    """A symbolic link to any other target names the target it found."""
    link = tmp_path / "dbos-python"
    link.symlink_to("../elsewhere")

    assert skill_link_problems(link) == [
        f"{link} is a link to ../elsewhere, expected {SKILL_LINK_TARGET}"
    ]


def test_the_skill_path_links_to_the_vendored_skill() -> None:
    """The repository's skill path is the link to the one vendored copy."""
    assert skill_link_problems(SKILL_LINK) == []
    assert (SKILL_LINK / "SKILL.md").samefile(VENDORED_SKILL / "SKILL.md")


def _record() -> str:
    """Return the adoption record's text."""
    return RECORD.read_text(encoding="utf-8")


def test_the_record_states_the_order_of_precedence() -> None:
    """The adoption record lists the four standards in order under `## Precedence`."""
    assert precedence_problems(RECORD_NAME, _record(), "Precedence") == []


def test_the_record_pins_the_manifesto_text() -> None:
    """The adoption record names the repository, the branch, the licence and the pinned commit."""
    assert pin_problems(_record()) == []


def test_the_record_lists_every_rule_once() -> None:
    """The adoption record has one row for each of the 33 rules of the pinned text."""
    assert row_problems(_rows(_record())) == []


def test_every_rule_in_the_record_has_a_known_status() -> None:
    """Each row of the adoption record is `Conforms`, `Limited by DBOS` or `Gap`."""
    assert status_problems(_rows(_record())) == []


def test_every_dbos_limit_in_the_record_names_a_reference_of_the_skill() -> None:
    """Each `Limited by DBOS` row names reference files that the vendored skill holds."""
    assert dbos_reference_problems(_rows(_record()), VENDORED_SKILL) == []


def test_every_gap_in_the_record_states_what_is_unmet() -> None:
    """No `Gap` row of the adoption record has an empty statement."""
    assert gap_statement_problems(_rows(_record())) == []


def _config() -> dict[str, object]:
    """Return the parsed OpenSpec project config."""
    return cast(dict[str, object], yaml.safe_load(CONFIG.read_text(encoding="utf-8")))


def test_the_openspec_config_states_the_order_of_precedence() -> None:
    """The context of the OpenSpec config lists the four standards in order under `## Standards`."""
    context = str(_config()["context"])

    assert precedence_problems(CONFIG_NAME, context, "Standards") == []


def test_the_openspec_config_carries_the_design_rule() -> None:
    """`rules.design` in the OpenSpec config asks a design to name the rules it affects."""
    assert design_rule_problems(_config()) == []


def test_the_agent_conventions_state_the_order_of_precedence() -> None:
    """`CLAUDE.md` lists the four standards in order under `## Standards`."""
    text = AGENT_CONVENTIONS.read_text(encoding="utf-8")

    assert precedence_problems(AGENT_CONVENTIONS_NAME, text, "Standards") == []


def test_the_documents_name_the_pinned_dbos_version() -> None:
    """`README.md` and the OpenSpec config name the DBOS minor version of the pin."""
    documents = {
        README_NAME: README.read_text(encoding="utf-8"),
        CONFIG_NAME: CONFIG.read_text(encoding="utf-8"),
    }

    assert documented_dbos_version_problems(PYPROJECT.read_text(encoding="utf-8"), documents) == []
