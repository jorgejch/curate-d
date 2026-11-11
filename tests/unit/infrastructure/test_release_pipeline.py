"""The release pipeline's own shape.

CI produces tags here and Flux selects them in the GitOps repository. These tests pin the
tags this repository emits; the policies that select them are checked where they live.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import cast

import pytest
import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "docker-publish.yml"

type Document = dict[str, object]

# PyYAML parses the bare `on:` key as the boolean True, per YAML 1.1, so the loaded
# mapping is keyed by object rather than by str.
type RawDocument = dict[object, object]


def _workflow() -> RawDocument:
    """Parse the docker-publish workflow YAML."""
    return cast(RawDocument, yaml.safe_load(WORKFLOW.read_text(encoding="utf-8")))


def _triggers() -> Document:
    """The workflow's `on.push` trigger configuration."""
    return cast(Document, cast(Document, _workflow()[True])["push"])


def _build_step() -> Document:
    """The metadata-action step in the build-and-push job."""
    jobs = cast(Document, _workflow()["jobs"])
    steps = cast(list[Document], cast(Document, jobs["build-and-push"])["steps"])
    return next(s for s in steps if "metadata-action" in str(s.get("uses", "")))


def _emitted_tags() -> list[str]:
    """The tag templates metadata-action is configured to emit."""
    with_block = cast(Document, _build_step()["with"])
    return [
        line.strip()
        for line in cast(str, with_block["tags"]).strip().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def test_only_the_dev_branch_triggers_a_branch_build() -> None:
    """main builds nothing by itself; a release is a tag on main.

    The trigger list carries this too; the job condition keeps a trigger added later from
    publishing a pull request or an untagged main commit.
    """
    jobs = cast(Document, _workflow()["jobs"])
    condition = cast(str, cast(Document, jobs["build-and-push"])["if"])
    assert "github.event_name == 'push'" in condition, "a pull request must never publish"
    assert "refs/heads/dev" in condition
    assert "startsWith(github.ref, 'refs/tags/')" in condition
    # An untagged main push matches neither ImagePolicy; publishing one would only cost
    # registry storage. Nothing in the condition admits main on its own.
    assert "refs/heads/main" not in condition


def test_a_pull_request_never_triggers_the_workflow() -> None:
    """The merge gate is checks.yml and integration.yml; this workflow only publishes."""
    on = cast(Document, _workflow()[True])
    assert set(on) == {"push"}


def test_dev_is_the_only_branch_push_that_triggers_the_workflow() -> None:
    """A push to main without a tag would build an image no policy selects."""
    assert _triggers()["branches"] == ["dev"]


def test_the_tag_trigger_is_the_semver_shape_not_a_wildcard() -> None:
    """`v*` also matches `v1` and `vfoo`, which build and then fail at the push step
    with an error that reads like a registry problem."""
    patterns = cast(list[str], _triggers()["tags"])
    assert patterns == ["v[0-9]+.[0-9]+.[0-9]+"]


@pytest.mark.parametrize("tag", ["v1", "vfoo", "v1.2", "v1.2.3-wip", "latest"])
def test_the_tag_trigger_rejects_non_semver_tags(tag: str) -> None:
    """The tag trigger's glob does not match a tag outside the semver shape."""
    # GitHub filter patterns: `+` means one or more of the preceding character, and
    # `.` is literal. Translated here so the assertion is about behaviour, not spelling.
    glob = cast(list[str], _triggers()["tags"])[0]
    assert not re.fullmatch(glob.replace(".", r"\.").replace("[0-9]+", r"[0-9]+"), tag)


def test_the_tag_trigger_accepts_a_release_tag() -> None:
    """The tag trigger's glob matches a genuine semver release tag."""
    glob = cast(list[str], _triggers()["tags"])[0]
    assert re.fullmatch(glob.replace(".", r"\."), "v10.2.31")


def _build_steps() -> list[Document]:
    """Return the steps of the workflow's `build-and-push` job."""
    jobs = cast(Document, _workflow()["jobs"])
    return cast(list[Document], cast(Document, jobs["build-and-push"])["steps"])


def _guard(name_fragment: str) -> Document:
    """Return the build-and-push step whose name contains `name_fragment`.

    Raises:
        AssertionError: no step matches.
    """
    step = next((s for s in _build_steps() if name_fragment in str(s.get("name", ""))), None)
    assert step is not None, f"no guard matching {name_fragment!r}"
    return step


def test_a_release_tag_must_be_reachable_from_main() -> None:
    """Git lets a tag be created on any commit; production must not accept one."""
    guard = _guard("release tag is on main")
    assert "--is-ancestor" in cast(str, guard["run"])
    assert cast(str, guard["if"]).startswith("startsWith(github.ref, 'refs/tags/')")


def test_a_release_tag_must_match_the_packaged_version() -> None:
    """DBOS records the packaged version against every workflow; a tag that disagreed
    would leave the release and its workflow records untraceable to each other."""
    guard = _guard("matches the packaged version")
    run = cast(str, guard["run"])
    assert "pyproject.toml" in run
    assert cast(str, guard["if"]).startswith("startsWith(github.ref, 'refs/tags/')")


def test_the_packaged_version_is_what_dbos_reports() -> None:
    """The check above is only meaningful if the code reads the same source.

    A dev image runs as its own tag instead; only a release image runs as the packaged
    version, which must match pyproject.
    """
    from curate_d.runtime.dbos_app import APPLICATION_VERSION

    pyproject = (REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    declared = next(
        line.split('"')[1] for line in pyproject.splitlines() if line.startswith("version = ")
    )
    assert declared == APPLICATION_VERSION or re.fullmatch(
        r"dev-\d{14}-[0-9a-f]{7,40}", APPLICATION_VERSION
    ), APPLICATION_VERSION


def test_the_release_guards_run_before_anything_reaches_the_registry() -> None:
    """A guard after the registry login still fails the run, but after the slow steps."""
    names = [str(s.get("name", "")) for s in _build_steps()]
    first_slow = next(i for i, n in enumerate(names) if "Log in" in n)
    for fragment in ("release tag is on main", "matches the packaged version"):
        assert next(i for i, n in enumerate(names) if fragment in n) < first_slow


def test_the_checkout_has_the_history_the_tag_guard_needs() -> None:
    """`git merge-base` against a shallow clone fails in a way that looks unrelated."""
    checkout = next(s for s in _build_steps() if "actions/checkout" in str(s.get("uses", "")))
    assert cast(Document, checkout["with"])["fetch-depth"] == 0


def test_the_dev_tag_is_not_gated_on_the_default_branch() -> None:
    """The dev tag names `dev`, so it survives the default branch moving to main."""
    dev_tag = next(t for t in _emitted_tags() if t.startswith("type=raw"))
    assert "is_default_branch" not in dev_tag
    assert "refs/heads/dev" in dev_tag


def test_build_and_push_is_the_only_job_and_waits_on_none() -> None:
    """The pull request already gated the merge; a test job here would only delay it."""
    jobs = cast(Document, _workflow()["jobs"])
    assert set(jobs) == {"build-and-push"}
    assert "needs" not in cast(Document, jobs["build-and-push"])


def test_the_dev_tag_sorts_by_commit_time() -> None:
    """Flux's dev ImagePolicy orders `dev-<UTC timestamp>-<sha>` tags; this is that shape.

    The stamp is the commit's, not the build's: a build-time stamp gives one commit a new
    tag per build, and a re-run of an older commit's build a tag Flux would deploy.
    """
    dev_tag = next(t for t in _emitted_tags() if t.startswith("type=raw"))
    assert "dev-${{ steps.stamp.outputs.commit_time }}-{{sha}}" in dev_tag
    assert "{{date" not in dev_tag


def test_the_commit_time_is_read_in_utc_and_only_on_dev() -> None:
    """`--date=format-local` renders in `TZ`, so the stamp is UTC whatever the runner's zone."""
    stamp = next(s for s in _build_steps() if s.get("id") == "stamp")
    run = cast(str, stamp["run"])
    assert "TZ=UTC git log -1 --format=%cd --date=format-local:%Y%m%d%H%M%S" in run
    assert '"$GITHUB_SHA"' in run
    assert "^[0-9]{14}$" in run
    assert stamp["if"] == "github.ref == 'refs/heads/dev'"
    names = [str(s.get("name", "")) for s in _build_steps()]
    assert names.index(str(stamp["name"])) < next(
        i for i, n in enumerate(names) if "container metadata" in n
    )


def _run_stamp_step(repository: Path, sha: str, runner_zone: str) -> tuple[int, str]:
    """Run the stamp step's script in `repository` as Actions would; return its exit and output."""
    stamp = next(s for s in _build_steps() if s.get("id") == "stamp")
    output = repository / "github_output"
    output.write_text("", encoding="utf-8")
    result = subprocess.run(
        ["bash", "-e", "-c", cast(str, stamp["run"])],
        cwd=repository,
        env={**os.environ, "GITHUB_SHA": sha, "GITHUB_OUTPUT": str(output), "TZ": runner_zone},
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    return result.returncode, output.read_text(encoding="utf-8")


@pytest.fixture
def dated_commit(tmp_path: Path) -> tuple[Path, str]:
    """Return a repository holding one commit made at 2026-11-10 23:01:14 -08:00, and its sha."""
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is not installed")
    when = "2026-11-10T23:01:14-08:00"
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
        "GIT_AUTHOR_DATE": when,
        "GIT_COMMITTER_DATE": when,
    }
    for args in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "dated"]):
        subprocess.run([git, *args], cwd=tmp_path, env=env, check=True, timeout=30)
    sha = subprocess.run(
        [git, "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True, check=True
    ).stdout.strip()
    return tmp_path, sha


@pytest.mark.parametrize("runner_zone", ["UTC", "America/Los_Angeles", "Asia/Tokyo"])
def test_the_stamp_step_writes_the_commit_time_in_utc(
    dated_commit: tuple[Path, str], runner_zone: str
) -> None:
    """Run the step itself: the same commit gives the same stamp in any runner zone."""
    repository, sha = dated_commit
    code, output = _run_stamp_step(repository, sha, runner_zone)
    assert code == 0
    assert output == "commit_time=20261111070114\n"


def test_the_stamp_step_fails_without_a_readable_commit(dated_commit: tuple[Path, str]) -> None:
    """An unreadable commit must fail the build, never publish a tag with an empty stamp."""
    repository, _ = dated_commit
    code, output = _run_stamp_step(repository, "0" * 40, "UTC")
    assert code != 0
    assert output == ""


def test_only_a_dev_image_is_built_with_its_build_revision() -> None:
    """A dev image names its build in its DBOS version; a release image stays the tag."""
    jobs = cast(Document, _workflow()["jobs"])
    steps = cast(list[Document], cast(Document, jobs["build-and-push"])["steps"])
    build = next(s for s in steps if "build-push-action" in str(s.get("uses", "")))
    build_args = cast(str, cast(Document, build["with"])["build-args"])
    assert (
        "CURATE_D_BUILD_REVISION=${{ github.ref == 'refs/heads/dev' "
        "&& steps.meta.outputs.version || '' }}"
    ) in build_args


def test_the_image_bakes_the_build_revision_into_its_environment() -> None:
    """The build arg only reaches the application if the runtime stage exports it."""
    dockerfile = (REPOSITORY_ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")
    runtime = dockerfile.split("AS runtime", 1)[1]
    assert 'ARG CURATE_D_BUILD_REVISION=""' in runtime
    assert "CURATE_D_BUILD_REVISION=${CURATE_D_BUILD_REVISION}" in runtime
