"""One copy and its evidence, against a scripted control API."""

from collections.abc import Mapping

import httpx
import pytest

from curate_d.domain import HashAlgorithm, StrongFingerprint
from curate_d.placement.rclone import JobStatus, ObjectInfo, RcloneClient, RcloneError
from curate_d.placement.transfer import (
    ScratchExhaustedError,
    TransferFailedError,
    TransferSpec,
    copy_and_collect,
    transfer_config,
)
from curate_d.placement.verification import CorruptTransfer, Verdict, assess

_SHA1 = StrongFingerprint(algorithm=HashAlgorithm.SHA1, digest="a" * 40)
_SHA256 = StrongFingerprint(algorithm=HashAlgorithm.SHA256, digest="b" * 64)
_MD5 = StrongFingerprint(algorithm=HashAlgorithm.MD5, digest="c" * 32)


class ScriptedRclone(RcloneClient):
    """Answers from a script; records what was asked."""

    def __init__(
        self,
        *,
        error: str = "",
        objects: Mapping[str, ObjectInfo] | None = None,
        computed: Mapping[HashAlgorithm, StrongFingerprint] | None = None,
    ) -> None:
        """Fail the job with `error`, or finish it, and stat `objects` by path."""
        super().__init__(httpx.AsyncClient(), "http://rclone.invalid")
        self.error = error
        self.objects = dict(objects or {})
        self.computed = dict(computed or {})
        self.configs: list[Mapping[str, object]] = []
        self.hashed: list[tuple[str, HashAlgorithm, bool]] = []

    async def start_copy(
        self,
        src_fs: str,
        src_remote: str,
        dst_fs: str,
        dst_remote: str,
        *,
        group: str,
        config: Mapping[str, object],
    ) -> int:
        """Accept the copy."""
        self.configs.append(config)
        return 7

    async def job_status(self, job: int) -> JobStatus:
        """Finished, failed if scripted to."""
        return JobStatus(finished=True, success=not self.error, error=self.error)

    async def stat(self, fs: str, remote: str) -> ObjectInfo | None:
        """The scripted object at `fs` + `remote`."""
        return self.objects.get(f"{fs}{remote}")

    async def hashsum_file(
        self, fs: str, remote: str, algorithm: HashAlgorithm, *, download: bool = False
    ) -> StrongFingerprint:
        """The scripted digest, or the backend's refusal."""
        self.hashed.append((f"{fs}{remote}", algorithm, download))
        if algorithm not in self.computed:
            raise RcloneError("operations/hashsumfile", f"{algorithm.value} unsupported")
        return self.computed[algorithm]


def _spec(*, large: bool = False) -> TransferSpec:
    """src:/a.jpg to dst:/b/a.jpg."""
    return TransferSpec(
        source_fs="src:",
        source_path="/a.jpg",
        destination_fs="dst:",
        destination_path="/b/a.jpg",
        size_bytes=10,
        group="placement-x",
        large=large,
    )


async def test_a_copy_rejected_as_corrupt_is_evidence_with_both_values() -> None:
    """rclone's post-copy check deleted the copy; its two values are kept."""
    rclone = ScriptedRclone(
        error='corrupted on transfer: sha1 hashes differ src(src:) "aa11" vs dst(dst:) "bb22"'
    )
    evidence = await copy_and_collect(rclone, _spec())
    assert evidence.corrupt == CorruptTransfer(what="sha1", source="aa11", destination="bb22")
    assessment = assess(evidence)
    assert (assessment.verdict, assessment.source_value, assessment.destination_value) == (
        Verdict.MISMATCH,
        "aa11",
        "bb22",
    )


async def test_running_out_of_scratch_is_an_explicit_resource_error() -> None:
    """Not a generic failure: the caller waits and retries it."""
    rclone = ScriptedRclone(error="write /scratch/x: no space left on device")
    with pytest.raises(ScratchExhaustedError, match="scratch exhausted"):
        await copy_and_collect(rclone, _spec())


async def test_any_other_job_failure_is_a_transfer_failure() -> None:
    """Named, with rclone's message."""
    rclone = ScriptedRclone(error="couldn't connect: timeout")
    with pytest.raises(TransferFailedError, match="timeout") as raised:
        await copy_and_collect(rclone, _spec())
    assert not isinstance(raised.value, ScratchExhaustedError)


async def test_a_common_algorithm_needs_no_read_back() -> None:
    """Both stats report SHA-1: nothing is downloaded."""
    rclone = ScriptedRclone(
        objects={
            "src:a.jpg": ObjectInfo(size_bytes=10, hashes=(_SHA1,)),
            "dst:b/a.jpg": ObjectInfo(size_bytes=10, hashes=(_SHA1, _MD5)),
        }
    )
    evidence = await copy_and_collect(rclone, _spec())
    assert rclone.hashed == []
    assert evidence.destination.reported_as(HashAlgorithm.SHA1) == _SHA1


async def test_without_a_common_algorithm_the_copy_is_read_back() -> None:
    """The strongest algorithm the source reports that the destination can compute wins."""
    rclone = ScriptedRclone(
        objects={
            "src:a.jpg": ObjectInfo(size_bytes=10, hashes=(_SHA1, _SHA256)),
            "dst:b/a.jpg": ObjectInfo(size_bytes=10, hashes=(_MD5,)),
        },
        computed={HashAlgorithm.SHA1: _SHA1},
    )
    evidence = await copy_and_collect(rclone, _spec())
    assert rclone.hashed == [
        ("dst:b/a.jpg", HashAlgorithm.SHA256, True),
        ("dst:b/a.jpg", HashAlgorithm.SHA1, True),
    ]
    assert evidence.destination.computed == (_SHA1,)
    assert evidence.destination.unavailable == ("read-back sha256 failed: sha256 unsupported",)


async def test_a_missing_side_is_recorded_as_unavailable() -> None:
    """The copy vanished: the reason is evidence too."""
    rclone = ScriptedRclone(objects={"src:a.jpg": ObjectInfo(size_bytes=10, hashes=(_SHA1,))})
    evidence = await copy_and_collect(rclone, _spec())
    assert not evidence.destination.present
    assert evidence.destination.unavailable == ("copy not found at the destination",)
    assessment = assess(evidence)
    assert (assessment.verdict, assessment.compared, assessment.source_value) == (
        Verdict.MISMATCH,
        "presence",
        "10",
    )


async def test_a_source_gone_after_the_copy_is_sent_back() -> None:
    """Whatever was copied can no longer be tied to the fingerprinted source."""
    rclone = ScriptedRclone(objects={"dst:b/a.jpg": ObjectInfo(size_bytes=10, hashes=(_SHA1,))})
    assessment = assess(await copy_and_collect(rclone, _spec()))
    assert (assessment.verdict, assessment.reason) == (
        Verdict.SOURCE_CHANGED,
        "source not found after copy",
    )


def test_only_large_transfers_use_multiple_streams() -> None:
    """One transfer per call, with rclone's own check kept on."""
    assert transfer_config(_spec(large=True)) == {
        "Transfers": 1,
        "MultiThreadStreams": 4,
        "CheckSum": True,
    }
    assert transfer_config(_spec())["MultiThreadStreams"] == 0
