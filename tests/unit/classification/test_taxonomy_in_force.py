"""Reads of the taxonomy in force, as strict as a submission.

A version in force that does not read raises a `ConfigurationError` naming its kind and
each problem, not a bare `ValidationError`.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

import pytest
from pydantic import BaseModel

from curate_d.classification.taxonomy import active_taxonomy
from curate_d.configuration.store import (
    ConfigurationError,
    ConfigurationRepository,
    DocumentValidator,
)
from curate_d.domain import ConfigurationKind

_SLASHED_CATEGORY: dict[str, object] = {"categories": {"A/B": []}}


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


async def test_a_taxonomy_in_force_this_build_rejects_raises_a_configuration_error() -> None:
    """The error names the taxonomy kind and the rejected value."""
    repository = cast(ConfigurationRepository, _Repository(_SLASHED_CATEGORY))

    with pytest.raises(ConfigurationError, match="invalid taxonomy configuration") as raised:
        await active_taxonomy(repository)

    assert raised.value.kind is ConfigurationKind.TAXONOMY
    assert any("'A/B'" in problem for problem in raised.value.problems)
