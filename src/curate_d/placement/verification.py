"""The verification ladder, assessed from recorded evidence alone.

The transferring profile collects what each side reports — size, time, provider digests —
and, when the two sides share no algorithm, a digest it computes over the destination copy
read back in an algorithm the source reports. That evidence is recorded before anything
is assessed. The assessment here reads no content: it compares recorded values and assigns
the strongest rung they support.

    STRONG_COMMON_HASH   both sides report one algorithm, and the digests agree
    STRONG_IN_TRANSIT    a digest computed over the copy agrees with the source's
    WEAK                 only sizes (and times) could be compared, and they agree
    UNVERIFIABLE         nothing comparable was obtained

Any comparison that disagrees is a mismatch, whatever rung it was on: the copy is not
trusted, and both observed values are kept.

Before any of that, the source is checked against what was fingerprinted. A copy of a file
that changed after it was classified would verify perfectly against itself and still not
be the content the ledger describes.
"""

from enum import StrEnum

from pydantic import AwareDatetime, Field

from curate_d.domain import DomainModel, HashAlgorithm, StrongFingerprint, VerificationLevel


class SideEvidence(DomainModel):
    """What was observed about one side of a copy."""

    present: bool = Field(default=True, description="False when the object was not found.")
    size_bytes: int | None = None
    modified_at: AwareDatetime | None = None
    reported: tuple[StrongFingerprint, ...] = Field(
        default=(), description="Digests the provider reports, from its metadata."
    )
    computed: tuple[StrongFingerprint, ...] = Field(
        default=(), description="Digests computed over the bytes, read back."
    )
    unavailable: tuple[str, ...] = Field(
        default=(), description="Values that could not be obtained, and why."
    )

    def reported_as(self, algorithm: HashAlgorithm) -> StrongFingerprint | None:
        """The reported digest under `algorithm`, if any."""
        return next((d for d in self.reported if d.algorithm is algorithm), None)


class CorruptTransfer(DomainModel):
    """The two values the transfer service's post-copy check found to differ."""

    what: str = Field(description="`size`, or the hash algorithm compared.")
    source: str
    destination: str


class TransferEvidence(DomainModel):
    """Both sides of one copy, as recorded before assessment."""

    source: SideEvidence
    destination: SideEvidence
    source_version_marker: str | None = None
    corrupt: CorruptTransfer | None = Field(
        default=None,
        description="The transfer service's own post-copy rejection, with both values.",
    )


class Verdict(StrEnum):
    """The outcome of assessing a copy."""

    VERIFIED = "verified"
    MISMATCH = "mismatch"
    SOURCE_CHANGED = "source_changed"


class Assessment(DomainModel):
    """The rung reached, or why the copy cannot be registered."""

    verdict: Verdict
    level: VerificationLevel = VerificationLevel.UNVERIFIABLE
    compared: str | None = Field(default=None, description="What the level rests on.")
    source_value: str | None = None
    destination_value: str | None = None
    reason: str | None = None


# Preferred order when both sides share several algorithms: the strongest first.
_PREFERENCE = (HashAlgorithm.SHA256, HashAlgorithm.SHA1, HashAlgorithm.MD5)


def source_changed(
    evidence: TransferEvidence,
    *,
    fingerprinted_digests: tuple[StrongFingerprint, ...],
    fingerprinted_marker: str | None,
) -> Assessment | None:
    """A source-changed verdict if the source at copy time is not the fingerprinted one."""
    if (
        fingerprinted_marker is not None
        and evidence.source_version_marker is not None
        and evidence.source_version_marker != fingerprinted_marker
    ):
        return Assessment(
            verdict=Verdict.SOURCE_CHANGED,
            compared="version_marker",
            source_value=evidence.source_version_marker,
            destination_value=fingerprinted_marker,
            reason="the source's version marker changed after fingerprinting",
        )
    for recorded in fingerprinted_digests:
        current = evidence.source.reported_as(recorded.algorithm)
        if current is not None and not current.matches(recorded):
            return Assessment(
                verdict=Verdict.SOURCE_CHANGED,
                compared=recorded.algorithm.value,
                source_value=current.digest,
                destination_value=recorded.digest,
                reason=f"the source's reported {recorded.algorithm.value} changed after "
                "fingerprinting",
            )
    return None


