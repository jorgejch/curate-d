"""The Dockerfile's base images: literal, tagged and digest-pinned, so Dependabot reads them."""

import re
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
DOCKERFILE = REPOSITORY_ROOT / "docker" / "Dockerfile"

_PLATFORM = re.compile(r"--platform=\S+\s+")
_FROM = re.compile(r"^FROM\s+(?P<ref>\S+)(\s+AS\s+\w+)?$")
_PINNED_REF = re.compile(r"[a-z0-9./-]+:[\w][\w.-]*@sha256:[0-9a-f]{64}")

# Build args that once chose the base image versions. Dependabot never evaluates `ARG`,
# so an interpolated `FROM` is invisible to it.
_DROPPED_ARGS = ("ARG PYTHON_VERSION", "ARG UV_VERSION")


def _lines() -> list[str]:
    """Return the Dockerfile's lines, stripped."""
    return [line.strip() for line in DOCKERFILE.read_text(encoding="utf-8").splitlines()]


def _from_lines() -> list[str]:
    """Return every `FROM` line of the Dockerfile."""
    return [line for line in _lines() if line.startswith("FROM")]


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
