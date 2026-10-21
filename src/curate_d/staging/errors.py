"""Failures staging can produce.

Every one of these is named rather than generic because staging sits between two trust
levels: the credential-holding profile writes it, the decoding profile reads it. A
caller that cannot tell "the entry was never staged" from "the entry is unreadable" from
"the key tried to leave the root" will eventually paper over the third with a retry.
"""


class StagingError(RuntimeError):
    """Base for every staging failure, so a caller can catch the boundary as a whole."""


class StagingKeyError(StagingError, ValueError):
    """A staging key is not a derived source object identifier.

    Keys are derived, never provider-supplied. Anything else reaching this constructor
    means a provider path has been threaded into the staging layer, which is exactly the
    shape of the path-traversal risk the derived key exists to remove.
    """


class StagingPathEscapeError(StagingError):
    """A resolved staging path landed outside the staging root.

    The second line of defence behind key validation, and the one that catches a symlink
    planted inside the root rather than a malformed key.
    """


class StagedEntryMissingError(StagingError):
    """The requested entry is not staged.

    Explicit on purpose. The decoding profile holds no provider credential, so it has no
    way to make this good; a silent re-fetch here would mean the credential came back.
    """


class StagedEntrySupersededError(StagedEntryMissingError):
    """The entry is staged, but as a different version than the one asked for.

    A newer ingest of the same file replaced the registration. The caller's version is
    gone; the newer one has its own work queued.
    """


class StagedContentUnreadableError(StagingError):
    """A staged entry exists but its bytes could not be read or copied to local scratch.

    Distinct from a missing entry: a missing entry means the fetch never landed, this
    means content that is staged could not be read back by this process.
    """


class StagingWriteError(StagingError):
    """Fetched bytes could not be committed to the staging volume."""


class StagingCapacityExhaustedError(StagingError):
    """Staging or a reader's local scratch cannot hold the requested bytes.

    Raised when eviction cannot free enough staged space, when the process's scratch
    budget is spent, and when the scratch volume itself runs out. Surfaced rather than
    resolved: the alternatives are overwriting an entry another worker is decoding, or
    abandoning the fetch quietly. Both lose work with no record.
    """


class StagingLeaseError(StagingError):
    """A use lease could not be taken or recorded."""


class StagingObjectStoreError(StagingError):
    """The object store rejected or could not complete a staging request.

    Wraps the SDK's exception surface so callers depend on this boundary rather than on
    botocore's, which differs between the two backends staging must run against.
    """


class StagingIntegrityError(StagingError):
    """Retrieved bytes did not match the size and digest registered for them.

    Never recovered from in place. The registered digest is what the writer verified
    after upload, so a mismatch means either the object changed underneath its
    registration or the transfer was truncated; both invalidate the cache entry rather
    than justifying a retry against the same bytes.
    """


class StagingBucketMissingError(StagingObjectStoreError):
    """The configured staging bucket does not exist at the configured endpoint.

    A configuration fault, never an absence. Deliberately not a `StagedEntryMissingError`:
    the callers that treat a missing entry as success — `delete()`, and the smoke check
    that reads "the reader wrote nothing" out of it — would otherwise report a vanished
    bucket as freed capacity and as a verified security boundary.
    """


class StagingUploadMissingError(StagingObjectStoreError):
    """The multipart upload named by an abort is no longer held by the backend.

    Raised only from the upload-abort path, where it is the desired end state rather
    than a failure: a peer writer sweeping the same bucket got there first.
    """


class StagingTransientError(StagingObjectStoreError):
    """A staging request failed in a way a later attempt may resolve.

    Throttling, an endpoint-side 5xx, a clock skew rejection, a socket timeout. Kept
    apart from the fatal family so a caller can retry these and only these; flattening
    them together makes a throttle indistinguishable from a bad signature.
    """


class StagingReservationLostError(StagingTransientError):
    """An upload finished after its staging reservation expired or was removed.

    Nothing was registered: a reservation that is gone may already have had its bytes
    reclaimed, so publishing them could name an absent object. Transient because the
    next attempt re-uploads under a fresh reservation.
    """


class StagingAccessDeniedError(StagingObjectStoreError):
    """The configured identity is not permitted to perform the requested operation.

    Kept distinct from a missing object because the two are indistinguishable in a
    bucket the reader cannot list, and treating a denial as absence would turn a
    misprovisioned policy into an endless re-staging loop.
    """


class StagingNotConfiguredError(StagingError):
    """Signal absent staging configuration without selecting a fallback backend."""


class StagingRoleMismatchError(StagingError):
    """Signal a mismatch between worker profile and configured staging authority."""


class StagingNotReadyError(StagingObjectStoreError):
    """Signal that staging cannot serve this worker.

    The bucket or readiness marker is inaccessible to the worker identity, readiness did
    not answer in time, or the worker's staging loop is not running or would not stop.
    """
