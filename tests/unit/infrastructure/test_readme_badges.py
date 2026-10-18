"""The README's status badges: dev's health, the licence and the release status."""

import re
import tomllib
from pathlib import Path
from typing import NamedTuple, cast
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
README = REPOSITORY_ROOT / "README.md"
PROPERTIES = REPOSITORY_ROOT / "sonar-project.properties"
PYPROJECT = REPOSITORY_ROOT / "pyproject.toml"

BRANCH = "dev"
SONAR_HOST = "sonarcloud.io"
_ACTIONS = "/jorgejch/curate-d/actions/workflows"


class _Source(NamedTuple):
    """Where a health badge's image comes from and where its link goes."""

    host: str
    image_path: str
    link_path: str
    metric: str | None = None


# In README order. GitHub serves CodeQL's default setup as a dynamic workflow, without a file.
HEALTH_SOURCES = {
    "Checks": _Source("github.com", f"{_ACTIONS}/checks.yml/badge.svg", f"{_ACTIONS}/checks.yml"),
    "CodeQL": _Source(
        "github.com",
        f"{_ACTIONS}/github-code-scanning/codeql/badge.svg",
        f"{_ACTIONS}/github-code-scanning/codeql",
    ),
    "Quality gate status": _Source(
        SONAR_HOST, "/api/project_badges/measure", "/summary/new_code", "alert_status"
    ),
    "Unit-test coverage": _Source(
        SONAR_HOST, "/api/project_badges/measure", "/summary/overall", "coverage"
    ),
}

_BADGE = re.compile(r"\[!\[(?P<alt>[^\]]*)\]\((?P<image>[^)\s]+)\)\]\((?P<link>[^)\s]+)\)")
_URL = re.compile(r"\]\((?P<url>[^)\s]+)\)")
_STATUS_NOTE = re.compile(r"\*\*Status: (?P<status>[^*]+?)\.\*\*")
# A shields.io static badge's path is `label-message-color`; a doubled dash is a literal one.
_SHIELDS_SEPARATOR = re.compile(r"(?<!-)-(?!-)")


class _Badge(NamedTuple):
    """A linked badge: `[![alt](image)](link)`."""

    alt: str
    image: str
    link: str


def _readme() -> str:
    """Return the README's text."""
    return README.read_text(encoding="utf-8")


def _badges() -> list[_Badge]:
    """Return the README's linked badges, in order."""
    return [_Badge(m["alt"], m["image"], m["link"]) for m in _BADGE.finditer(_readme())]


def _health_badges() -> list[_Badge]:
    """Return the badges whose alt text names a health signal, in README order."""
    return [badge for badge in _badges() if badge.alt in HEALTH_SOURCES]


def _badge(alt: str) -> _Badge:
    """Return the one badge called `alt`, failing when there is none or several."""
    matches = [badge for badge in _badges() if badge.alt == alt]
    if len(matches) != 1:
        pytest.fail(f"README.md holds {len(matches)} badges called {alt!r}, expected 1")
    return matches[0]


def _branch(url: str) -> str | None:
    """Return the branch a URL names, from `branch=` or an Actions `query=branch:` filter."""
    query = parse_qs(urlsplit(url).query)
    if "branch" in query:
        return query["branch"][0]
    for term in query.get("query", []):
        for part in term.split():
            if part.startswith("branch:"):
                return part.removeprefix("branch:")
    return None


def _shields_message(image: str) -> str:
    """Return the message of a shields.io static badge, decoding its escapes."""
    path = unquote(urlsplit(image).path).removeprefix("/badge/")
    parts = _SHIELDS_SEPARATOR.split(path)
    if len(parts) != 3:
        pytest.fail(f"{image} is not a shields.io static badge (label-message-color)")
    message = parts[1]
    return message.replace("__", "\0").replace("_", " ").replace("\0", "_").replace("--", "-")


