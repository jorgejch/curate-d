"""Versioned configuration documents.

Every change adds a version and makes it active in one transaction; nothing is edited in
place. A document is validated before it is written, so an invalid change never becomes a
version, and the previous one stays active. Rendering and classification record the
version they used, which is how a later change stays distinguishable in the ledger.
"""

from collections.abc import Callable, Mapping
from typing import Final

from pydantic import AwareDatetime, BaseModel, Field, ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncConnection

from curate_d.domain import ConfigurationKind, DomainModel
from curate_d.ledger.mapping import Row, moment, number
from curate_d.ledger.tables import configurations

# Serializes activations of one kind: the advisory lock's second key is the kind's hash.
_LOCK_CLASS: Final = 0x63646366  # b"cdcf"

type DocumentValidator = Callable[[Mapping[str, object]], BaseModel]


class ConfigurationError(ValueError):
    """A configuration document failed validation. The active version is unchanged."""

    def __init__(self, kind: ConfigurationKind, problems: list[str]) -> None:
        """Name the kind and every problem found."""
        self.kind = kind
        self.problems = problems
        super().__init__(f"invalid {kind.value} configuration: " + "; ".join(problems))


class ConfigurationVersion(DomainModel):
    """One version of a configuration kind."""

    kind: ConfigurationKind
    version: int = Field(ge=1)
    active: bool
    created_at: AwareDatetime
    created_by: str


def check_document(
    kind: ConfigurationKind, document: Mapping[str, object], validator: DocumentValidator
) -> BaseModel:
    """Validate a document for `kind`, returning the parsed model.

    Raises:
        ConfigurationError: the document is not valid for `kind`.
    """
    try:
        return validator(document)
    except ValidationError as invalid:
        raise ConfigurationError(
            kind,
            [
                f"{'.'.join(str(part) for part in error['loc']) or '(root)'}: {error['msg']}"
                for error in invalid.errors()
            ],
        ) from None
    except ValueError as invalid:
        raise ConfigurationError(kind, [str(invalid)]) from None


def _version(row: Row, kind: ConfigurationKind) -> ConfigurationVersion:
    """Translate a configurations row."""
    active = row["active"]
    created_by = row["created_by"]
    return ConfigurationVersion(
        kind=kind,
        version=number(row, "version"),
        active=bool(active),
        created_at=moment(row, "created_at"),
        created_by=str(created_by),
    )


class ConfigurationRepository:
    """Reads and activates configuration versions on a caller-owned connection."""

    def __init__(self, connection: AsyncConnection) -> None:
        """Wrap a caller-owned connection; the repository never opens its own."""
        self._connection = connection

    async def active(self, kind: ConfigurationKind) -> tuple[int, Mapping[str, object]] | None:
        """The active version number and its document, or None if none was ever stored."""
        row = (
            (
                await self._connection.execute(
                    select(configurations.c.version, configurations.c.document).where(
                        configurations.c.kind == kind.value, configurations.c.active
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        document = row["document"]
        if not isinstance(document, Mapping):
            raise ConfigurationError(kind, ["stored document is not an object"])
        return number(row, "version"), document

    async def activate(
        self,
        kind: ConfigurationKind,
        document: Mapping[str, object],
        validator: DocumentValidator,
        *,
        created_by: str,
    ) -> int:
        """Validate `document`, store it as the next version, and make it active.

        Returns:
            The new version number.

        Raises:
            ConfigurationError: the document is invalid; nothing was written.
        """
        parsed = check_document(kind, document, validator)
        await self._connection.execute(
            text("SELECT pg_advisory_xact_lock(:class, hashtext(:kind))"),
            {"class": _LOCK_CLASS, "kind": kind.value},
        )
        latest = await self._connection.scalar(
            select(func.coalesce(func.max(configurations.c.version), 0)).where(
                configurations.c.kind == kind.value
            )
        )
        version = int(latest or 0) + 1
        await self._connection.execute(
            configurations.update()
            .where(configurations.c.kind == kind.value, configurations.c.active)
            .values(active=False)
        )
        await self._connection.execute(
            configurations.insert().values(
                kind=kind.value,
                version=version,
                document=parsed.model_dump(mode="json"),
                active=True,
                created_by=created_by,
            )
        )
        return version

    async def ensure(
        self,
        kind: ConfigurationKind,
        default: BaseModel,
        validator: DocumentValidator,
        *,
        created_by: str,
    ) -> int:
        """Store `default` as version 1 if the kind has never been configured.

        Returns:
            The active version.
        """
        existing = await self.active(kind)
        if existing is not None:
            return existing[0]
        return await self.activate(
            kind, default.model_dump(mode="json"), validator, created_by=created_by
        )

    async def history(self, kind: ConfigurationKind) -> list[ConfigurationVersion]:
        """Every version of a kind, newest first."""
        result = await self._connection.execute(
            select(configurations)
            .where(configurations.c.kind == kind.value)
            .order_by(configurations.c.version.desc())
        )
        return [_version(row, kind) for row in result.mappings()]
