"""The release pipeline's own shape.

CI produces tags here and Flux selects them in the GitOps repository. These tests pin the
tags this repository emits; the policies that select them are checked where they live.
"""

import hashlib
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
        r"dev-\d{14}-[0-9a-f]{7,12}-[0-9a-f]{12}", APPLICATION_VERSION
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


def test_the_dev_tag_sorts_by_commit_time_and_names_the_build_environment() -> None:
    """Flux's dev ImagePolicy orders `dev-<UTC timestamp>-<sha>-<hash>` tags by timestamp.

    The stamp is the commit's, not the build's: a build-time stamp gives one commit a new
    tag per build, and a re-run of an older commit's build a tag Flux would deploy. The
    hash names the system packages, the one image input the commit does not fix.
    """
    hashed, alias = (t for t in _emitted_tags() if t.startswith("type=raw"))
    assert (
        "dev-${{ steps.stamp.outputs.commit_time }}-{{sha}}-${{ steps.environment.outputs.hash }}"
        in hashed
    )
    assert "priority=300" in hashed, "the hashed tag must be `version`, the DBOS version"
    assert "value=dev-${{ steps.stamp.outputs.commit_time }}-{{sha}}," in alias
    assert "priority=200" in alias
    assert "{{date" not in hashed + alias


def test_the_commit_time_is_read_in_utc_and_only_on_dev() -> None:
    """`--date=format-local` renders in `TZ`, so the stamp is UTC whatever the runner's zone."""
    stamp = _step("stamp")
    run = cast(str, stamp["run"])
    assert "TZ=UTC git log -1 --format=%cd --date=format-local:%Y%m%d%H%M%S" in run
    assert '"$GITHUB_SHA"' in run
    assert "^[0-9]{14}$" in run
    assert stamp["if"] == "github.ref == 'refs/heads/dev'"
    assert _position(stamp) < _position(_build_step())


def _step(step_id: str) -> Document:
    """Return the build-and-push step with the given `id`."""
    return next(s for s in _build_steps() if s.get("id") == step_id)


def _position(step: Document) -> int:
    """Return the index of a step in the build-and-push job."""
    return _build_steps().index(step)


