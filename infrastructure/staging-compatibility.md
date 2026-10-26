# Staging S3 compatibility evidence matrix

What is actually known about the two backends the staging adapter must work against, and
what is not. One row per behavior the design depends on.

The local engine is **RustFS**, which replaced MinIO on 2026-09-26 (MinIO's open-source upstream is archived). MinIO evidence did not transfer: every local row
was re-earned against RustFS at its pinned digest, and the MinIO column is kept, labelled
superseded, as history.

**A local-engine pass is not evidence about OVH.** Each backend is recorded from its own run.
A skipped check is never recorded as a pass.

OVH rows marked `PASS (probe)` were executed on 2026-09-15 against a disposable bucket on
OVH, provisioned through the management API and destroyed afterwards. That
probe covered the wire-level behaviors an upload depends on; it did **not** cover the
runtime identity split, lifecycle expiry actually firing, or anything requiring two
scoped identities, so those rows were left `PENDING` and each was closed later by a named
check, dated in its cell.

| Target | Identity |
| --- | --- |
| RustFS server | `rustfs/rustfs:1.0.0@sha256:8cc9801755448b71a786705ce76692c77e14936cccd87cf2fc31842e58f4d1ff` (multi-arch index) |
| RustFS client | `rustfs/rc:v0.1.36@sha256:ab024bfebee49a750ce886b4c70963ccd9ddaa03f491704a90710641d7a26699` |
| RustFS endpoint | `http://127.0.0.1:9100`, path style, `s3v4`, region `us-east-1`; lifecycle observation on a separate time-scaled instance (`make lifecycle-check`) |
| RustFS evidence date | 2026-09-26, Docker Desktop, `linux/arm64`. Re-run 2026-09-27 against the running RustFS container at the pinned digest above, endpoint `http://127.0.0.1:9100`: `test_staging_contract.py` **20 passed**, `test_staging_backend_checks.py` **15 passed / 2 failed** (rows 30/31 PENDING, self-reported: no observation budget). `make lifecycle-check` the same day: rows 30/31 **2 passed** on the time-scaled instance |
| MinIO server (superseded) | `quay.io/minio/minio:RELEASE.2025-04-22T22-12-26Z@sha256:a1ea29fa28355559ef137d71fc570e508a214ec84ff8083e39bc5428980b015e` |
| MinIO client (superseded) | `quay.io/minio/mc:RELEASE.2025-04-16T18-13-26Z@sha256:aead63c77f9db9107f1696fb08ecb0faeda23729cde94b0f663edf4fe09728e3` |
| MinIO endpoint (superseded) | `http://127.0.0.1:9000`, path style, `s3v4`, region `us-east-1` |
| OVH endpoint | `https://s3.us-east-va.io.cloud.ovh.us`, **virtual-hosted** style, `s3v4`, region `us-east-va` |
| SDK | `aioboto3` 15.5.0 / `aiobotocore` 2.25.1 / `botocore` 1.40.61 |
| MinIO evidence date (superseded) | 2026-09-14 |
| OVH probe date | 2026-09-15, disposable bucket on OVH, deleted after the run |
| OVH documentation review date | 2026-09-13 (documentation only) |
| OVH suite run date | **2026-09-27**, bucket `curate-d-test-contract` (persistent contract-test bucket), region `US-EAST-VA` |
| OVH suite run result | Reader with `s3:GetObject` only: `test_staging_contract.py` 12 passed / 8 failed; `test_staging_backend_checks.py` 9 passed / 3 failed. With `s3:ListBucket` added: contract suite 19 passed / 1 failed (the `denied_reader` leg of row 21), backend checks 10 passed / 2 failed. After `denied_reader` moved to an explicitly denied third identity (row 21): `test_staging_contract.py` **20 passed**; `test_staging_backend_checks.py` **11 passed / 2 failed**. After the explicit bucket-management Deny (row 20): **32 passed / 2 failed**. Latest run, endpoint `https://s3.us-east-va.io.cloud.ovh.us`, SDK unchanged: `test_staging_contract.py` **20 passed**; `test_staging_backend_checks.py` **13 passed / 2 failed / 2 skipped**. Rows 30/31 fail only when run without an observation budget (self-reported PENDING). The skips are the local-only `test_reader_is_denied_another_bucket` and `test_neighbouring_keys_stay_denied`, not passes; OVH cross-bucket denial was exercised instead by the provisioning smoke check. Combined: 33 passed / 2 failed / 2 skipped in 150 s. |
| Python | 3.12.14 |

The two backends are **not** addressed the same way, and this is load-bearing rather than
cosmetic: OVH returns `AccessDenied` for path-style requests signed with `us-east-va`, and
only accepts path style when signed `us-east-1`. Virtual-hosted style works under any
region string. RustFS, like MinIO before it, is reached path style. Neither setting may be
inferred from the other.

Authoritative sources cited below:

- [RustFS S3 compatibility reference](https://docs.rustfs.com/en/reference/s3-compatibility)
- [RustFS lifecycle management](https://docs.rustfs.com/en/administration/data/lifecycle-management)
- [RustFS SSE-S3](https://docs.rustfs.com/en/security-compliance/encryption/sse-s)
- [RustFS IAM policies](https://docs.rustfs.com/en/security-compliance/iam/policies)

- [MinIO S3 API compatibility reference](https://github.com/minio/docs/blob/main/source/reference/s3-api-compatibility.rst)
- [MinIO KMS guide](https://github.com/minio/minio/blob/master/docs/kms/README.md)
- [OVH Object Storage compliancy table](https://support.us.ovhcloud.com/hc/en-us/articles/4603831309715-Object-Storage-Compliancy)
- [OVH lifecycle rules guide](https://support.us.ovhcloud.com/hc/en-us/articles/42696002771859-Object-Storage-Smart-storage-management-with-lifecycle-rules)
- [OVH Object Storage getting started](https://support.us.ovhcloud.com/hc/en-us/articles/4603838122643-Getting-started-with-Object-Storage)
- [OVH Object Storage FAQ](https://support.us.ovhcloud.com/hc/en-us/articles/23896851215379-Object-Storage-FAQ)

MinIO documentation URLs redirect to AIStor pages; they are kept for the superseded column
only. RustFS documentation, like MinIO's, is checked against the pinned release above, and
where it is silent the source at the `1.0.0` tag is cited instead (see
`docs/staging.md`, "Local engine: RustFS").

## RESOLVED 2026-09-15: the SDK's default checksum is accepted by every backend

`botocore` 1.40.61 defaults `request_checksum_calculation` to `when_supported`, so a plain
`put_object` carries a CRC32 checksum it was never asked for.

Nothing in the application asks for this. It is the SDK's default data-integrity behavior,
present since `botocore` 1.36, and it is more than an extra header — the request switches
to `aws-chunked` streaming with a trailing checksum:

```
X-Amz-Content-SHA256: STREAMING-UNSIGNED-PAYLOAD-TRAILER
X-Amz-Decoded-Content-Length: <n>
x-amz-sdk-checksum-algorithm: CRC32
```

- **RustFS accepts it.** `test_default_sdk_checksum_headers_are_accepted` green against the
  pinned server on 2026-09-26.
- **MinIO accepted it** (superseded). Observed on the wire against the pinned server on
  2026-09-14; every passing test in the historical MinIO-only suite carried these headers.
- **OVH accepts it.** Observed on the wire against OVH on 2026-09-15; the
  `PutObject` above succeeded and the object read back byte-identical. This checksum-specific
  blocker is closed; the identity, cleanup and other checks then pending have since run on
  OVH (rows below).

One caveat that survives the resolution: **OVH does not echo the checksum back.** `HEAD`
returns no `ChecksumCRC32`, so the stored checksum cannot be read back for verification.
Integrity therefore continues to rest on the application's own SHA-256, which is what the
design already requires — it is not a fallback introduced by this finding.

**Re-verify after any SDK upgrade.** The default changed once already; the header set and
the streaming mode are properties of the pinned `botocore` build, not of the code.


## How to read the matrix

- **Check** names the shared two-backend contract test, or, where the mechanism is
  deliberately backend-specific, the provisioning check that stands in for it. A check
  that does not exist yet is marked pending.
- **RustFS** is `PASS` only where the named check actually ran against the pinned server on
  2026-09-26. `PASS (time-scaled)` means the check ran on a separate instance whose
  lifecycle day was scaled to one second, with a real-day control that did not reclaim.
- **MinIO (superseded)** is the retired local engine's record, unchanged. It is history, not
  current evidence; checks marked *historical* existed only in the removed MinIO-only suite.
- **OVH** is `PASS (probe)` where the behavior was executed against OVH on
  2026-09-15, and `PASS` with a date where a named check ran against OVH
  later. A row not yet executed is `PENDING`; where the 2026-09-13 review recorded a
  documented claim, the cell says so and cites it, because documentation is not execution.
  As of 2026-09-28 every row is executed on OVH: 36 pass and row 18 records a
  provider behaviour. The 16 rows the probe left open are listed under "What the probe did not
  cover" below.
- Rows 30/31 run only with a wall-clock budget,
  `CURATE_D_TEST_STAGING_LIFECYCLE_TIMEOUT_SECONDS`. Without one, RustFS skips them and
  cites its recorded time-scaled observation, and OVH fails them as `PENDING` unless
  `CURATE_D_STAGING_LIFECYCLE_OBSERVATION` names its recorded observation. CI refuses a
  budget, so its RustFS runs skip both; a skip is not a pass.

## Core object operations

| # | Behavior | Check | RustFS | MinIO (superseded) | OVH | Design response |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | `PutObject`, single part, non-empty body | `test_writer_round_trips_content` | PASS — 2026-09-26, `rustfs/rustfs:1.0.0@sha256:8cc98017…`: `test_writer_round_trips_content` | PASS | PASS (probe) — executed against OVH 2026-09-15 | Same adapter, same request on both. |
| 2 | `PutObject`, zero-length body | `test_zero_length_content_round_trips` | PASS — 2026-09-26, `rustfs/rustfs:1.0.0@sha256:8cc98017…`: `test_zero_length_content_round_trips` | PASS | PASS (probe) — executed against OVH 2026-09-15 | Empty files are a spec requirement; no special-casing in the adapter. |
| 3 | `GetObject`, full body read | `test_writer_round_trips_content`, `test_reader_is_read_only`; historical `test_reader_can_read_staged_media` (MinIO only) | PASS — 2026-09-26, `rustfs/rustfs:1.0.0@sha256:8cc98017…`: `test_writer_round_trips_content`; reader GET leg of `test_reader_is_read_only` | PASS | PASS (probe) — executed against OVH 2026-09-15 | Reads are verified by size and SHA-256 before a decoder handle exists, so a partial body fails loudly on either backend. |
| 4 | `HeadObject` metadata retrieval | `test_storage_class_standard_is_accepted` (HEAD encryption and class); the adapter's pre-registration HEAD in `test_multipart_upload_completes_and_verifies`; historical `test_objects_are_encrypted_at_rest` (MinIO only) | PASS — 2026-09-26, `rustfs/rustfs:1.0.0@sha256:8cc98017…`: `test_storage_class_standard_is_accepted` (single and multipart), `test_multipart_upload_completes_and_verifies` | PASS | PASS (probe) — executed against OVH 2026-09-15 | HEAD is used for existence and encryption assertions only; registered size/digest live in the cache index, not in response metadata. |
| 5 | `DeleteObject` on an existing key | `staged_key` fixture teardown; contract fixture cleanup in `test_fixture_reclaims_completed_objects_and_abandoned_parts`; `rc object remove writer/...` in `infrastructure/rustfs/initialize.sh`; historical `mc rm writer/...` in the MinIO initialization | PASS — 2026-09-26, `rustfs/rustfs:1.0.0@sha256:8cc98017…`: backend-check teardown and `test_fixture_reclaims_completed_objects_and_abandoned_parts` (listing empty after deletes); `rustfs-init` probe removal | PASS | PASS (probe) — executed against OVH 2026-09-15 | Release is the normal cleanup path. Failure must be visible and retryable, never swallowed. |
| 6 | `DeleteObject` on an absent key succeeds, repeatedly | `test_deleting_an_absent_object_succeeds` | PASS — 2026-09-26, `rustfs/rustfs:1.0.0@sha256:8cc98017…`: `test_deleting_an_absent_object_succeeds` | PASS | PASS (probe) — executed against OVH 2026-09-15 | Cleanup retries delete unconditionally rather than checking first. If OVH errors instead, cleanup needs an explicit tolerated-error path. |
| 7 | `ListObjectsV2`, prefix-scoped | `test_listing_paginates`; `test_listing_the_content_prefix_yields_each_object_once` (at the prefix itself, marker present) | **PASS WITH A RECORDED DEVIATION** — 2026-09-26, `rustfs/rustfs:1.0.0@sha256:8cc98017…`: `test_listing_paginates`. 2026-09-27: at the prefix itself RustFS lists the zero-byte readiness marker `content/` twice; ordinary keys are listed once. `test_listing_the_content_prefix_yields_each_object_once` passes because the adapter skips the marker and yields each key once (upstream [rustfs/rustfs#8175](https://github.com/rustfs/rustfs/issues/8175)) | PASS | PASS (probe) — executed against OVH 2026-09-15 | Occupancy accounting and orphan reconciliation both depend on it; on RustFS the writer identity alone holds `s3:ListBucket` (on OVH the reader can list too, row 18). The adapter yields each listed key once, so an engine that repeats an entry cannot double-count occupancy. |
| 8 | `ListObjectsV2` pagination across continuation tokens | `test_listing_paginates` (`MaxKeys=1`); `test_listing_the_content_prefix_yields_each_object_once` (pages of 2 across the marker) | **PASS WITH A RECORDED DEVIATION** — 2026-09-26, `rustfs/rustfs:1.0.0@sha256:8cc98017…`: `test_listing_paginates` (`MaxKeys=1`, below the marker). 2026-09-27: at the prefix itself, `MaxKeys=1` returns only the marker and reports `IsTruncated=false`, so every other object is missed; pages of 2 or more list everything. `test_listing_the_content_prefix_yields_each_object_once` crosses pages of 2 (upstream [rustfs/rustfs#8175](https://github.com/rustfs/rustfs/issues/8175)) | PASS | PASS (probe) — executed against OVH 2026-09-15 | Pagination is mandatory: a truncated listing understates occupancy and would admit work past the bound. Uncertain occupancy must fail admission. The adapter lists at the default page size (1000) and must never page one key at a time at the content prefix while the RustFS deviation stands. |

## Multipart upload

| # | Behavior | Check | RustFS | MinIO (superseded) | OVH | Design response |
| --- | --- | --- | --- | --- | --- | --- |
| 9 | `CreateMultipartUpload` | `test_multipart_upload_completes_and_verifies` | PASS — 2026-09-26, `rustfs/rustfs:1.0.0@sha256:8cc98017…`: `test_multipart_upload_completes_and_verifies` (5242880, 5242881 and 10485761 bytes) | PASS | PASS (probe) — executed against OVH 2026-09-15 | Upload IDs are tracked by the writer so an interrupted transfer can always be aborted explicitly. |
| 10 | `UploadPart` at the 5 MiB minimum part size | `test_multipart_upload_completes_and_verifies` | PASS — 2026-09-26, `rustfs/rustfs:1.0.0@sha256:8cc98017…`: `test_multipart_upload_completes_and_verifies` (5242880, 5242881 and 10485761 bytes) | PASS | PASS (probe) — executed against OVH 2026-09-15 | Parts use bounded buffers at or above 5 MiB so completion genuinely exercises multipart rather than collapsing to one part. Confirm OVH's minimum independently. |
| 11 | `CompleteMultipartUpload` assembles the original bytes | `test_multipart_upload_completes_and_verifies` (SHA-256 of the read-back body) | PASS — 2026-09-26, `rustfs/rustfs:1.0.0@sha256:8cc98017…`: `test_multipart_upload_completes_and_verifies`, SHA-256 of the read-back body | PASS | PASS (probe) — executed against OVH 2026-09-15 | Upload computes the digest and verifies size/storage class with HEAD before registration; download verifies full-content SHA-256. |
| 12 | `AbortMultipartUpload` leaves no readable object | `test_an_aborted_upload_leaves_no_readable_object`, `test_explicit_abort_reclaims_residual_parts` | PASS — 2026-09-26, `rustfs/rustfs:1.0.0@sha256:8cc98017…`: `test_an_aborted_upload_leaves_no_readable_object` (failure and cancellation), `test_explicit_abort_reclaims_residual_parts` (`NoSuchUpload` after abort) | PASS | PASS — 2026-09-18 suite run against `curate-d-test-contract`: `test_explicit_abort_reclaims_residual_parts` green; `ListMultipartUploads` reported zero uploads after the run | Explicit abort on failure is the primary residual-storage mechanism on both backends; lifecycle is the backstop, not the plan. |
| 13 | `ListMultipartUploads` with `Prefix` set to the exact key | `test_fixture_reclaims_completed_objects_and_abandoned_parts` in the shared adapter suite; historical `test_incomplete_uploads_are_discoverable_for_reconciliation` | PASS — 2026-09-26, `rustfs/rustfs:1.0.0@sha256:8cc98017…`: `test_fixture_reclaims_completed_objects_and_abandoned_parts` (exact-key sweep). Discovery with a broad prefix was not exercised on RustFS; the adapter keeps the exact-key sweep | PASS | PASS (probe) — executed against OVH 2026-09-15 | MinIO required exact keys for reliable multipart discovery, and RustFS was not tested with a broad prefix, so the contract teardown still tracks keys and sweeps each one. Runtime broad-prefix abort may return zero while residual parts exist; the lifecycle abort rule (rows 28/31) is the backstop. |
| 14 | Multipart `ETag` is not a content digest | `test_multipart_upload_completes_and_verifies` (completion ETag carries a `-` suffix and differs from the SHA-256); historical `test_a_multipart_etag_is_not_a_content_digest` (MinIO only) | PASS — 2026-09-26, `rustfs/rustfs:1.0.0@sha256:8cc98017…`: `test_multipart_upload_completes_and_verifies` | PASS — differs from SHA-256 and carries a `-<partcount>` suffix | PASS (probe) — executed against OVH 2026-09-15 | Integrity never reads an ETag. `GetObjectAttributes` is not used either (see below). Application SHA-256 over a full read is the only accepted proof. |

## Identity and permissions

The provisioning mechanisms are deliberately different: RustFS uses admin identity policies
(`rc admin policy`, as MinIO did with `mc admin policy`), OVH uses user S3 policies, because
the 2026-09-13 review recorded the bucket policy APIs as unsupported on OVH. The *grants*
must match; the mechanism need not. RustFS needs one extra grant for the readiness marker
(row 16).

| # | Behavior | Check | RustFS | MinIO (superseded) | OVH | Design response |
| --- | --- | --- | --- | --- | --- | --- |
| 15 | Writer grant: Get/Put/Delete plus multipart management under `content/` | `test_writer_round_trips_content`, `test_multipart_upload_completes_and_verifies`, `test_workers_pass_readiness_at_the_content_prefix`; provisioning matrix in `infrastructure/rustfs/initialize.sh` | PASS — 2026-09-26, `rustfs/rustfs:1.0.0@sha256:8cc98017…`: the writer tests above and `test_workers_pass_readiness_at_the_content_prefix` (writer readiness: listing, PUT/GET/DELETE, multipart grants, the `content/` marker). **Deviation:** the grant also covers the one key `content` — see row 16 | PASS | PASS — 2026-09-18 suite run against `curate-d-test-contract`: `test_writer_round_trips_content`, `test_multipart_upload_completes_and_verifies` green against the applied writer policy | Same allow/deny matrix runs on both. Provisioning-specific: the OVH identities are created through the provider, and the provisioning smoke check exercises them. |
| 16 | Reader grant: `GetObject` under `content/` and nothing else | `test_reader_is_read_only`, `test_workers_pass_readiness_at_the_content_prefix`, `test_neighbouring_keys_stay_denied`; `rc object show reader/...` in `initialize.sh`; historical `test_reader_can_read_staged_media` (MinIO only) | **PASS WITH A RECORDED DEVIATION** — 2026-09-26, `rustfs/rustfs:1.0.0@sha256:8cc98017…`. RustFS evaluates a policy against the object key with its trailing slash removed, so the zero-byte readiness marker `content/` is checked as `content` and matches neither `content/*` nor an explicit `content/`. Both RustFS policies therefore also grant `arn:aws:s3:::curate-d-staging/content`, widening each identity by exactly that one key, which the application never writes. `test_workers_pass_readiness_at_the_content_prefix` (reader GET of the marker) failed before the grant and passes with it; `test_neighbouring_keys_stay_denied` pins that `contentx`, `content-other/…` and `content.<uuid>` stay `AccessDenied` for both identities | PASS | PASS — 2026-09-18 suite run against `curate-d-test-contract`: `test_reader_is_read_only` GET leg green against the applied reader policy | The grant bounds what the CPU credential can do, so the grant is what gets tested, not the code that uses it. RustFS's one-key widening is engine-specific provisioning (`infrastructure/rustfs/initialize.sh`); MinIO and OVH need no such grant. |
| 17 | Reader denied `PutObject` and `DeleteObject` | `test_reader_is_read_only`; denial probes in `initialize.sh`; historical `test_reader_cannot_write`, `test_reader_cannot_delete` (MinIO only) | PASS — `AccessDenied` on both — 2026-09-26, `rustfs/rustfs:1.0.0@sha256:8cc98017…`: `test_reader_is_read_only`, plus the `rustfs-init` access-matrix probes | PASS — `AccessDenied` | PASS — `AccessDenied` on both — 2026-09-18 suite run against `curate-d-test-contract`: `test_reader_is_read_only`, plus a manual PUT and DELETE probe, both `AccessDenied`, object confirmed surviving via writer HEAD | A reader that can write could introduce content for a decoder; a reader that can delete could destroy media another worker waits on. |
| 18 | Reader denied `ListBucket` | `test_reader_bucket_enumeration_matches_recorded_posture` (every backend; asserts `AccessDenied` on the local engine and a permitted listing on OVH); historical `test_reader_cannot_list_the_bucket` (MinIO only) | PASS — `AccessDenied` — 2026-09-26, `rustfs/rustfs:1.0.0@sha256:8cc98017…`: `test_reader_bucket_enumeration_matches_recorded_posture`; `rc object list reader/...` denied in `rustfs-init` | PASS — `AccessDenied` | **Provider behaviour, not a pass** — verified against OVH: a reader identity needs `s3:ListBucket` for a missing key to return `NoSuchKey` (404) instead of `AccessDenied` (403), and for `ListBucket` the policy Resource is the bucket ARN, not an object path. OVH does not support an `s3:prefix` condition, so the grant is bucket-wide: the reader can enumerate the whole bucket. A policy carrying an `s3:prefix` condition is accepted as an opaque JSON string and then ignored at evaluation, so it cannot be used to narrow the grant. The application's `_translate` in `src/curate_d/staging/object_store.py` maps `AccessDenied` to `StagingAccessDeniedError` and `NoSuchKey` to `StagedEntryMissingError` as sibling branches with no common ancestor below `StagingError`, so the 404 is what keeps a denial from being mistaken for absence. The test asserts the listing *succeeds* on OVH and is denied on the local engine, so revoking or widening the grant fails a test rather than going unnoticed. | The reader may hold `ListBucket` where the backend cannot distinguish a missing key from a denied one. The reader still fetches known keys from the cache index; nothing in the application lists as the reader. Revisit if OVH ships prefix conditions. |
| 19 | Both identities confined to the `content/` prefix | `test_a_missing_object_is_distinguishable_from_a_denial` (out-of-prefix `denied_reader`), `test_neighbouring_keys_stay_denied`, `test_reader_is_denied_another_bucket`; `rc object show reader/<bucket>/outside/object` in `initialize.sh`; historical `test_reader_cannot_escape_its_prefix`, `test_writer_is_also_confined_to_the_content_prefix` (MinIO only) | PASS — `AccessDenied` — 2026-09-26, `rustfs/rustfs:1.0.0@sha256:8cc98017…`: the out-of-prefix reader in `test_a_missing_object_is_distinguishable_from_a_denial`; reader and writer denied `contentx`, `content-other/…` and `content.<uuid>` in `test_neighbouring_keys_stay_denied`; reader denied its own key in another bucket in `test_reader_is_denied_another_bucket`. The one exception is the key `content` itself (row 16) | PASS — `AccessDenied` | PASS (confinement) — 2026-09-18 suite run against `curate-d-test-contract`: the out-of-prefix `denied_reader` (`outside-<uuid>/`) is denied as required. From 2026-09-22 the OVH `denied_reader` is the explicitly denied third identity instead (row 21); out-of-prefix confinement on OVH remains evidenced by the earlier run. Note: on OVH the reader's out-of-prefix denial is not *distinguishable* from an absent key, which is row 21's residual gap | Confining the writer too is what stops content landing where no reader may fetch it, which would otherwise look like a silent staging miss. |
| 20 | Neither runtime identity may `CreateBucket` | `test_neither_runtime_identity_can_create_a_bucket` (every backend); historical `test_neither_identity_can_create_a_bucket` (MinIO only) | PASS — `AccessDenied` — 2026-09-26, `rustfs/rustfs:1.0.0@sha256:8cc98017…`: `test_neither_runtime_identity_can_create_a_bucket`; `rc bucket create writer/...` denied in `rustfs-init` | PASS — `AccessDenied` | **PASS** — `test_neither_runtime_identity_can_create_a_bucket[ovh]` green for writer and reader once each identity carries an explicit `Deny` on bucket management (create/delete/configuration, object ACL/retention, `s3:ListAllMyBuckets`). Allow-only policies are not enough: an action a policy does not name falls back to OVH's default operator role, so the writer's `CreateBucket` succeeded until the explicit Deny was applied. The Deny can take a few minutes to take effect, and checks run sooner can observe the old policy. Writer and reader `ListBuckets` return `AccessDenied`. | Readiness depends on the bucket being pre-provisioned; a worker that can create one turns a misconfiguration into a silently empty bucket. |
| 21 | Absent key inside the grant returns 404, not `AccessDenied` | `test_a_missing_object_is_distinguishable_from_a_denial` | PASS — 2026-09-26, `rustfs/rustfs:1.0.0@sha256:8cc98017…`: `test_a_missing_object_is_distinguishable_from_a_denial`: `NoSuchKey` for the reader, `AccessDenied` for the out-of-prefix identity | PASS — `404`/`NoSuchKey`, distinct from row 19's `AccessDenied` | **PASS** — verified against OVH. Direct probe: reader GET on an absent `content/` key returns **`NoSuchKey` (404)** once the reader holds `s3:ListBucket` (it was `AccessDenied` (403) before the grant), and reader HEAD returns 404. `test_a_missing_object_is_distinguishable_from_a_denial` needs its out-of-prefix `denied_reader` to be a separate identity with an explicit `Deny` on `s3:*` for the bucket and `bucket/*` (`CURATE_D_TEST_OVH_DENIED_*`): with only bucket-wide `ListBucket`, an absent out-of-prefix key also returns `NoSuchKey`. With that identity the same absent `content/` key raises `StagedEntryMissingError` for the reader and `StagingAccessDeniedError` for the denied identity, and the test is green. | Cache-miss handling and permission failure must stay distinguishable: treating a denial as absence turns a misprovisioned policy into an endless re-staging loop. Now satisfied for in-prefix keys. The residual gap: a revoked reader credential is still distinguishable (auth fails outright), but an out-of-prefix key is no longer reported as denied. |
| 22 | Policy provisioning mechanism | `test_only_provisioned_identities_are_accepted`, `test_anonymous_requests_are_denied`, `test_reader_is_read_only`; backend-specific identity provisioning in `infrastructure/rustfs/initialize.sh` | PASS — 2026-09-26, `rustfs/rustfs:1.0.0@sha256:8cc98017…`: the three checks green; `rc admin user add` and `rc admin policy create/attach` idempotent on re-run and across a server restart  | PASS — `mc admin policy create/attach` and `mc admin user add` are idempotent on re-run | PASS — 2026-09-18 suite run against `curate-d-test-contract`: `test_only_provisioned_identities_are_accepted`, `test_anonymous_requests_are_denied`, `test_reader_is_read_only` all green against provisioned user S3 policies | Explicit, documented provisioning difference. Permitted because the observable access matrix is identical and tested separately on each backend. |

## Encryption

| # | Behavior | Check | RustFS | MinIO (superseded) | OVH | Design response |
| --- | --- | --- | --- | --- | --- | --- |
| 23 | Bucket-default SSE-S3 applies with no encryption header on the request | `test_storage_class_standard_is_accepted` (single and multipart), `test_provisioning_encryption_and_lifecycle` | PASS — 2026-09-26, `rustfs/rustfs:1.0.0@sha256:8cc98017…`: `test_storage_class_standard_is_accepted` (single and multipart, no encryption header sent, `AES256` returned); `test_provisioning_encryption_and_lifecycle` reads back the bucket default `AES256` | PASS | PASS — 2026-09-18 suite run against `curate-d-test-contract`: `test_storage_class_standard_is_accepted` (single and multipart) green; no encryption header sent, `AES256` returned | The adapter never sets an encryption header on either backend; encryption is bucket configuration. A backend that quietly dropped it would be invisible from the application side, which is why it is asserted on the response. |
| 24 | `HeadObject` reports `ServerSideEncryption: AES256` | `test_storage_class_standard_is_accepted` (single and multipart) | PASS — 2026-09-26, `rustfs/rustfs:1.0.0@sha256:8cc98017…`: `test_storage_class_standard_is_accepted` (single and multipart); same `AES256` spelling | PASS | PASS — 2026-09-18 suite run against `curate-d-test-contract`: `HeadObject` returns `ServerSideEncryption: AES256`, same spelling as MinIO | Assert the response, not the configuration. If OVH spells it differently, the assertion becomes backend-parameterized; it does not get dropped. Assert on `HeadObject`, never on the upload response: OVH's `CompleteMultipartUpload` response carries no `ServerSideEncryption` field even though the completed object is `AES256` (observed 2026-09-27 on OVH; `PutObject` does return it). |
| 25 | Encryption prerequisite fails closed | `test_rustfs_encryption_provisioning_fails_closed`; OVH management-state assertion in `test_provisioning_encryption_and_lifecycle`; historical `test_minio_encryption_provisioning_fails_closed` (MinIO only) | PASS — 2026-09-26, `rustfs/rustfs:1.0.0@sha256:8cc98017…`: with `RUSTFS_SSE_S3_MASTER_KEY` unset or malformed the server **starts** but refuses every SSE-S3 write (`InvalidRequest`), and `rustfs-init` exits 1 at its encrypted probe write. `test_rustfs_encryption_provisioning_fails_closed` pins that probe and its `AES256` readback in the script | PASS — server with a malformed `MINIO_KMS_SECRET_KEY` refuses to start (`kms: invalid key length 34`); `initialize.sh` exits 1 when KMS is absent | PASS — 2026-09-18 suite run against `curate-d-test-contract`: management readback in `test_provisioning_encryption_and_lifecycle` confirmed `sseAlgorithm: AES256`, versioning disabled, object lock disabled before the row-28 abort-filter assertion failed | Unsupported configuration fails visibly rather than silently storing staged media in the clear. Backend-specific mechanism, equivalent guarantee. On RustFS the guarantee rests on initialization's encrypted probe write, because the server itself does not refuse to start. |

## Storage class

| # | Behavior | Check | RustFS | MinIO (superseded) | OVH | Design response |
| --- | --- | --- | --- | --- | --- | --- |
| 26 | `PutObject` with `StorageClass="STANDARD"` is accepted | `test_storage_class_standard_is_accepted` in `test_staging_backend_checks.py` | PASS — 2026-09-26, `rustfs/rustfs:1.0.0@sha256:8cc98017…`: `test_storage_class_standard_is_accepted` single and multipart | PASS — named single and multipart checks executed on MinIO 2026-09-15 | PASS — 2026-09-18 suite run against `curate-d-test-contract`: `test_storage_class_standard_is_accepted` green for single and multipart against OVH | The adapter sends STANDARD explicitly on both. The named backend check ran on OVH on 2026-09-18, independently of the historical probe. |
| 27 | Storage class in the response / actual provisioned tier | `test_storage_class_standard_is_accepted`; actual OVH management-reported object class in `test_provisioning_encryption_and_lifecycle` | PASS — 2026-09-26, `rustfs/rustfs:1.0.0@sha256:8cc98017…`: `test_storage_class_standard_is_accepted`: HEAD reports STANDARD or omits the class (the assertion accepts either and did not record which), and `ListObjectsV2` reports `STANDARD` | PASS — HEAD omits `StorageClass` entirely; absence means STANDARD, MinIO does not echo the spelling back | PASS — 2026-09-18 suite run against `curate-d-test-contract`: management API reported the staged object as `storageClass: STANDARD` | HEAD absence means STANDARD. The historical probe covers response behavior; the actual tier comes from the separate management check, which also confirmed `STANDARD` on 2026-09-27. |

## Cleanup mechanisms

| # | Behavior | Check | RustFS | MinIO (superseded) | OVH | Design response |
| --- | --- | --- | --- | --- | --- | --- |
| 28 | Lifecycle `AbortIncompleteMultipartUpload` action | `test_provisioning_encryption_and_lifecycle`: local abort-rule readback (historically MinIO's rejection); OVH management lifecycle readback | **ACCEPTED AND ENFORCED** — 2026-09-26, `rustfs/rustfs:1.0.0@sha256:8cc98017…`: `test_provisioning_encryption_and_lifecycle` reads back the `rustfs-init` rule (`AbortIncompleteMultipartUpload` `DaysAfterInitiation: 1`, filter `content/`). Enforcement is row 31: observed on a time-scaled engine | **REJECTED** — `mc` refuses the rule: "The XML you provided was not well-formed or did not validate against our published schema". MinIO does not support the action | **PASS** — 2026-09-18 post-grant, bucket `curate-d-test-contract`: `test_provisioning_encryption_and_lifecycle` green. The `abort-incomplete-multipart-uploads` rule carries `filter.prefix == content/` alongside `abortIncompleteMultipartUpload`, which is what the check requires. Rule acceptance only — reclamation actually firing is row 31, observed 2026-09-28 | Resolved divergence: RustFS accepts the same action and prefix filter OVH carries, so the cleanup contract converges on one lifecycle mechanism. MinIO rejected the action and relied on explicit abort (row 12) plus server reclamation. OVH's backstop is provisioned, accepted and observed firing (row 31). |
| 29 | Lifecycle expiration rule accepted by the configuration API | `test_provisioning_encryption_and_lifecycle` | PASS — 2026-09-26, `rustfs/rustfs:1.0.0@sha256:8cc98017…`: `test_provisioning_encryption_and_lifecycle` reads back the `expire-staged-content` rule (prefix `content/`, 7 days) with the root identity | PASS — configured expiration read back on MinIO 2026-09-15 | PASS — 2026-09-18 suite run against `curate-d-test-contract`: the `expire-test-objects` rule (prefix `content/`, 1 day) was read back enabled through the management API; the abort-rule half of that run failed and passed post-grant the same day, see row 28 | Expiration is the orphan backstop, not the normal path. Application age-based cleanup is what actually bounds occupancy. |
| 30 | Expiration actually removing an object | `test_expiration_removes_an_aged_object` — requires explicit wall-clock observation budget. RustFS: `make lifecycle-check`. OVH: observed once, over two days, by a one-off seed/check harness | **PASS (time-scaled)** — 2026-09-26, `rustfs/rustfs:1.0.0@sha256:8cc98017…`: `make lifecycle-check`: a disposable RustFS with its lifecycle day **scaled to one second** (`RUSTFS_ILM_DEBUG_DAY_SECS=1`, `compose.lifecycle.yaml`); the rules are the unchanged ones `rustfs-init` imports. `test_expiration_removes_an_aged_object` observed the object removed 8–10 s after upload. Control: the same settings with a real day (`RUSTFS_ILM_DEBUG_DAY_SECS=86400`) removed nothing within a 60 s budget. Evidence that the rule fires, not of its wall-clock timing | **NOT A PASS** — the rule was accepted; no expiry was observed | **PASS** — seeded 2026-09-26T04:22:16Z in `curate-d-test-contract` (rule `expire-test-objects`, 1 day), with `HeadObject` returning an `Expiration` header. **Observed absent 2026-09-28T06:44:06Z**, endpoint `https://s3.us-east-va.io.cloud.ovh.us`: `test_expiration_removes_an_aged_object[ovh]` in the harness's seed/check mode, where the writer's `HeadObject` returned 404. Removed between one day after seeding and the check; the exact time was not observed. | Do not promise deletion at an exact hour on either backend. Readers treat an expired published object as a recoverable cache miss requiring a re-stage. |
| 31 | Server-side stale/orphan multipart reclamation without explicit abort | `test_stale_multipart_is_reclaimed` — requires explicit wall-clock observation budget. RustFS: `make lifecycle-check`. OVH: observed once, over two days, by a one-off seed/check harness | **PASS (time-scaled)** — 2026-09-26, `rustfs/rustfs:1.0.0@sha256:8cc98017…`: `make lifecycle-check`: a disposable RustFS with its lifecycle day **scaled to one second** (`RUSTFS_ILM_DEBUG_DAY_SECS=1`, `compose.lifecycle.yaml`); the rules are the unchanged ones `rustfs-init` imports, with the stale-upload loop at `1s` (`RUSTFS_API_STALE_UPLOADS_CLEANUP_INTERVAL`). `test_stale_multipart_is_reclaimed` observed a never-aborted 5 MiB upload reclaimed 2–4 s after initiation. The loop's own 24 h expiry was left at its default, so the reclamation came from the lifecycle rule. Control: a real day reclaimed nothing within 60 s. Under default settings the loop runs every 6 h | **NOT A PASS** — only explicit abort was exercised; the pinned release's stale-upload reclamation is unverified | **PASS** — seeded 2026-09-26T04:22:19Z in `curate-d-test-contract`: one 5 MiB part, never aborted (rule `abort-incomplete-multipart-uploads`, 1 day). **Observed reclaimed 2026-09-28T06:44:07Z**, endpoint `https://s3.us-east-va.io.cloud.ovh.us`: `test_stale_multipart_is_reclaimed[ovh]` in the harness's seed/check mode, where `ListParts` on the recorded upload ID returned `NoSuchUpload`. Reclaimed between one day after initiation and the check; the exact time was not observed. | Residual multipart storage is a separately reported and billed residual. Explicit abort remains the primary mechanism; the lifecycle rule is observed as the backstop on RustFS (time-scaled) and on OVH. |

## Wire format and SDK defaults

| # | Behavior | Check | RustFS | MinIO (superseded) | OVH | Design response |
| --- | --- | --- | --- | --- | --- | --- |
| 32 | Default `x-amz-checksum-crc32` and `x-amz-sdk-checksum-algorithm: CRC32` on every `PutObject` | `test_default_sdk_checksum_headers_are_accepted` in `test_staging_contract.py` | PASS — 2026-09-26, `rustfs/rustfs:1.0.0@sha256:8cc98017…`: `test_default_sdk_checksum_headers_are_accepted` under botocore 1.40.61 | PASS — named adapter test executed on MinIO 2026-09-15; safe checksum headers recorded in JUnit | PASS — 2026-09-18 suite run against `curate-d-test-contract`: `test_default_sdk_checksum_headers_are_accepted` green under botocore 1.40.61 (supersedes the 2026-09-15 probe) | Default botocore 1.40.61 behavior. Existing OVH probe acceptance is preserved; rerun the shared suite on both after SDK changes. |
| 33 | `X-Amz-Content-SHA256` payload signing and `aws-chunked` streaming trailers | Same as row 32 | PASS — 2026-09-26, `rustfs/rustfs:1.0.0@sha256:8cc98017…`: same test | PASS — named checksum test captured a hex payload hash and CRC32 headers on MinIO 2026-09-15 | PASS — 2026-09-18 suite run against `curate-d-test-contract`: same test green; OVH still does not echo the checksum back on `HEAD` | Streaming defaults are a property of the pinned SDK. If OVH requires `UNSIGNED-PAYLOAD` or rejects trailers, it becomes explicit adapter configuration tested on both, not a per-backend code branch. |
| 34 | SigV4 (`signature_version="s3v4"`) | Implicit in every test in the shared and backend-specific suites; the client config sets it explicitly | PASS — 2026-09-26, `rustfs/rustfs:1.0.0@sha256:8cc98017…`: every request in both suites signed `s3v4` | PASS | PASS — 2026-09-18 suite run against `curate-d-test-contract`: every request in both suites signed `s3v4`; 21 OVH tests transacted successfully | Signing version is configuration, identical on both. An OVH failure here would surface on the first request and in the provisioning smoke check. |
| 35 | Signing region | Implicit in every local-engine test (`us-east-1`) | PASS — 2026-09-26, `rustfs/rustfs:1.0.0@sha256:8cc98017…`: `us-east-1` throughout both suites | PASS | PASS — 2026-09-18 suite run against `curate-d-test-contract`: region `us-east-va` signed successfully against its own endpoint throughout both suites | Region is configuration. The value differs between backends by design; what must be verified is that the configured value signs successfully against its own endpoint. |

## Addressing and transport

| # | Behavior | Check | RustFS | MinIO (superseded) | OVH | Design response |
| --- | --- | --- | --- | --- | --- | --- |
| 36 | Addressing style | Implicit in every local-engine test (`addressing_style: path`) | PASS — 2026-09-26, `rustfs/rustfs:1.0.0@sha256:8cc98017…`: path style against `http://127.0.0.1:9100` | PASS — path style | PASS — 2026-09-18 suite run against `curate-d-test-contract`: virtual-hosted style over certificate-verified HTTPS across both suites | `CURATE_D_STAGING_S3_ADDRESSING_STYLE` is configuration, not a code path. Because the two backends run different values, neither value is evidence for the other; each must pass against its own endpoint. |
| 37 | TLS with certificate verification | `test_endpoint_transport_and_certificate_trust` | **N/A** for certificate rejection — RustFS runs plain HTTP on loopback by design (with `RUSTFS_TLS_PATH` it is HTTPS-only, checked 2026-09-26) and passes the check's explicit plaintext branch; certificate verification is a client property, proven against OVH. | **N/A** — plain HTTP on loopback; passed the plaintext branch | PASS — 2026-09-18 suite run against `curate-d-test-contract`: `test_endpoint_transport_and_certificate_trust` green, including unrelated-CA rejection against OVH | Settings reject HTTP with verification enabled, but currently accept HTTPS with verification disabled. That stricter validation was left unchanged by request. Remote test fixtures always enforce verified HTTPS and assert rejection using an unrelated valid CA. Production must retain verification. |

## Deliberately not relied on

Recorded so nobody adds them later without re-entering this matrix:

- `GetObjectAttributes` — MinIO lists it, the 2026-09-13 review recorded OVH as not
  supporting it. Integrity uses an application SHA-256 over a full read instead.
- Bucket policy APIs — recorded as unsupported on OVH; identities are provisioned per
  backend (row 22).
- Object versioning and Object Lock — off by design; they impose minimum-duration charges
  on content whose whole purpose is to be short-lived.
- Conditional writes / `If-None-Match` — the design coordinates through PostgreSQL advisory
  locks and immutable attempt keys precisely so no undocumented conditional-write behavior
  is load-bearing.

## What the 2026-09-15 OVH probe established beyond the rows

Four findings that came out of the probe and change what must be built, rather than only
confirming a row.

**A bucket created without an explicit encryption block is stored in plaintext.** The OVH
management API reported `encryption: {"sseAlgorithm": "plaintext"}` for a bucket created
with no encryption argument. AES256 is not a default and is not inherited. Provisioning must set `encryption { sse_algorithm = "AES256" }` (or the equivalent) on every staging bucket, or staged media is unencrypted
at rest while nothing in the S3 API reports a problem.

**Encryption and versioning are unreachable over the S3 API.** `PutBucketEncryption`,
`GetBucketEncryption` and `GetBucketVersioning` all return `AccessDenied` even to an
identity holding `s3:*` on the bucket, and `GetBucketPolicy` returns `NotImplemented`.
These are management-API concerns, so they are configured through the management API and verified there — not by the contract suite. This confirms the design's decision to use
OVH user S3 policies rather than bucket policies. Versioning reported `disabled` by
default, which is what the design requires.

**Path-style addressing is region-sensitive on OVH.** Path style signed `us-east-va`
returns `AccessDenied`; signed `us-east-1` it works. Virtual-hosted style works under any
region string tried. The local engine (RustFS, previously MinIO) is reached path style.
Addressing and signing region are
therefore per-backend configuration and must never be inferred from the other backend.

**OVH returned one unreproducible `AccessDenied`.** A `PutObject` that had succeeded
moments earlier, and succeeded on all 12 subsequent attempts, failed once with
`AccessDenied` shortly after the identity's policy was updated. Most likely policy
propagation, but it is recorded rather than dismissed: the design fails closed on
permission errors, so a transient denial would surface as a staging failure rather than a
retry. If this recurs, the admission path needs an explicit decision about whether a
denial is ever retryable — it currently is not.

## What the probe did not cover

The probe left these 16 rows `PENDING`. Named checks have since closed all of them on OVH:

- **Identity and permissions (rows 15–22).** The probe used a single full-access identity
  on a throwaway bucket. The production split — writer scoped to `content/`, reader with
  `GetObject` only, neither able to create buckets, neither able to reach another
  environment's bucket — was never exercised on OVH. This is the security contract and
  needs the real provisioned identities. *Closed 2026-09-18 to 2026-09-26:
  rows 15–17 and 19–22 pass; row 18 records a provider behaviour.*
- **Encryption at rest actually applying (rows 23–25).** The probe bucket was plaintext,
  so no encrypted round trip was observed on OVH. Verify after AES256 is set.
  *Closed 2026-09-18.*
- **Lifecycle expiry and stale-upload reclamation actually firing (rows 30–31).** Both
  rules were accepted and read back, but observing them fire takes at least a day of wall
  clock. Acceptance is not execution. *Closed 2026-09-28: both observed on OVH (rows 30/31).*
- **Dedicated signature and signing-region checks (rows 34–35).** The probe recorded
  successful signed requests and addressing differences; separate shared-suite evidence
  for these rows is still pending. *Closed 2026-09-18.*
- **TLS behavior under failure (row 37).** All probe traffic used verified HTTPS
  successfully; certificate rejection and endpoint-allowlist behavior were not tested.
  *Certificate rejection closed 2026-09-18.*

## How to refresh this matrix

Re-run and re-record, do not reason from the previous result.

| Trigger | Re-run |
| --- | --- |
| RustFS server or `rc` image change | The shared `test_staging_contract.py` and backend-specific `test_staging_backend_checks.py` suites with `--staging-backend=rustfs`, `make lifecycle-check`, plus `make staging-init`. Update the digests in the header table. Rows 16, 25 and 28–31 are provisioning or engine behavior and change with the server, not with the app; re-check the one-key `content` grant (row 16) on every upgrade and drop it if the engine stops normalizing trailing slashes. |
| SDK upgrade (`aioboto3`/`aiobotocore`/`botocore`) | Rows 32–35 first, on **both** backends, then the whole contract suite. Capture the actual request headers, not just a green test — the checksum default changed silently once already. |
| Provider capability change, new region, or new endpoint | Every OVH cell. A documentation citation from a previous review does not survive an endpoint change. |
| New S3 operation, request option, header, or management feature | Add a row before writing the code. Check both vendors' documentation, then run the affected check on RustFS and OVH. |
| Any OVH check finally executes | Replace the `PENDING` cell with the result, the date, the endpoint and the test that produced it. Record failures here too — a failed OVH check is evidence, not a reason to delete the row. |

**A failed or unavailable required check blocks S3 activation for that behavior.** Not for
the whole system by default, and not never — for that behavior. An unavailable OVH test is
pending evidence, which is the same thing as a block: it does not become a pass because
the local engine was green, because credentials were missing, or because the skip was expected.

Current state, 2026-09-28: **37 rows, none pending on OVH.** On OVH, 36 pass: 13 on the
2026-09-15 probe (rows 1–11, 13 and 14) and 23 on named checks run from 2026-09-18 to
2026-09-28 (rows 12, 15–17 and 19–37). Row 18 records a provider behaviour. Locally, all 37 rows were re-earned on RustFS on
2026-09-26: 32 `PASS` (rows 30/31 time-scaled), rows 7, 8 and 16 `PASS` with a recorded
deviation (7 and 8 since 2026-09-27), row 28 accepted and enforced, and row 37 `N/A`. The
shared adapter suite passed on MinIO and, from 2026-09-26, on RustFS; its OVH runs are
recorded in the header table.
