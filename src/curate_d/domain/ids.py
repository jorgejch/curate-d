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
DestinationId = NewType("DestinationId", UUID)

# Root namespace. Fixed for the life of the project: changing it renames every entity.
CURATE_D_NAMESPACE = UUID("6f2f7c8a-2b3d-5c41-9f0e-1d7a4b6c8e02")

_SOURCE_NAMESPACE = uuid5(CURATE_D_NAMESPACE, "source-object")
_CONTENT_NAMESPACE = uuid5(CURATE_D_NAMESPACE, "content-object")
_PLACEMENT_NAMESPACE = uuid5(CURATE_D_NAMESPACE, "placement")
_DESTINATION_NAMESPACE = uuid5(CURATE_D_NAMESPACE, "destination")

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


def normalize_root(root_path: str) -> str:
    """Return a destination root in its one canonical form.

    A trailing slash is dropped, except on "/" itself, so "/a/" and "/a" name one place.

    Raises:
        ValueError: the root is not absolute, or has an empty, "." or ".." segment, any of
            which would let two spellings name one place or a root escape itself.
    """
    if not root_path.startswith("/"):
        raise ValueError(f"root path {root_path!r} is not absolute")
    trimmed = root_path.rstrip("/")
    if not trimmed:
        return "/"
    segments = trimmed.split("/")[1:]
    if any(segment in {"", ".", ".."} for segment in segments):
        raise ValueError(f"root path {root_path!r} has an empty, '.' or '..' segment")
    return trimmed


def derive_destination_id(provider: str, account: str, root_path: str) -> DestinationId:
    """Derive the identity of a place copies are kept: an account's root at a provider.

    The policy's name for the destination is not part of it: a rename keeps the identity,
    and a name pointed at another account or root names a new destination.

    Raises:
        ValueError: the root path is not in a form `normalize_root` accepts.
    """
    return DestinationId(
        uuid5(_DESTINATION_NAMESPACE, _join(provider, account, normalize_root(root_path)))
    )


def derive_placement_id(
    content_object_id: ContentObjectId, destination_id: DestinationId
) -> PlacementId:
    """Derive the identity of one content object's copy at one destination identity."""
    return PlacementId(
        uuid5(_PLACEMENT_NAMESPACE, _join(str(content_object_id), str(destination_id)))
    )
