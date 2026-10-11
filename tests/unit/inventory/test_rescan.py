"""Full-rescan verdicts, and the streaming hasher they rest on."""

import hashlib
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import SecretStr

from curate_d.domain import (
    ChangeSignal,
    ContentObjectId,
    HashAlgorithm,
    Provider,
    SourceObject,
    SourceObjectState,
    StrongFingerprint,
)
from curate_d.ingest.hashing import ContentDigests, hash_content
from curate_d.inventory.rescan import RescanVerdict, apply_discrepancy, assess_rescan
from curate_d.providers.pcloud.files import PCloudFiles
from tests.stubs.pcloud import StubPCloud

_NOW = datetime(2026, 9, 29, tzinfo=UTC)


def _digests(content: bytes) -> ContentDigests:
    """What reading `content` would establish."""
    return ContentDigests(
        size_bytes=len(content),
        sha1=hashlib.sha1(content).hexdigest(),
        sha256=hashlib.sha256(content).hexdigest(),
    )


def _fingerprint(content: bytes) -> StrongFingerprint:
    """The recorded fingerprint of `content`."""
    return StrongFingerprint(
        algorithm=HashAlgorithm.SHA256, digest=hashlib.sha256(content).hexdigest()
    )


def _fingerprinted() -> SourceObject:
    """A source whose content was fingerprinted."""
    return SourceObject.build(
        provider=Provider.PCLOUD,
        account="a",
        native_id="1",
        path="/x",
        size_bytes=3,
        modified_at=_NOW,
        version_marker="m",
    ).model_copy(
        update={
            "state": SourceObjectState.FINGERPRINTED,
            "content_object_id": ContentObjectId(uuid4()),
        }
    )


def test_unchanged_content_is_confirmed() -> None:
    """Matching fingerprints need nothing but a stamp."""
    assert (
        assess_rescan(_fingerprinted(), _fingerprint(b"old"), _digests(b"old"))
        is RescanVerdict.CONFIRMED
    )


def test_changed_content_behind_the_same_marker_is_a_discrepancy() -> None:
    """The failure the marker was trusted not to have."""
    assert (
        assess_rescan(_fingerprinted(), _fingerprint(b"old"), _digests(b"new"))
        is RescanVerdict.DISCREPANCY
    )


def test_a_source_without_content_is_reingested_rather_than_compared() -> None:
    """Nothing recorded to compare with."""
    source = _fingerprinted().model_copy(update={"content_object_id": None})
    assert assess_rescan(source, None, _digests(b"x")) is RescanVerdict.UNFINGERPRINTED


def test_a_discrepancy_resets_the_source_for_reingest_with_the_observed_digests() -> None:
    """Back to discovered, unlinked, stamped, and carrying what was actually read."""
    updated = apply_discrepancy(_fingerprinted(), _digests(b"new!"), now=_NOW)
    assert updated.state is SourceObjectState.DISCOVERED
    assert updated.content_object_id is None
    assert updated.change_signal is ChangeSignal.CONTENT_FINGERPRINT
    assert updated.last_verified_at == _NOW
    assert updated.size_bytes == 4
    assert updated.reported(HashAlgorithm.SHA256) == _fingerprint(b"new!")
    assert updated.reported(HashAlgorithm.SHA1) is not None


async def test_the_hasher_streams_both_digests_in_one_read() -> None:
    """One content request yields size, SHA-1 and SHA-256."""
    pcloud = StubPCloud()
    content = bytes(range(256)) * 10_000
    stub = pcloud.file("/big.bin", content)
    async with pcloud.client() as http:
        files = PCloudFiles(
            http, api_host=pcloud.host, account="1", access_token=SecretStr(pcloud.access_token)
        )
        digests = await hash_content(files, str(stub.file_id))
    assert digests == _digests(content)
    assert pcloud.requests["content"] == 1
