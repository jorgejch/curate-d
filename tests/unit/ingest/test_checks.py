"""Staged bytes are checked against every value the listing reported."""

import hashlib
from datetime import UTC, datetime

from curate_d.domain import HashAlgorithm, Provider, SourceObject, StrongFingerprint
from curate_d.ingest.checks import content_fingerprint, source_change

_LISTED = b"listed content"


def _source(*algorithms: HashAlgorithm) -> SourceObject:
    """A source listing `_LISTED` with digests under `algorithms`."""
    digests = {
        HashAlgorithm.SHA1: hashlib.sha1(_LISTED).hexdigest(),
        HashAlgorithm.SHA256: hashlib.sha256(_LISTED).hexdigest(),
    }
    return SourceObject.build(
        provider=Provider.PCLOUD,
        account="a",
        native_id="1",
        path="/x",
        size_bytes=len(_LISTED),
        modified_at=datetime(2026, 1, 1, tzinfo=UTC),
        reported_digests=tuple(
            StrongFingerprint(algorithm=a, digest=digests[a]) for a in algorithms
        ),
    )


def _check(source: SourceObject, content: bytes) -> str | None:
    """Check `content` as if it had been staged for `source`."""
    return source_change(
        source,
        size_bytes=len(content),
        sha1=hashlib.sha1(content).hexdigest(),
        sha256=hashlib.sha256(content).hexdigest(),
    )


def test_matching_bytes_are_accepted() -> None:
    """Same size and digests: no change."""
    assert _check(_source(HashAlgorithm.SHA1, HashAlgorithm.SHA256), _LISTED) is None


def test_a_sha1_mismatch_names_both_values() -> None:
    """A US account reports only SHA-1, and it alone catches the change."""
    other = b"listed CONTENT"
    reason = _check(_source(HashAlgorithm.SHA1), other)
    assert reason is not None
    assert hashlib.sha1(_LISTED).hexdigest() in reason
    assert hashlib.sha1(other).hexdigest() in reason


def test_a_sha256_mismatch_is_caught_too() -> None:
    """An EU account's SHA-256 is checked as well."""
    reason = _check(_source(HashAlgorithm.SHA256), b"listed CONTENT")
    assert reason is not None
    assert "sha256 reported" in reason


def test_a_size_mismatch_is_caught_without_digests() -> None:
    """A provider reporting no digests still has its size checked."""
    reason = _check(_source(), b"longer than it was listed")
    assert reason is not None
    assert f"size listed {len(_LISTED)}" in reason


def test_the_content_fingerprint_is_the_staged_sha256() -> None:
    """No second hash: the staging SHA-256 is the fingerprint."""
    digest = hashlib.sha256(_LISTED).hexdigest()
    fingerprint = content_fingerprint(digest)
    assert fingerprint.algorithm is HashAlgorithm.SHA256
    assert fingerprint.digest == digest