def _run_script(script: str, cwd: Path, env: dict[str, str], output: Path) -> tuple[int, str, str]:
    """Run a step's script with `bash -e`, as Actions does; return exit, output file, stderr."""
    output.write_text("", encoding="utf-8")
    result = subprocess.run(
        ["bash", "-e", "-c", script],
        cwd=cwd,
        env={**env, "GITHUB_OUTPUT": str(output)},
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    return result.returncode, output.read_text(encoding="utf-8"), result.stdout + result.stderr


def _isolated_git_env(home: Path) -> dict[str, str]:
    """Return an environment where git reads no caller, user or system state.

    Every `GIT_*` variable is dropped: under a git hook, `GIT_DIR` and `GIT_INDEX_FILE`
    point at the caller's repository, and git would write there. `HOME` and the config
    variables keep user and system config out, and the ceiling stops git from finding a
    repository above the copy.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    home.mkdir(exist_ok=True)
    env.update(
        {
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CEILING_DIRECTORIES": str(home.parent),
        }
    )
    return env


def _run_stamp_step(repository: Path, sha: str, runner_zone: str) -> tuple[int, str]:
    """Run the stamp step's script in `repository` as Actions would; return its exit and output."""
    env = {**_isolated_git_env(repository.parent / "home"), "GITHUB_SHA": sha, "TZ": runner_zone}
    code, output, _ = _run_script(
        cast(str, _step("stamp")["run"]), repository, env, repository.parent / "github_output"
    )
    return code, output


@pytest.fixture
def dated_commit(tmp_path: Path) -> tuple[Path, str]:
    """Return a throwaway copy of this repository with a commit dated 2026-11-10 23:01:14 -08:00.

    The copy is a clone, so the test never writes to the checkout it runs from, and it
    reads no user or system git config. The commit is the one the test needs on top of
    the copied head; the second item is its sha.
    """
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is not installed")
    env = _isolated_git_env(tmp_path / "home")
    repository = tmp_path / "repository"
    subprocess.run(
        [git, "clone", "--quiet", "--depth", "1", REPOSITORY_ROOT.as_uri(), str(repository)],
        env=env,
        check=True,
        timeout=120,
    )
    when = "2026-11-10T23:01:14-08:00"
    identity = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid"}
    identity |= {"GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    identity |= {"GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when}
    subprocess.run(
        [git, "commit", "--quiet", "--allow-empty", "--no-verify", "-m", "dated"],
        cwd=repository,
        env={**env, **identity},
        check=True,
        timeout=30,
    )
    sha = subprocess.run(
        [git, "rev-parse", "HEAD"],
        cwd=repository,
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout.strip()
    return repository, sha


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


def test_the_build_environment_is_listed_and_hashed_before_the_tags() -> None:
    """The hash is part of the tag, so the package list is built before metadata runs."""
    listing = next(s for s in _build_steps() if s.get("name") == _LISTING_STEP)
    listing_with = cast(Document, listing["with"])
    assert listing_with["target"] == "build-environment"
    assert listing_with["outputs"] == "type=local,dest=${{ runner.temp }}/build-environment"
    assert "push" not in listing_with
    hashing = _step("environment")
    for step in (listing, hashing):
        assert step["if"] == "github.ref == 'refs/heads/dev'"
    assert _position(listing) < _position(hashing) < _position(_build_step())


_LISTING_STEP = "List the system packages the build resolves"


def _run_hash_step(runner_temp: Path) -> tuple[int, str]:
    """Run the hash step's script against `runner_temp`; return its exit and output."""
    env = {"PATH": os.environ["PATH"], "RUNNER_TEMP": str(runner_temp)}
    code, output, _ = _run_script(
        cast(str, _step("environment")["run"]), runner_temp, env, runner_temp / "github_output"
    )
    return code, output


def test_the_hash_step_writes_the_package_lists_sha256_prefix(tmp_path: Path) -> None:
    """The step's hash is what `application_version` and the Dockerfile check compute."""
    packages = b"libpq5:amd64=17.6-0+deb13u1\ntesseract-ocr:amd64=5.5.0-1+b1\n"
    (tmp_path / "build-environment").mkdir()
    (tmp_path / "build-environment" / "build-environment").write_bytes(packages)
    code, output = _run_hash_step(tmp_path)
    assert code == 0
    assert output == f"hash={hashlib.sha256(packages).hexdigest()[:12]}\n"


@pytest.mark.parametrize("contents", [None, b""])
def test_the_hash_step_fails_without_a_package_list(tmp_path: Path, contents: bytes | None) -> None:
    """A missing or empty list must fail the build, never hash nothing into a tag."""
    (tmp_path / "build-environment").mkdir()
    if contents is not None:
        (tmp_path / "build-environment" / "build-environment").write_bytes(contents)
    code, output = _run_hash_step(tmp_path)
    assert code != 0
    assert output == ""


_FAKE_DOCKER = """#!/bin/sh
echo "$*" >> "$DOCKER_CALLS"
case "$FAKE_REGISTRY" in
  published) echo "Name: $4"; exit 0 ;;
  unpublished) echo "ERROR: $4: not found" >&2; exit 1 ;;
  *) echo "ERROR: failed to authorize: denied" >&2; exit 1 ;;
esac
"""


def _run_published_check(
    tmp_path: Path, ref: str, version: str, registry: str
) -> tuple[int, str, str]:
    """Run the publish check against a fake registry; return exit, output and docker calls."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(_FAKE_DOCKER, encoding="utf-8")
    docker.chmod(0o755)
    calls = tmp_path / "docker_calls"
    calls.write_text("", encoding="utf-8")
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "GITHUB_REF": ref,
        "GITHUB_REPOSITORY": "Owner/curate-d",
        "VERSION": version,
        "FAKE_REGISTRY": registry,
        "DOCKER_CALLS": str(calls),
    }
    code, output, _ = _run_script(
        cast(str, _step("published")["run"]), tmp_path, env, tmp_path / "github_output"
    )
    return code, output, calls.read_text(encoding="utf-8")


_HASHED_TAG = "dev-20261111070114-e69d801-0123456789ab"


@pytest.mark.parametrize(
    ("ref", "version", "checked"),
    [
        ("refs/heads/dev", _HASHED_TAG, "ghcr.io/owner/curate-d:dev-20261111070114-e69d801"),
        ("refs/tags/v0.1.1", "v0.1.1", "ghcr.io/owner/curate-d:v0.1.1"),
    ],
)
@pytest.mark.parametrize(
    ("registry", "published"), [("published", "true"), ("unpublished", "false")]
)
def test_the_publish_check_looks_up_the_commit_alias_or_the_release_tag(
    tmp_path: Path, ref: str, version: str, checked: str, registry: str, published: str
) -> None:
    """On dev the commit's alias decides, whatever the packages; on a release, its tag."""
    code, output, calls = _run_published_check(tmp_path, ref, version, registry)
    assert code == 0
    assert output == f"published={published}\n"
    assert calls == f"buildx imagetools inspect {checked}\n"


def test_the_publish_check_fails_when_the_registry_cannot_answer(tmp_path: Path) -> None:
    """Only "not found" means unpublished; a denied lookup must not publish over a tag."""
    code, output, _ = _run_published_check(tmp_path, "refs/heads/dev", _HASHED_TAG, "denied")
    assert code != 0
    assert output == ""


@pytest.mark.parametrize("version", ["", "0.1.1", "dev-20261111070114"])
def test_the_publish_check_refuses_a_tag_it_does_not_know(tmp_path: Path, version: str) -> None:
    """An empty or unexpected tag fails rather than checking the wrong name."""
    ref = "refs/heads/dev" if version.startswith("dev") else "refs/tags/v0.1.1"
    code, output, calls = _run_published_check(tmp_path, ref, version, "unpublished")
    assert code != 0
    assert output == ""
    assert calls == ""


def test_an_already_published_build_pushes_nothing() -> None:
    """The image is built and pushed only when the publish check found nothing."""
    published = _step("published")
    assert _position(_build_step()) < _position(published) < _position(_push_step())
    assert _push_step()["if"] == "steps.published.outputs.published == 'false'"


def test_two_runs_of_one_commit_wait_for_each_other() -> None:
    """Without this, two runs could both find a build unpublished and both push it."""
    concurrency = cast(Document, _workflow()["concurrency"])
    assert concurrency["group"] == "${{ github.workflow }}-${{ github.ref }}-${{ github.sha }}"
    assert concurrency["cancel-in-progress"] is False


def _push_step() -> Document:
    """Return the step that builds and pushes the image."""
    return next(
        s
        for s in _build_steps()
        if "build-push-action" in str(s.get("uses", "")) and s.get("name") != _LISTING_STEP
    )


def _dockerfile() -> str:
    """Return the Dockerfile's text."""
    return (REPOSITORY_ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")


def test_the_image_lists_its_system_packages_where_the_application_reads_them() -> None:
    """The listing stage, the runtime image and `BUILD_ENVIRONMENT_PATH` share one file."""
    from curate_d.runtime.dbos_app import BUILD_ENVIRONMENT_PATH

    dockerfile = _dockerfile()
    assert f"&& LC_ALL=C sort /tmp/packages > {BUILD_ENVIRONMENT_PATH} " in dockerfile
    assert f"COPY --from=system {BUILD_ENVIRONMENT_PATH} /build-environment" in dockerfile
    assert "FROM system AS runtime" in dockerfile


def test_the_image_build_fails_on_a_tag_hashed_from_other_packages() -> None:
    """A tag naming other packages fails the build, so nothing is pushed under it."""
    runtime = _dockerfile().split("AS runtime", 1)[1]
    check = runtime.split('ARG CURATE_D_BUILD_REVISION=""', 1)[1]
    assert "sha256sum /usr/share/curate-d/build-environment | cut -c1-12" in check
    assert '[ "${CURATE_D_BUILD_REVISION##*-}" = "${actual}" ]' in check
    assert "exit 1" in check


def test_only_a_dev_image_is_built_with_its_build_revision() -> None:
    """A dev image names its build in its DBOS version; a release image stays the tag."""
    build_args = cast(str, cast(Document, _push_step()["with"])["build-args"])
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
