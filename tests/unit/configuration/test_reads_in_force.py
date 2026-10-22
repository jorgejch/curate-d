"""Reads of the policy and template set in force, as strict as a submission.

A version in force that does not read raises a `ConfigurationError` naming its kind and
each problem, not a bare `ValidationError`.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

import pytest
from pydantic import BaseModel

from curate_d.configuration.store import (
    ConfigurationError,
    ConfigurationRepository,
    DocumentValidator,
)
from curate_d.domain import ConfigurationKind
from curate_d.placement.policy import active_policy
from curate_d.rendering.planning import active_templates

_UNKNOWN_PROFILE: dict[str, object] = {
    "destinations": [
        {
            "name": "archive",
            "provider": "pcloud",
            "account": "1",
            "root_path": "/",
            "profile": "no-such-profile",
        }
    ],
    "rules": [],
    "retention": [],
}
_UNKNOWN_MEDIA_KIND: dict[str, object] = {"templates": [{"name": "x", "when": {"media_kind": "x"}}]}


@dataclass
class _Repository:
    """A configuration store holding one active document as version 2."""

    document: Mapping[str, object]

    async def ensure(
        self,
        kind: ConfigurationKind,
        default: BaseModel,
        validator: DocumentValidator,
        *,
        created_by: str,
    ) -> int:
        """Return the stored version; nothing is seeded."""
        return 2

    async def active(self, kind: ConfigurationKind) -> tuple[int, Mapping[str, object]]:
        """Return the stored document as version 2."""
        return 2, self.document


async def test_a_policy_in_force_naming_an_unknown_profile_raises_a_configuration_error() -> None:
    """The error names the policy kind and each problem."""
    repository = cast(ConfigurationRepository, _Repository(_UNKNOWN_PROFILE))

    with pytest.raises(ConfigurationError, match="invalid policy configuration") as raised:
        await active_policy(repository)

    assert raised.value.kind is ConfigurationKind.POLICY
    assert raised.value.problems


async def test_a_template_set_in_force_on_an_unknown_media_kind_raises_a_configuration_error() -> (
    None
):
    """The error names the templates kind and each problem."""
    repository = cast(ConfigurationRepository, _Repository(_UNKNOWN_MEDIA_KIND))

    with pytest.raises(ConfigurationError, match="invalid templates configuration") as raised:
        await active_templates(repository)

    assert raised.value.kind is ConfigurationKind.TEMPLATES
    assert raised.value.problems
