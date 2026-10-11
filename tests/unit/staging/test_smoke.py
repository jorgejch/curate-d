"""Verify provisioning smoke results through substituted application SDK clients."""

import asyncio
from dataclasses import dataclass, replace
from typing import cast

import pytest
from pydantic import HttpUrl, SecretStr

from curate_d.config import Settings, StagingRole
from curate_d.staging import smoke
from curate_d.staging.object_store import S3StagingObjectStore
from curate_d.staging.smoke import SmokeCheck, SmokeReport, SmokeStores
from tests.unit.staging.test_object_store import (
    FakeBody,
    FakeS3Client,
    Response,
    _client_error,
    _factory,
    _settings,
)


@dataclass
class SmokeFixture:
    """Expose independent identity clients sharing one simulated staging bucket."""

    stores: SmokeStores
    writer: FakeS3Client
    reader: FakeS3Client
    reader_as_writer: FakeS3Client
    writer_cross: FakeS3Client
    reader_cross: FakeS3Client
    markers: list[str]


@pytest.fixture
def backend() -> SmokeFixture:
    """Configure the expected backend-side allow and deny matrix."""
    writer, reader, reader_write, writer_cross, reader_cross = (FakeS3Client() for _ in range(5))
    for client in (reader, reader_write):
        client.objects = writer.objects
    reader_write.errors = {
        operation: _client_error("AccessDenied", operation)
        for operation in ("put_object", "delete_object")
    }
    for client in (writer_cross, reader_cross):
        client.errors["get_object"] = _client_error("AccessDenied", "GetObject")

    def store(client: FakeS3Client, role: StagingRole) -> S3StagingObjectStore:
        """Keep the adapter real and substitute only its network client."""
        return S3StagingObjectStore(_settings(role=role), client_factory=_factory(client))

    markers: list[str] = []

    async def publish_marker() -> None:
        """Record publication separately from the disposable access probes."""
        markers.append("published")

    return SmokeFixture(
        SmokeStores(
            writer=store(writer, StagingRole.WRITER),
            reader=store(reader, StagingRole.READER),
            reader_as_writer=store(reader_write, StagingRole.WRITER),
            writer_cross_bucket=store(writer_cross, StagingRole.WRITER),
            reader_cross_bucket=store(reader_cross, StagingRole.READER),
            publish_readiness_marker=publish_marker,
        ),
        writer,
        reader,
        reader_write,
        writer_cross,
        reader_cross,
        markers,
    )


def _outcomes(report: SmokeReport) -> dict[SmokeCheck, bool]:
    """Index outcomes by the operation being proved."""
    return {item.check: item.passed for item in report.outcomes}


async def test_full_access_matrix_and_cleanup(backend: SmokeFixture) -> None:
    """Prove every required grant and denial through the application adapter."""
    report = await smoke.run_smoke_checks(backend.stores)
    assert report.passed
    assert report.exit_code == 0
    assert backend.markers == ["published"]
    assert set(_outcomes(report)) == set(SmokeCheck) - {SmokeCheck.CLEANUP}
    assert backend.writer.objects == {}
    assert "put_object" in backend.reader_as_writer.operations
    assert "delete_object" in backend.reader_as_writer.operations
    assert "get_object" in backend.reader.operations
    assert "get_object" in backend.writer_cross.operations
    assert "get_object" in backend.reader_cross.operations


