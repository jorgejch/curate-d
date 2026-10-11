"""Worker profiles.

Two deployables, split for isolation rather than concurrency. Per-stage concurrency and
per-provider rate limiting are already handled by the queue topology within a single
pool; what the split buys is something queue configuration cannot express:

- **Credential blast radius.** The I/O profile runs beside the rclone sidecar, which
  holds live provider credentials. The CPU profile decodes untrusted file content —
  image decoders and document parsers over bytes an attacker may have influenced. The
  CPU profile should hold only a read-only staging identity and have egress limited to
  what it needs, so a decoder compromise cannot reach the process that holds provider
  credentials.
- **Resource shape.** Kubernetes requests are per pod and cannot vary by which queue a
  workflow arrived on. A unified profile would request the union: cores for inference,
  local snapshot scratch, and an rclone sidecar on every replica whatever it does.

The queue lists here are the single source of truth; a deployment only names the profile
through `CURATE_D_WORKER_PROFILE`. The manifests that set it live in the GitOps repository,
and any check on them lives there.
"""

import os
from enum import StrEnum

from curate_d.runtime.queues import CPU_QUEUES, IO_QUEUES, QueueName

PROFILE_ENV = "CURATE_D_WORKER_PROFILE"


class WorkerProfile(StrEnum):
    """Which half of the pipeline a worker process serves."""

    IO = "io"
    CPU = "cpu"

    @property
    def queues(self) -> tuple[QueueName, ...]:
        """The stages this profile dequeues from. It may still enqueue to any."""
        return IO_QUEUES if self is WorkerProfile.IO else CPU_QUEUES

    @property
    def handles_untrusted_content(self) -> bool:
        """Whether this profile decodes bytes the system did not produce."""
        return self is WorkerProfile.CPU

    @property
    def holds_provider_credentials(self) -> bool:
        """Whether this profile runs beside the rclone sidecar."""
        return self is WorkerProfile.IO


class UnknownWorkerProfileError(ValueError):
    """The configured profile is not one this application deploys."""


def resolve_profile(value: str | None = None) -> WorkerProfile:
    """Read the profile from configuration, failing loudly on anything unexpected.

    No default: a worker that silently picks a profile would dequeue the wrong stages,
    and the symptom is a queue that never drains rather than an error.
    """
    raw = (value if value is not None else os.environ.get(PROFILE_ENV, "")).strip().lower()
    if not raw:
        raise UnknownWorkerProfileError(
            f"{PROFILE_ENV} is not set; expected one of "
            f"{', '.join(profile.value for profile in WorkerProfile)}"
        )
    try:
        return WorkerProfile(raw)
    except ValueError as error:
        raise UnknownWorkerProfileError(
            f"{PROFILE_ENV}={raw!r} is not a known profile; expected one of "
            f"{', '.join(profile.value for profile in WorkerProfile)}"
        ) from error
