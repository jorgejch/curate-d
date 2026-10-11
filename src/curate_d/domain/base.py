"""Base class shared by every domain model."""

from pydantic import BaseModel, ConfigDict


class DomainModel(BaseModel):
    """Immutable, strictly validated value object.

    Frozen because domain records are facts about the world at a point in time: a
    correction is a new record with an audit entry, never a mutation of the old one.
    """

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,
        validate_default=True,
        use_enum_values=False,
    )