def _sonar_project_key() -> str:
    """Return `sonar.projectKey` from sonar-project.properties."""
    for line in PROPERTIES.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() == "sonar.projectKey":
            return value.strip()
    pytest.fail(f"{PROPERTIES.name} declares no sonar.projectKey")


def test_the_health_badges_are_all_there_in_order() -> None:
    """The README holds Checks, CodeQL, quality gate and coverage once each, in that order."""
    found = [badge.alt for badge in _health_badges()]
    assert found == list(HEALTH_SOURCES), f"health badges found: {found}"


def test_each_health_badge_image_names_dev() -> None:
    """Every health badge's image URL names `dev`, so it never shows another branch."""
    offenders = [
        f"{badge.alt}: branch {_branch(badge.image)!r} in {badge.image}"
        for badge in _health_badges()
        if _branch(badge.image) != BRANCH
    ]
    assert not offenders, offenders


def test_each_health_badge_image_comes_from_its_source() -> None:
    """Checks and CodeQL draw on their workflows, the Sonar badges on their metrics."""
    offenders: list[str] = []
    for badge in _health_badges():
        source = HEALTH_SOURCES[badge.alt]
        image = urlsplit(badge.image)
        metric = parse_qs(image.query).get("metric", [None])[0]
        if (image.netloc, image.path, metric) != (source.host, source.image_path, source.metric):
            offenders.append(f"{badge.alt}: {badge.image}")
    assert not offenders, offenders


def test_each_health_badge_links_to_devs_view() -> None:
    """Every health badge links to its signal's view, filtered to `dev`."""
    offenders: list[str] = []
    for badge in _health_badges():
        source = HEALTH_SOURCES[badge.alt]
        link = urlsplit(badge.link)
        if (link.netloc, link.path) != (source.host, source.link_path):
            offenders.append(f"{badge.alt}: links to {badge.link}")
        elif _branch(badge.link) != BRANCH:
            offenders.append(f"{badge.alt}: branch {_branch(badge.link)!r} in {badge.link}")
    assert not offenders, offenders


def test_sonarcloud_urls_name_the_analysed_project() -> None:
    """Every SonarCloud URL names the project sonar-project.properties analyses."""
    expected = _sonar_project_key()
    offenders: list[str] = []
    for match in _URL.finditer(_readme()):
        url = urlsplit(match["url"])
        if url.netloc != SONAR_HOST:
            continue
        query = parse_qs(url.query)
        # Badge images name the project in `project=`, dashboard links in `id=`.
        project = next(iter(query.get("project", []) + query.get("id", [])), None)
        if project != expected:
            offenders.append(f"{match['url']}: project {project!r}, expected {expected!r}")
    assert not offenders, offenders


def test_the_readme_links_sonarclouds_branch_list() -> None:
    """Branches other than dev are reached through SonarCloud's branch list."""
    links = [
        match["url"]
        for match in _URL.finditer(_readme())
        if urlsplit(match["url"]).netloc == SONAR_HOST
        and urlsplit(match["url"]).path == "/project/branches_list"
    ]
    assert links, "README.md does not link SonarCloud's branch list"


def test_the_license_badge_matches_the_declared_licence() -> None:
    """The License badge reads pyproject.toml's `project.license` and links to LICENSE."""
    pyproject = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    declared = cast(str, cast(dict[str, object], pyproject["project"])["license"])
    badge = _badge("License")
    shown = _shields_message(badge.image)
    assert shown == declared, f"License badge reads {shown!r}, pyproject.toml {declared!r}"
    assert badge.link == "LICENSE", f"License badge links to {badge.link}"


def test_the_status_badge_matches_the_status_note() -> None:
    """The Status badge states the same status as the README's "Status:" note."""
    note = _STATUS_NOTE.search(_readme())
    if note is None:
        pytest.fail('README.md has no "**Status: <status>.**" note')
    shown = _shields_message(_badge("Status").image)
    assert shown == note["status"], f"Status badge reads {shown!r}, note {note['status']!r}"
