"""The local stack publishes on loopback only, and the image binds the API to every interface."""

from pathlib import Path
from typing import cast

import pytest
import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
COMPOSE_FILES = (REPOSITORY_ROOT / "compose.yaml", REPOSITORY_ROOT / "compose.lifecycle.yaml")
DOCKERFILE = REPOSITORY_ROOT / "docker" / "Dockerfile"

LOOPBACK = "127.0.0.1:"

type Document = dict[str, object]


class _ComposeLoader(yaml.SafeLoader):
    """Safe YAML loader that reads Compose's `!override` and `!reset` merge tags as plain nodes."""


def _construct_untagged(loader: yaml.SafeLoader, node: yaml.Node) -> object:
    """Construct a tagged node as the plain sequence, mapping or scalar it holds."""
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    return loader.construct_scalar(cast(yaml.ScalarNode, node))


for _tag in ("!override", "!reset"):
    _ComposeLoader.add_constructor(_tag, _construct_untagged)


def _published_ports() -> list[tuple[str, str, object]]:
    """Return (compose file, service, entry) for every `ports:` entry of the stack's files."""
    published: list[tuple[str, str, object]] = []
    for path in COMPOSE_FILES:
        compose = cast(Document, yaml.load(path.read_text(encoding="utf-8"), _ComposeLoader))
        services = cast(dict[str, Document], compose.get("services") or {})
        for service, definition in services.items():
            for entry in cast(list[object], definition.get("ports") or []):
                published.append((path.name, service, entry))
    return published


def test_the_stack_publishes_ports() -> None:
    """The loopback check below is not vacuous: the API and PostgreSQL still publish ports."""
    services = {service for _, service, _ in _published_ports()}
    assert {"api", "postgres"} <= services


@pytest.mark.parametrize(
    ("compose_file", "service", "entry"),
    _published_ports(),
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_every_published_port_is_loopback_only(
    compose_file: str, service: str, entry: object
) -> None:
    """Each published port binds 127.0.0.1 on the host, never every interface."""
    assert isinstance(entry, str) and entry.startswith(LOOPBACK), (
        f"{compose_file}: service {service!r} publishes {entry!r}; "
        f'write it as "{LOOPBACK}<host port>:<container port>"'
    )


def test_the_image_binds_the_api_to_every_interface() -> None:
    """The image sets CURATE_D_API_LISTEN_HOST, so compose and Kubernetes reach the API."""
    lines = [line.strip() for line in DOCKERFILE.read_text(encoding="utf-8").splitlines()]
    assert "ENV CURATE_D_API_LISTEN_HOST=0.0.0.0" in lines
