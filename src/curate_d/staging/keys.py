"""Staging keys and their resolution within the staging root.

A key names one staged entry. It is derived from a source object identifier and never
from a provider-supplied path: the volume is shared between two trust levels, so a key
carrying provider-influenced text is the whole path-traversal risk expressed in one
value.
"""

from pathlib import Path
from typing import NewType
from uuid import UUID

from curate_d.domain.ids import SourceObjectId
from curate_d.staging.errors import StagingKeyError, StagingPathEscapeError

StagingKey = NewType("StagingKey", str)

# Entries shard on the first byte of the key. Hundreds of thousands of files in one
# directory is a shape that shared network filesystems handle badly.
_SHARD_LENGTH = 2


def derive_staging_key(source_object_id: SourceObjectId) -> StagingKey:
    """The staging key for one source object's fetched bytes.

    Keyed by source object rather than by content fingerprint because the fingerprint is
    not known until the bytes have been read, which is the very thing staging enables.
    """
    return StagingKey(source_object_id.hex)


def parse_staging_key(value: str) -> StagingKey:
    """Validate untrusted text as a staging key.

    Rejects rather than sanitizes. A value that is not already a derived identifier
    arrived from somewhere it should not have, and quietly cleaning it up would destroy
    the evidence of that while leaving the caller believing the key was legitimate.
    """
    try:
        parsed = UUID(hex=value)
    except (ValueError, AttributeError, TypeError) as error:
        raise StagingKeyError(
            f"staging key {value!r} is not a derived source object identifier"
        ) from error
    if parsed.hex != value:
        raise StagingKeyError(
            f"staging key {value!r} must be dashless lowercase hex; a formatted UUID and "
            f"its dashless form would otherwise name two entries for one source object"
        )
    return StagingKey(value)


def entry_path(root: Path, key: StagingKey) -> Path:
    """Where `key` lives under `root`, proven to be inside it.

    Key validation already makes a traversing key unrepresentable; this second check
    catches what validation cannot see, which is a symlink planted inside the root.
    """
    return require_within_root(root, root / key[:_SHARD_LENGTH] / key)


def require_within_root(root: Path, candidate: Path) -> Path:
    """Resolve `candidate` and refuse it if it lands outside `root`."""
    resolved_root = root.resolve()
    resolved = candidate.resolve()
    if not resolved.is_relative_to(resolved_root):
        raise StagingPathEscapeError(
            f"staging path {candidate} resolves to {resolved}, which is outside the "
            f"staging root {resolved_root}"
        )
    return resolved
