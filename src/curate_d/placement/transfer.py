"""One copy and the evidence it leaves, on the I/O profile.

The copy is one async `copyfile` job. When it finishes, both sides are stat'ed with
hashes; if they share no algorithm, the destination copy is read back and hashed in each
algorithm the source reports until one succeeds. Everything obtained, and everything that
could not be, goes into the evidence the verification step assesses. Nothing here decides
whether the copy is good.

The source is never written: every call on the source side is a read.
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Final

from pydantic import Field

from curate_d.domain import DomainModel, HashAlgorithm
from curate_d.placement.rclone import (
    ObjectInfo,
    RcloneClient,
    RcloneError,
    corrupted_transfer,
    out_of_space,
)
from curate_d.placement.verification import SideEvidence, TransferEvidence

# The algorithms tried, strongest first, when the copy has to be read back and hashed.
_READ_BACK_PREFERENCE: Final = (HashAlgorithm.SHA256, HashAlgorithm.SHA1, HashAlgorithm.MD5)


class TransferSpec(DomainModel):
    """One copy: where from, where to, and how big."""

    source_fs: str
    source_path: str
    destination_fs: str
    destination_path: str
    size_bytes: int = Field(ge=0)
    group: str = Field(description="Statistics group; the placement's workflow id.")
    large: bool = False


class TransferFailedError(RuntimeError):
    """The copy did not complete, for a reason other than a detected mismatch."""


class ScratchExhaustedError(TransferFailedError):
    """Local scratch ran out mid-transfer. Retry later; nothing is wrong with the file."""


def transfer_config(spec: TransferSpec) -> dict[str, object]:
    """Per-call overrides: one transfer at a time, multi-threading only for large files.

    `Transfers` is one because scheduling already happens per placement at the queue
    level; rclone's own parallelism would multiply with it.
    """
    return {
        "Transfers": 1,
        "MultiThreadStreams": 4 if spec.large else 0,
        "CheckSum": True,
    }


def _side(info: ObjectInfo | None, missing: str) -> SideEvidence:
    """Evidence from one stat."""
    if info is None:
        return SideEvidence(present=False, unavailable=(missing,))
    return SideEvidence(
        size_bytes=info.size_bytes, modified_at=info.modified_at, reported=info.hashes
    )


def _relative(path: str) -> str:
    """A remote path as rclone takes it within a remote: no leading separator."""
    return path.lstrip("/")


async def copy_and_collect(
    rclone: RcloneClient,
    spec: TransferSpec,
    *,
    on_progress: Callable[[int, int], Awaitable[None]] | None = None,
    poll_seconds: float = 0.25,
) -> TransferEvidence:
    """Copy one file and return the evidence both sides leave.

    Raises:
        ScratchExhaustedError: local scratch ran out; retry later.
        TransferFailedError: the copy failed for another reason.
        RcloneError: the control API refused a call.
    """
    job = await rclone.start_copy(
        spec.source_fs,
        _relative(spec.source_path),
        spec.destination_fs,
        _relative(spec.destination_path),
        group=spec.group,
        config=transfer_config(spec),
    )
    while True:
        status = await rclone.job_status(job)
        if status.finished:
            break
        if on_progress is not None:
            stats = await rclone.stats(spec.group)
            done, total = stats.get("bytes"), stats.get("totalBytes")
            await on_progress(
                done if isinstance(done, int) else 0,
                total if isinstance(total, int) and total else spec.size_bytes,
            )
        await asyncio.sleep(poll_seconds)
    if not status.success:
        corrupt = corrupted_transfer(status.error)
        if corrupt is not None:
            return TransferEvidence(
                source=SideEvidence(),
                destination=SideEvidence(
                    present=False, unavailable=("rclone deleted the corrupt copy",)
                ),
                corrupt=corrupt,
            )
        if out_of_space(status.error):
            raise ScratchExhaustedError(
                f"scratch exhausted copying {spec.source_path}: {status.error}"
            )
        raise TransferFailedError(f"copying {spec.source_path} failed: {status.error}")

    source = _side(
        await rclone.stat(spec.source_fs, _relative(spec.source_path)),
        "source not found after copy",
    )
    copied = await rclone.stat(spec.destination_fs, _relative(spec.destination_path))
    destination = _side(copied, "copy not found at the destination")
    if copied is None:
        return TransferEvidence(source=source, destination=destination)
    shared = {h.algorithm for h in source.reported} & {h.algorithm for h in destination.reported}
    if shared or not source.reported:
        if not source.reported:
            destination = destination.model_copy(
                update={"unavailable": (*destination.unavailable, "the source reports no digest")}
            )
        return TransferEvidence(source=source, destination=destination)

    reasons: list[str] = []
    offered = {h.algorithm for h in source.reported}
    for algorithm in (a for a in _READ_BACK_PREFERENCE if a in offered):
        try:
            computed = await rclone.hashsum_file(
                spec.destination_fs, _relative(spec.destination_path), algorithm, download=True
            )
        except RcloneError as error:
            reasons.append(f"read-back {algorithm.value} failed: {error.message}")
            continue
        destination = destination.model_copy(update={"computed": (computed,)})
        break
    if reasons:
        destination = destination.model_copy(
            update={"unavailable": (*destination.unavailable, *reasons)}
        )
    return TransferEvidence(source=source, destination=destination)