@pytest.mark.parametrize("passed", [True, False])
def test_command_exit_status_with_substituted_clients(
    backend: SmokeFixture, passed: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    """Return zero for the full matrix and nonzero for a failed client check."""
    if not passed:
        backend.writer.errors["put_object"] = _client_error("AccessDenied", "PutObject")
    status = smoke.main(["--cross-bucket", "unrelated"], stores=backend.stores)
    assert status == (0 if passed else 1)
    assert '"event": "staging.smoke.result"' in capsys.readouterr().err


@pytest.mark.parametrize(
    ("side", "operation", "check"),
    [
        ("writer", "put_object", SmokeCheck.WRITER_UPLOAD),
        ("reader", "get_object", SmokeCheck.READER_GET),
        ("writer", "delete_object", SmokeCheck.WRITER_DELETE),
        ("reader_as_writer", "put_object", SmokeCheck.READER_WRITE_DENIED),
        ("reader_as_writer", "delete_object", SmokeCheck.READER_DELETE_DENIED),
        ("reader_cross", "get_object", SmokeCheck.READER_CROSS_BUCKET_DENIED),
        ("writer_cross", "get_object", SmokeCheck.WRITER_CROSS_BUCKET_DENIED),
    ],
)
async def test_each_request_failure_fails_report(
    backend: SmokeFixture, side: str, operation: str, check: SmokeCheck
) -> None:
    """Reject unrelated request failures without reporting denial as proved."""
    client = cast(FakeS3Client, getattr(backend, side))
    client.errors[operation] = _client_error("InternalError", operation)
    report = await smoke.run_smoke_checks(backend.stores)
    assert not report.passed
    assert not _outcomes(report)[check]


@pytest.mark.parametrize("code", ["NoSuchKey", "NoSuchBucket"])
async def test_cross_bucket_absence_is_not_denial(backend: SmokeFixture, code: str) -> None:
    """Fail cross-bucket verification when the backend only reports absence."""
    backend.reader_cross.errors["get_object"] = _client_error(code, "GetObject")
    report = await smoke.run_smoke_checks(backend.stores)
    assert not _outcomes(report)[SmokeCheck.READER_CROSS_BUCKET_DENIED]


async def test_unexpected_reader_write_is_failure_and_cleaned(backend: SmokeFixture) -> None:
    """Clean an unexpectedly accepted reader PUT while retaining the failed denial."""
    del backend.reader_as_writer.errors["put_object"]
    report = await smoke.run_smoke_checks(backend.stores)
    assert not _outcomes(report)[SmokeCheck.READER_WRITE_DENIED]
    assert backend.writer.objects == {}


async def test_cleanup_denial_cannot_pass_reader_write_check(backend: SmokeFixture) -> None:
    """Keep successful reader PUT distinct from a later denied writer cleanup."""
    del backend.reader_as_writer.errors["put_object"]
    backend.writer.errors["delete_object"] = _client_error("AccessDenied", "DeleteObject")
    report = await smoke.run_smoke_checks(backend.stores)
    assert not _outcomes(report)[SmokeCheck.READER_WRITE_DENIED]
    assert not _outcomes(report)[SmokeCheck.CLEANUP]
    assert not report.passed


async def test_unexpected_reader_delete_fails(backend: SmokeFixture) -> None:
    """Reject reader DELETE authority on the existing writer object."""
    del backend.reader_as_writer.errors["delete_object"]
    report = await smoke.run_smoke_checks(backend.stores)
    assert not _outcomes(report)[SmokeCheck.READER_DELETE_DENIED]
    assert backend.writer.objects == {}


async def test_failed_upload_verification_still_cleans_object(backend: SmokeFixture) -> None:
    """Reclaim an upload that landed before response verification failed."""
    backend.writer.head_length = 999
    report = await smoke.run_smoke_checks(backend.stores)
    outcomes = _outcomes(report)
    assert not outcomes[SmokeCheck.WRITER_UPLOAD]
    assert not outcomes[SmokeCheck.READER_GET]
    assert not outcomes[SmokeCheck.INTEGRITY]
    assert not outcomes[SmokeCheck.WRITER_DELETE]
    assert backend.markers == []
    assert backend.writer.objects == {}


async def test_failed_reader_integrity_still_cleans_object(backend: SmokeFixture) -> None:
    """Clean writer bytes when the reader retrieves content that does not match."""

    class CorruptClient(FakeS3Client):
        """Replace retrieved bytes after the writer's upload verification."""

        async def get_object(self, **request: object) -> Response:
            return {"Body": FakeBody(b"bad")}

    reader = S3StagingObjectStore(
        _settings(role=StagingRole.READER), client_factory=_factory(CorruptClient())
    )
    report = await smoke.run_smoke_checks(replace(backend.stores, reader=reader))
    assert not _outcomes(report)[SmokeCheck.INTEGRITY]
    assert backend.writer.objects == {}


@pytest.mark.parametrize(
    "path", ["success", "failure", "config", "unexpected", "cancelled", "marker"]
)
def test_credentials_never_reach_output(
    backend: SmokeFixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    path: str,
) -> None:
    """Exclude literal credentials even from validation and unexpected-error output."""
    secrets = ("writer-visible-key", "writer-visible-secret", "reader-visible-key", "q!")
    writer = _settings().model_copy(
        update={
            "staging_s3_access_key_id": SecretStr(secrets[0]),
            "staging_s3_secret_access_key": SecretStr(secrets[1]),
        }
    )
    monkeypatch.setenv(smoke._READER_KEY_VAR, secrets[2])
    monkeypatch.setenv(smoke._READER_SECRET_VAR, secrets[3])
    leak = " ".join(secrets)
    if path == "failure":
        backend.reader.errors["get_object"] = RuntimeError(leak)
    if path == "cancelled":
        backend.reader.errors["get_object"] = asyncio.CancelledError(leak)
    if path == "marker":

        async def failed_marker() -> None:
            raise RuntimeError(leak)

        backend.stores = replace(backend.stores, publish_readiness_marker=failed_marker)
    if path == "config":

        def bad_settings() -> Settings:
            raise ValueError(leak)

        monkeypatch.setattr(smoke, "get_settings", bad_settings)
        status = smoke.main(["--cross-bucket", leak])
    elif path == "unexpected":

        async def crashed(stores: SmokeStores) -> SmokeReport:
            raise RuntimeError(leak)

        monkeypatch.setattr(smoke, "run_smoke_checks", crashed)
        status = smoke.main(["--cross-bucket", leak], stores=backend.stores)
    else:
        status = smoke.main(["--cross-bucket", leak], settings=writer, stores=backend.stores)
    output = capsys.readouterr()
    for secret in secrets:
        assert secret not in output.out + output.err
    assert status == (0 if path == "success" else 1 if path in {"failure", "marker"} else 2)


def test_argument_errors_do_not_echo_credentials(capsys: pytest.CaptureFixture[str]) -> None:
    """Keep invalid CLI values out of parser diagnostics."""
    with pytest.raises(SystemExit) as error:
        smoke.main(["--cross-bucket", "unrelated", "--secret", "credential-to-hide"])
    assert error.value.code == 2
    assert "credential-to-hide" not in capsys.readouterr().err


def test_report_requires_all_checks() -> None:
    """Reject empty and incomplete reports even if no explicit failure appears."""
    assert not SmokeReport(()).passed
    assert not SmokeReport((smoke.SmokeCheckOutcome(SmokeCheck.WRITER_UPLOAD, True, "ok"),)).passed


def _reader(writer: Settings) -> Settings:
    """Derive a matching reader configuration with a separate explicit identity."""
    return writer.model_copy(
        update={
            "staging_s3_role": StagingRole.READER,
            "staging_s3_access_key_id": SecretStr("reader-key"),
            "staging_s3_secret_access_key": SecretStr("reader-secret"),
        }
    )


@pytest.mark.parametrize(
    "invalid", ["disabled", "writer_role", "reader_role", "identity", "endpoint", "bucket"]
)
def test_store_builder_rejects_invalid_matrix(invalid: str) -> None:
    """Reject configurations that cannot prove two scoped roles on one backend."""
    writer = _settings()
    reader = _reader(writer)
    cross_bucket = "unrelated"
    if invalid == "disabled":
        writer = Settings()
    elif invalid == "writer_role":
        writer = writer.model_copy(update={"staging_s3_role": StagingRole.READER})
    elif invalid == "reader_role":
        reader = reader.model_copy(update={"staging_s3_role": StagingRole.WRITER})
    elif invalid == "identity":
        reader = reader.model_copy(
            update={"staging_s3_access_key_id": writer.staging_s3_access_key_id}
        )
    elif invalid == "endpoint":
        reader = reader.model_copy(
            update={"staging_s3_endpoint_url": HttpUrl("https://other.test")}
        )
    elif invalid == "bucket":
        cross_bucket = writer.staging_s3_bucket
    with pytest.raises(ValueError):
        smoke.build_smoke_stores(
            writer_settings=writer, reader_settings=reader, cross_bucket=cross_bucket
        )


def test_store_builder_rejects_an_empty_content_prefix() -> None:
    """Hold the bounded-prefix guarantee on the path that skips the worker factories."""
    empty = {"staging_s3_content_prefix": ""}
    writer = _settings().model_copy(update=empty)
    with pytest.raises(ValueError, match="nonempty content prefix"):
        smoke.build_smoke_stores(
            writer_settings=writer,
            reader_settings=_reader(writer).model_copy(update=empty),
            cross_bucket="unrelated",
        )


def test_command_builds_explicit_role_scoped_clients(
    backend: SmokeFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Build the provisioning-only role override with reader credentials at the SDK boundary."""
    writer = _settings()
    monkeypatch.setenv(smoke._READER_KEY_VAR, "reader-key")
    monkeypatch.setenv(smoke._READER_SECRET_VAR, "reader-secret")
    seen: list[Settings] = []
    clients = iter(
        (
            backend.writer,
            backend.reader,
            backend.reader_as_writer,
            backend.writer_cross,
            backend.reader_cross,
        )
    )
    real_store = S3StagingObjectStore

    def build(settings: Settings) -> S3StagingObjectStore:
        seen.append(settings)
        return real_store(settings, client_factory=_factory(next(clients)))

    async def publish_marker(settings: Settings) -> None:
        assert settings is writer
        backend.markers.append("published")

    monkeypatch.setattr("curate_d.staging.factories.refresh_readiness_marker", publish_marker)
    monkeypatch.setattr(smoke, "S3StagingObjectStore", build)
    assert (
        smoke.main(["--cross-bucket", "unrelated", "--log-format", "console"], settings=writer) == 0
    )
    assert seen[2].staging_s3_role is StagingRole.WRITER
    assert seen[2].staging_s3_access_key_id == SecretStr("reader-key")
    assert seen[3].staging_s3_bucket == seen[4].staging_s3_bucket == "unrelated"


def test_missing_reader_identity_fails_without_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail incomplete provisioning credentials before reaching an SDK client."""
    monkeypatch.delenv(smoke._READER_KEY_VAR, raising=False)
    monkeypatch.delenv(smoke._READER_SECRET_VAR, raising=False)
    assert smoke.main(["--cross-bucket", "unrelated"], settings=_settings()) == 2


async def test_reader_head_denial_after_accepted_put_is_failure(backend: SmokeFixture) -> None:
    """Detect an accepted reader PUT even when its HEAD verification is denied."""
    del backend.reader_as_writer.errors["put_object"]
    backend.reader_as_writer.errors["head_object"] = _client_error("AccessDenied", "HeadObject")
    report = await smoke.run_smoke_checks(backend.stores)
    assert not _outcomes(report)[SmokeCheck.READER_WRITE_DENIED]
    assert backend.writer.objects == {}


async def test_uncertain_reader_put_absence_is_failure(backend: SmokeFixture) -> None:
    """Fail closed when writer inspection cannot establish that reader PUT was denied."""
    backend.writer.errors["get_object"] = _client_error("AccessDenied", "GetObject")
    report = await smoke.run_smoke_checks(backend.stores)
    assert not _outcomes(report)[SmokeCheck.READER_WRITE_DENIED]


async def test_failed_marker_publication_fails_report(backend: SmokeFixture) -> None:
    """Keep CPU startup blocked when the post-check readiness marker cannot be seeded."""

    async def publish_marker() -> None:
        raise RuntimeError("private backend detail")

    report = await smoke.run_smoke_checks(
        replace(backend.stores, publish_readiness_marker=publish_marker)
    )
    assert not _outcomes(report)[SmokeCheck.READINESS_MARKER]
    assert report.exit_code == 1
    assert backend.writer.objects == {}


async def test_cancelled_reader_still_cleans_probe(backend: SmokeFixture) -> None:
    """Reclaim temporary probes when an in-flight smoke read is cancelled."""
    backend.reader.errors["get_object"] = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await smoke.run_smoke_checks(backend.stores)
    assert backend.writer.objects == {}
    assert backend.markers == []


async def test_vanished_bucket_during_reader_put_verification_is_failure(
    backend: SmokeFixture,
) -> None:
    """Fail the write-denial check when the bucket itself vanished mid-verification."""
    backend.writer.errors["get_object"] = _client_error("NoSuchBucket", "GetObject")
    report = await smoke.run_smoke_checks(backend.stores)
    outcomes = {item.check: item for item in report.outcomes}
    assert not outcomes[SmokeCheck.READER_WRITE_DENIED].passed
    assert outcomes[SmokeCheck.READER_WRITE_DENIED].detail == (
        "reader PUT absence could not be verified"
    )
    assert not report.passed