def assess(evidence: TransferEvidence) -> Assessment:
    """The strongest rung the recorded evidence supports, or the mismatch it shows."""
    if evidence.corrupt is not None:
        corrupt = evidence.corrupt
        return Assessment(
            verdict=Verdict.MISMATCH,
            compared=f"transfer service {corrupt.what} check",
            source_value=corrupt.source,
            destination_value=corrupt.destination,
            reason=f"the transfer service rejected the copy: {corrupt.what} differs",
        )
    source, destination = evidence.source, evidence.destination
    if not source.present:
        return Assessment(
            verdict=Verdict.SOURCE_CHANGED,
            compared="presence",
            reason="; ".join(source.unavailable) or "the source is gone",
        )
    if not destination.present:
        return Assessment(
            verdict=Verdict.MISMATCH,
            compared="presence",
            source_value=None if source.size_bytes is None else str(source.size_bytes),
            reason="; ".join(destination.unavailable) or "the copy is not at the destination",
        )
    for algorithm in _PREFERENCE:
        ours, theirs = source.reported_as(algorithm), destination.reported_as(algorithm)
        if ours is None or theirs is None:
            continue
        if ours.matches(theirs):
            return Assessment(
                verdict=Verdict.VERIFIED,
                level=VerificationLevel.STRONG_COMMON_HASH,
                compared=algorithm.value,
                source_value=ours.digest,
                destination_value=theirs.digest,
            )
        return Assessment(
            verdict=Verdict.MISMATCH,
            compared=algorithm.value,
            source_value=ours.digest,
            destination_value=theirs.digest,
            reason=f"{algorithm.value} digests differ",
        )
    for computed in destination.computed:
        ours = source.reported_as(computed.algorithm)
        if ours is None:
            continue
        if ours.matches(computed):
            return Assessment(
                verdict=Verdict.VERIFIED,
                level=VerificationLevel.STRONG_IN_TRANSIT,
                compared=f"{computed.algorithm.value} read back",
                source_value=ours.digest,
                destination_value=computed.digest,
            )
        return Assessment(
            verdict=Verdict.MISMATCH,
            compared=f"{computed.algorithm.value} read back",
            source_value=ours.digest,
            destination_value=computed.digest,
            reason=f"{computed.algorithm.value} of the copy read back differs from the source's",
        )
    why = "; ".join((*source.unavailable, *destination.unavailable)) or (
        "no common digest, and none computed over the copy"
    )
    if source.size_bytes is not None and destination.size_bytes is not None:
        if source.size_bytes != destination.size_bytes:
            return Assessment(
                verdict=Verdict.MISMATCH,
                compared="size",
                source_value=str(source.size_bytes),
                destination_value=str(destination.size_bytes),
                reason="sizes differ",
            )
        return Assessment(
            verdict=Verdict.VERIFIED,
            level=VerificationLevel.WEAK,
            compared="size",
            source_value=str(source.size_bytes),
            destination_value=str(destination.size_bytes),
            reason=f"only size could be compared: {why}",
        )
    return Assessment(
        verdict=Verdict.VERIFIED,
        level=VerificationLevel.UNVERIFIABLE,
        reason=f"nothing could be compared: {why}",
    )


def expected_level(
    source_algorithms: frozenset[HashAlgorithm], destination_algorithms: frozenset[HashAlgorithm]
) -> VerificationLevel:
    """The rung a placement between two providers can reach, from their advertised hashes.

    A common algorithm gives the strong common-hash rung. Otherwise, as long as the source
    reports any algorithm, the copy can be read back and hashed in it.
    """
    if source_algorithms & destination_algorithms:
        return VerificationLevel.STRONG_COMMON_HASH
    if source_algorithms:
        return VerificationLevel.STRONG_IN_TRANSIT
    return VerificationLevel.WEAK
