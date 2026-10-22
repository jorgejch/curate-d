"""Base class shared by every domain model, and the error a stale pickle raises."""

from pydantic import BaseModel, ConfigDict


class StaleCheckpointError(Exception):
    """A pickled model lacks a required field its class declares today.

    DBOS pickles workflow inputs and step outputs, so a checkpoint written by an older
    build can reach a newer one. A field added since carries a default and is filled; a
    required one has nothing to fill it with.

    Attributes:
        model: The model class's module-qualified name.
        missing: The required fields the pickle lacks, sorted.
    """

    def __init__(self, model: str, missing: tuple[str, ...]) -> None:
        """Name the model and the required fields its pickle lacks."""
        self.model = model
        self.missing = missing
        super().__init__(
            f"{model} pickled before field(s) {', '.join(missing)} existed; "
            "redeploy the version that wrote it or drain it first"
        )


class DomainModel(BaseModel):
    """Immutable, strictly validated value object.

    Frozen because domain records are facts about the world at a point in time: a
    correction is a new record with an audit entry, never a mutation of the old one.

    Instances outlive the code that pickled them, in DBOS checkpoints. A field added to a
    model carries a default that means what the old code did; never retype or rename a
    field, retire it instead.
    """

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,
        validate_default=True,
        use_enum_values=False,
    )

    def __setstate__(self, state: dict[object, object]) -> None:
        """Restore a pickled model, filling declared defaults for fields added since it was written.

        Pydantic restores the stored `__dict__` as is, so a field declared after the pickle
        was written would otherwise raise `AttributeError` when read. A defaulted field
        gets its declared default, a fresh one from a default factory, without the
        validation construction runs on it. A key no longer declared as a field is a
        retired field and is ignored, as `parse_stored_policy` ignores one in a stored
        document.

        Raises:
            StaleCheckpointError: A required field is missing from the pickle. Defaulted
                fields are filled before it is raised.
        """
        super().__setstate__(state)
        cls = type(self)
        missing: list[str] = []
        for name, field in cls.model_fields.items():
            if name in self.__dict__:
                continue
            if field.is_required():
                missing.append(name)
            else:
                self.__dict__[name] = field.get_default(call_default_factory=True)
        if missing:
            raise StaleCheckpointError(
                f"{cls.__module__}.{cls.__qualname__}", tuple(sorted(missing))
            )
