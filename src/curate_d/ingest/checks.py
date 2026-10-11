"""Binding staged bytes to the file the scan listed.

The scan recorded what the provider reported — size, SHA-1, SHA-256 in pCloud's EU
region — without reading content. Staging then reads the content once and computes both
digests itself. If any reported value disagrees with what was read, the file changed
between listing and fetching, and what was staged is not the file the ledger describes:
it is rejected as a source change rather than fingerprinted under the old listing.
"""

from curate_d.domain import HashAlgorithm, SourceObject, StrongFingerprint


def source_change(source: SourceObject, *, size_bytes: int, sha1: str, sha256: str) -> str | None:
    """Describe how the staged bytes disagree with the listing, or None if they agree.

    Every reported value is checked, and a disagreement names both observed values.
    """
    differences: list[str] = []
    if size_bytes != source.size_bytes:
        differences.append(f"size listed {source.size_bytes}, staged {size_bytes}")
    for algorithm, staged in ((HashAlgorithm.SHA1, sha1), (HashAlgorithm.SHA256, sha256)):
        reported = source.reported(algorithm)
        if reported is not None and reported.digest != staged:
            differences.append(f"{algorithm.value} reported {reported.digest}, staged {staged}")
    if not differences:
        return None
    return "source changed since it was listed: " + "; ".join(differences)


def content_fingerprint(sha256: str) -> StrongFingerprint:
    """The content object's strong fingerprint: the SHA-256 staging computed and verified.

    Taken from the staging registration rather than hashed again on the CPU profile: the
    writer computed it over the bytes it uploaded, and every snapshot re-verifies it on
    open, so a second pass would only repeat that.
    """
    return StrongFingerprint(algorithm=HashAlgorithm.SHA256, digest=sha256)
