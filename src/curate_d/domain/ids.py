"""Identifier derivation.

Every identifier is a UUIDv5 over the natural key of the thing it names, so the same
real-world entity always gets the same identifier. That is what lets an interrupted run
re-insert a row it may have already written without creating a second one.
"""

from typing import NewType
from uuid import UUID, uuid5

SourceObjectId = NewType("SourceObjectId", UUID)
ContentObjectId = NewType("ContentObjectId", UUID)
PlacementId = NewType("PlacementId", UUID)

# Root namespace. Fixed for the life of the project: changing it renames every entity.
CURATE_D_NAMESPACE = UUID("6f2f7c8a-2b3d-5c41-9f0e-1d7a4b6c8e02")

_SOURCE_NAMESPACE = uuid5(CURATE_D_NAMESPACE, "source-object")
_CONTENT_NAMESPACE = uuid5(CURATE_D_NAMESPACE, "content-object")
_PLACEMENT_NAMESPACE = uuid5(CURATE_D_NAMESPACE, "placement")

# Unit separator: cannot occur in a provider identifier, so no two distinct key tuples
# can flatten to the same string.
_SEPARATOR = "\x1f"


def _join(*parts: str) -> str:
    """Concatenate key parts with a separator that cannot appear in any part."""
    return _SEPARATOR.join(parts)


def derive_source_object_id(provider: str, account: str, native_id: str) -> SourceObjectId:
    """Derive the identity of a file as the provider knows it, stable across renames."""
    return SourceObjectId(uuid5(_SOURCE_NAMESPACE, _join(provider, account, native_id)))


def derive_content_object_id(fingerprint: str) -> ContentObjectId:
    """Derive the identity of the bytes themselves, stable across providers and paths."""
    return ContentObjectId(uuid5(_CONTENT_NAMESPACE, fingerprint))


def derive_placement_id(content_object_id: ContentObjectId, destination: str) -> PlacementId:
    """Derive the identity of one content object's copy at one destination."""
    return PlacementId(uuid5(_PLACEMENT_NAMESPACE, _join(str(content_object_id), destination)))
