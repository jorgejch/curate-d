"""Hashing a file's content as it streams from the provider, without staging it.

For the paths that need a fingerprint but not the bytes: a full rescan re-reads every
file to confirm it still is what the ledger says, and has no use for a staged copy.
Memory stays at one chunk whatever the file's size.
"""

import hashlib

from pydantic import Field

from curate_d.domain import DomainModel, HashAlgorithm, StrongFingerprint
from curate_d.providers.access import ProviderFiles


class ContentDigests(DomainModel):
    """What reading a file's content established."""

    size_bytes: int = Field(ge=0)
    sha1: str = Field(min_length=40, max_length=40)
    sha256: str = Field(min_length=64, max_length=64)

    @property
    def fingerprint(self) -> StrongFingerprint:
        """The content fingerprint these bytes would have."""
        return StrongFingerprint(algorithm=HashAlgorithm.SHA256, digest=self.sha256)

    @property
    def as_reported(self) -> tuple[StrongFingerprint, ...]:
        """The digests, in the form a provider's reported digests take."""
        return (
            StrongFingerprint(algorithm=HashAlgorithm.SHA1, digest=self.sha1),
            self.fingerprint,
        )


async def hash_content(files: ProviderFiles, native_id: str) -> ContentDigests:
    """Stream a file's content through SHA-1 and SHA-256 in one pass.

    Raises:
        ProviderError: the provider could not deliver the content.
    """
    sha1 = hashlib.sha1()
    sha256 = hashlib.sha256()
    size = 0
    async with files.open_content(native_id) as chunks:
        async for chunk in chunks:
            sha1.update(chunk)
            sha256.update(chunk)
            size += len(chunk)
    return ContentDigests(size_bytes=size, sha1=sha1.hexdigest(), sha256=sha256.hexdigest())
