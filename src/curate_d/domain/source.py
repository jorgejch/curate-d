"""The provider-native identity layer.

A source object is one file as one provider account knows it. Its identity survives
renames and moves within that provider, which is what makes an incremental rescan cheap.
"""

from enum import StrEnum
from typing import Self

from pydantic import AwareDatetime, Field, model_validator

from curate_d.domain.base import DomainModel
from curate_d.domain.fingerprint import HashAlgorithm, StrongFingerprint
from curate_d.domain.ids import ContentObjectId, SourceObjectId, derive_source_object_id


class Provider(StrEnum):
    """Storage providers the system can connect to.

    Only pCloud is in scope; other providers are deferred to follow-on changes.
    """

    PCLOUD = "pcloud"


class ChangeSignal(StrEnum):
    """How confidently the last change comparison could be made.

    Recorded per source object because a provider that supplies no version marker leaves
    the system comparing size and timestamp, which can miss an in-place edit.
    """

    VERSION_MARKER = "version_marker"
    SIZE_AND_MODIFIED_TIME = "size_and_modified_time"
    CONTENT_FINGERPRINT = "content_fingerprint"


class SourceObjectState(StrEnum):
    """Where a source object sits in the ingest pipeline."""

    DISCOVERED = "discovered"
    FINGERPRINTED = "fingerprinted"
    FAILED = "failed"
    EXCLUDED = "excluded"


class SourceObject(DomainModel):
    """One file at one provider account."""

    id: SourceObjectId
    provider: Provider
    account: str = Field(min_length=1, description="Provider account identifier.")
    native_id: str = Field(
        min_length=1,
        description="Provider-assigned file identifier; stable across renames.",
    )
    path: str = Field(min_length=1, description="Provider path at the last scan.")
    size_bytes: int = Field(ge=0)
    modified_at: AwareDatetime
    version_marker: str | None = Field(
        default=None,
        description="Provider-supplied change token; absent on providers that offer none.",
    )
    state: SourceObjectState = SourceObjectState.DISCOVERED
    content_object_id: ContentObjectId | None = Field(
        default=None,
        description="Set once the file's content has been fingerprinted.",
    )
    change_signal: ChangeSignal = ChangeSignal.VERSION_MARKER
    last_verified_at: AwareDatetime | None = Field(
        default=None,
        description="When a full rescan last confirmed the recorded fingerprint. "
        "Drives full-rescan resumption.",
    )
    failure_reason: str | None = None
    reported_digests: tuple[StrongFingerprint, ...] = Field(
        default=(),
        description="Digests the provider reports for the file, each with its algorithm. "
        "Obtained from provider metadata, never by reading content; what binds the staged "
        "bytes, and later the copy, to the file that was listed.",
    )

    @model_validator(mode="after")
    def _one_digest_per_algorithm(self) -> Self:
        """Reject two reported digests under one algorithm.

        Raises:
            ValueError: an algorithm appears more than once.
        """
        algorithms = [digest.algorithm for digest in self.reported_digests]
        if len(algorithms) != len(set(algorithms)):
            raise ValueError(f"one reported digest per algorithm, got {algorithms}")
        return self

    def reported(self, algorithm: HashAlgorithm) -> StrongFingerprint | None:
        """Return the provider-reported digest under `algorithm`, if there is one."""
        return next((d for d in self.reported_digests if d.algorithm is algorithm), None)

    @classmethod
    def build(
        cls,
        *,
        provider: Provider,
        account: str,
        native_id: str,
        path: str,
        size_bytes: int,
        modified_at: AwareDatetime,
        version_marker: str | None = None,
        reported_digests: tuple[StrongFingerprint, ...] = (),
    ) -> "SourceObject":
        """Construct with the identifier derived from the natural key."""
        return cls(
            id=derive_source_object_id(provider.value, account, native_id),
            provider=provider,
            account=account,
            native_id=native_id,
            path=path,
            size_bytes=size_bytes,
            modified_at=modified_at,
            version_marker=version_marker,
            reported_digests=reported_digests,
            change_signal=(
                ChangeSignal.VERSION_MARKER
                if version_marker is not None
                else ChangeSignal.SIZE_AND_MODIFIED_TIME
            ),
        )
