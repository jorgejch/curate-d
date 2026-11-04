"""The Dockerfile's pins: base images literal, tagged and digest-pinned, so Dependabot reads
them, and every `uv sync` locked and wheel-only, so no third-party build script runs."""

import re
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
DOCKERFILE = REPOSITORY_ROOT / "docker" / "Dockerfile"

_PLATFORM = re.compile(r"--platform=\S+\s+")
_FROM = re.compile(r"^FROM\s+(?P<ref>\S+)(\s+AS\s+\w+)?$")
_PINNED_REF = re.compile(r"[a-z0-9./-]+:[\w][\w.-]*@sha256:[0-9a-f]{64}")
_UV_SYNC = re.compile(r"\buv\s+sync\b")

# Build args that once chose the base image versions. Dependabot never evaluates `ARG`,
# so an interpolated `FROM` is invisible to it.
_DROPPED_ARGS = ("ARG PYTHON_VERSION", "ARG UV_VERSION")

# `--locked`: the install matches uv.lock or fails. `--no-build`: every third-party
# dependency comes from a wheel the lock pins, never from a source distribution's build
# script; the project itself is still built by its own backend.
_UV_SYNC_FLAGS = ("--locked", "--no-build")


def _lines() -> list[str]:
    """Return the Dockerfile's lines, stripped."""
    return [line.strip() for line in DOCKERFILE.read_text(encoding="utf-8").splitlines()]


def _from_lines() -> list[str]:
    """Return every `FROM` line of the Dockerfile."""
    return [line for line in _lines() if line.startswith("FROM")]


def _uv_sync_lines() -> list[tuple[int, str]]:
    """Return every `uv sync` line of the Dockerfile with its 1-based line number."""
    return [(number, line) for number, line in enumerate(_lines(), 1) if _UV_SYNC.search(line)]


def _image_ref(line: str) -> str | None:
    """Return the image reference of a `FROM` line, or None if the line has another shape."""
    match = _FROM.match(_PLATFORM.sub("", line, count=1))
    return match.group("ref") if match else None


def test_the_dockerfile_has_from_lines() -> None:
    """The pin check below is not vacuous: the Dockerfile still has base images to check."""
    assert _from_lines()


def test_every_from_image_is_a_literal_tag_and_digest() -> None:
    """Dependabot updates only `name:tag@sha256:<digest>` with no interpolation."""
    for line in _from_lines():
        ref = _image_ref(line)
        assert ref is not None, line
        assert "$" not in ref, line
        assert _PINNED_REF.fullmatch(ref), line


def test_no_build_arg_chooses_a_base_image_version() -> None:
    """The version args are gone, so nothing can bring an interpolated `FROM` back quietly."""
    for line in _lines():
        assert not line.startswith(_DROPPED_ARGS), line


def test_the_dockerfile_has_uv_sync_lines() -> None:
    """The flag check below is not vacuous: the image still installs through `uv sync`."""
    assert _uv_sync_lines()


def test_every_uv_sync_is_locked_and_wheel_only() -> None:
    """A `uv sync` without `--locked` and `--no-build` fails the suite, naming its line."""
    unpinned = [
        f"docker/Dockerfile:{number}: `{line}` lacks {', '.join(missing)}"
        for number, line in _uv_sync_lines()
        if (missing := [flag for flag in _UV_SYNC_FLAGS if flag not in line.split()])
    ]
    assert not unpinned, "\n".join(unpinned)
