"""Promotion extends the taxonomy in force, read under the taxonomy's lock."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import cast

from pydantic import BaseModel

from curate_d.classification.taxonomy import promote
from curate_d.configuration.store import ConfigurationRepository, DocumentValidator
from curate_d.domain import SEED_TAXONOMY, ConfigurationKind, RejectedValue, TaxonomyField


@dataclass
class _Repository:
    """A configuration store holding one taxonomy version, recording each call in order."""

    calls: list[str] = field(default_factory=list)

    async def lock(self, kind: ConfigurationKind) -> None:
        """Record the exclusive lock."""
        self.calls.append(f"lock {kind.value}")

    async def ensure(
        self, kind: ConfigurationKind, _default: BaseModel, _validator: object, *, created_by: str
    ) -> int:
        """Record the read; version 1 is stored."""
        self.calls.append(f"ensure {kind.value}")
        return 1

    async def active(self, kind: ConfigurationKind) -> tuple[int, Mapping[str, object]]:
        """Record the read and return the seed as version 1."""
        self.calls.append(f"active {kind.value}")
        return 1, SEED_TAXONOMY.model_dump(mode="json")

    async def activate(
        self,
        kind: ConfigurationKind,
        _document: Mapping[str, object],
        _validator: DocumentValidator,
        *,
        created_by: str,
    ) -> int:
        """Record the write; it becomes version 2."""
        self.calls.append(f"activate {kind.value}")
        return 2


async def test_promotion_locks_the_taxonomy_before_reading_the_version_in_force() -> None:
    """No taxonomy activation can land between the read and the extended version's write."""
    repository = _Repository()

    promoted = await promote(
        cast(ConfigurationRepository, repository),
        [RejectedValue(field=TaxonomyField.CATEGORY, value="Receipts")],
        promoted_by="test",
    )

    assert repository.calls[0] == "lock taxonomy"
    assert repository.calls[-1] == "activate taxonomy"
    assert promoted.version == 2
    assert promoted.taxonomy.accepts(TaxonomyField.CATEGORY, "Receipts")
