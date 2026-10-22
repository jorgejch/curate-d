"""A pickled domain model restores across fields added since it was written."""

import pickle

import pytest
from pydantic import Field

from curate_d.domain import (
    DestinationConfig,
    DomainModel,
    HashAlgorithm,
    Provider,
    StaleCheckpointError,
    StrongFingerprint,
    derive_content_object_id,
)
from curate_d.workflows.placement import PlacementRequest


class Inner(DomainModel):
    """A nested model."""

    label: str


class Checkpoint(DomainModel):
    """A model whose newer fields an old pickle lacks."""

    a: int
    b: int = 2
    c: tuple[str, ...] = ()
    d: dict[str, str] = Field(default_factory=dict)
    inner: Inner | None = None


def _restore(cls: type[DomainModel], state: dict[object, object]) -> DomainModel:
    """Restore an instance of `cls` from a pickled state, as `pickle.loads` does."""
    obj = cls.__new__(cls)
    obj.__setstate__(state)
    return obj


def _stale_state(model: DomainModel, *dropped: str, **extra: object) -> dict[object, object]:
    """Return `model`'s pickled state with `dropped` fields removed and `extra` keys added."""
    state = model.__getstate__()
    fields = {k: v for k, v in state["__dict__"].items() if k not in dropped}
    state["__dict__"] = fields | extra
    state["__pydantic_fields_set__"] = state["__pydantic_fields_set__"] - set(dropped)
    return state


def test_added_fields_read_as_their_defaults() -> None:
    """A field added since the pickle was written reads as its declared default."""
    restored = _restore(Checkpoint, _stale_state(Checkpoint(a=1), "b", "c", "d", "inner"))
    assert isinstance(restored, Checkpoint)
    assert (restored.a, restored.b, restored.c, restored.d, restored.inner) == (1, 2, (), {}, None)
    assert restored == Checkpoint(a=1)


def test_default_factory_gives_each_restore_a_fresh_value() -> None:
    """A default-factory field gets a new value per restore, never a shared one."""
    first = _restore(Checkpoint, _stale_state(Checkpoint(a=1), "d"))
    second = _restore(Checkpoint, _stale_state(Checkpoint(a=1), "d"))
    assert isinstance(first, Checkpoint)
    assert isinstance(second, Checkpoint)
    assert first.d == {}
    assert first.d is not second.d


def test_complete_pickle_round_trips_unchanged() -> None:
    """A pickle holding every field, a nested model included, restores equal."""
    original = Checkpoint(a=1, b=3, c=("x",), d={"k": "v"}, inner=Inner(label="n"))
    assert pickle.loads(pickle.dumps(original)) == original


def test_missing_required_field_raises_stale_checkpoint() -> None:
    """A pickle lacking a required field raises StaleCheckpointError naming it."""
    with pytest.raises(StaleCheckpointError, match="field\\(s\\) a existed") as raised:
        _restore(Checkpoint, _stale_state(Checkpoint(a=1), "a", "b"))
    assert raised.value.model.endswith(".Checkpoint")
    assert raised.value.missing == ("a",)


def test_retired_key_is_ignored() -> None:
    """A key no longer declared as a field is dropped and the model equals a fresh one."""
    restored = _restore(Checkpoint, _stale_state(Checkpoint(a=1, b=5), retired="old"))
    assert isinstance(restored, Checkpoint)
    assert not hasattr(restored, "retired")
    assert restored == Checkpoint(a=1, b=5)


def test_placement_request_pickled_before_policy_version_reads_none() -> None:
    """A PlacementRequest pickled before policy_version existed reads it as None."""
    fingerprint = StrongFingerprint(algorithm=HashAlgorithm.SHA256, digest="a" * 64)
    request = PlacementRequest(
        content_object_id=derive_content_object_id(str(fingerprint)),
        fingerprint=fingerprint,
        destination=DestinationConfig(
            name="archive", provider=Provider.PCLOUD, account="1", root_path="/"
        ),
        locator="photos/a.jpg",
        template_version="seed-1",
        size_bytes=1,
        policy_version=3,
    )
    restored = _restore(PlacementRequest, _stale_state(request, "policy_version"))
    assert isinstance(restored, PlacementRequest)
    assert restored.policy_version is None
    assert restored.locator == "photos/a.jpg"
