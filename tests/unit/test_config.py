"""Settings resolution."""

import pytest
from pydantic import ValidationError

from curate_d.config import (
    DeploymentEnvironment,
    S3AddressingStyle,
    Settings,
    StagingRole,
    get_settings,
)

_S3_ENV = {
    "CURATE_D_STAGING_S3_ENDPOINT_URL": "https://s3.us-east-va.io.cloud.ovh.us",
    "CURATE_D_STAGING_S3_BUCKET": "test-bucket",
    "CURATE_D_STAGING_S3_ACCESS_KEY_ID": "AKIAEXAMPLE",
    "CURATE_D_STAGING_S3_SECRET_ACCESS_KEY": "s3cret-value",
}


def _configure_s3(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    """Set a complete S3 staging environment, with optional per-test overrides."""
    for name, value in {**_S3_ENV, **overrides}.items():
        monkeypatch.setenv(name, value)


def test_a_separate_dbos_system_database_is_not_a_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The system tables live in the ledger database; an environment URL for them is ignored."""
    monkeypatch.setenv(
        "CURATE_D_DBOS_SYSTEM_DATABASE_URL",
        "postgresql+psycopg://curate_d:curate_d@dbos-host:5432/dbos",
    )
    settings = Settings()
    assert not hasattr(settings, "dbos_system_database_url")
    assert not hasattr(settings, "effective_dbos_system_database_url")


def test_settings_are_read_from_the_prefixed_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """A CURATE_D_-prefixed environment variable overrides its Settings field."""
    monkeypatch.setenv(
        "CURATE_D_LEDGER_DATABASE_URL",
        "postgresql+psycopg://user:pw@ledger-host:5432/ledger",
    )
    assert Settings().ledger_database_url.hosts()[0]["host"] == "ledger-host"


def test_settings_are_frozen() -> None:
    """Assigning to a Settings field raises instead of silently mutating it."""
    settings = Settings()
    with pytest.raises(ValueError, match="frozen"):
        settings.ledger_database_url = settings.ledger_database_url  # type: ignore[misc]


def test_get_settings_returns_one_cached_instance() -> None:
    """get_settings caches and returns the same Settings instance across calls."""
    get_settings.cache_clear()
    try:
        assert get_settings() is get_settings()
    finally:
        get_settings.cache_clear()


def test_s3_staging_is_disabled_without_an_endpoint() -> None:
    """No configured endpoint means S3 staging is off, not guessed from a default."""
    settings = Settings()
    assert settings.staging_s3_endpoint_url is None
    assert settings.s3_staging_enabled is False


def test_s3_staging_reads_a_complete_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """A complete S3 environment resolves into enabled staging settings."""
    _configure_s3(monkeypatch)
    settings = Settings()
    assert settings.s3_staging_enabled is True
    assert settings.staging_s3_bucket == "test-bucket"
    assert settings.staging_s3_region == "US-EAST-VA"


def test_staging_role_defaults_to_reader() -> None:
    """An undeclared profile gets read-only authority rather than write authority."""
    assert Settings().staging_s3_role is StagingRole.READER


def test_staging_role_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """The writer role must be declared explicitly to take effect."""
    monkeypatch.setenv("CURATE_D_STAGING_S3_ROLE", "writer")
    assert Settings().staging_s3_role is StagingRole.WRITER


def test_unknown_staging_role_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unrecognized role fails rather than silently falling back to a default."""
    monkeypatch.setenv("CURATE_D_STAGING_S3_ROLE", "administrator")
    with pytest.raises(ValidationError):
        Settings()


@pytest.mark.parametrize(
    "omitted",
    [
        "CURATE_D_STAGING_S3_BUCKET",
        "CURATE_D_STAGING_S3_ACCESS_KEY_ID",
        "CURATE_D_STAGING_S3_SECRET_ACCESS_KEY",
    ],
)
def test_endpoint_without_bucket_or_credentials_is_rejected(
    monkeypatch: pytest.MonkeyPatch, omitted: str
) -> None:
    """A configured endpoint requires an explicit bucket and both credential halves."""
    _configure_s3(monkeypatch)
    monkeypatch.delenv(omitted)
    with pytest.raises(ValidationError, match="no ambient credential fallback"):
        Settings()


def test_plaintext_endpoint_requires_deliberately_disabled_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An http endpoint with verification left on is a misconfiguration, not a warning."""
    _configure_s3(monkeypatch, CURATE_D_STAGING_S3_ENDPOINT_URL="http://localhost:9000")
    with pytest.raises(ValidationError, match="plaintext"):
        Settings()


def test_plaintext_endpoint_is_allowed_for_an_explicit_local_fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Local RustFS over http works once verification is turned off on purpose."""
    _configure_s3(
        monkeypatch,
        CURATE_D_STAGING_S3_ENDPOINT_URL="http://localhost:9000",
        CURATE_D_STAGING_S3_VERIFY_TLS="false",
        CURATE_D_STAGING_S3_ADDRESSING_STYLE="path",
    )
    settings = Settings()
    assert settings.staging_s3_verify_tls is False
    assert settings.staging_s3_addressing_style is S3AddressingStyle.PATH


@pytest.mark.parametrize("prefix", ["/content/", "content"])
def test_malformed_content_prefix_is_rejected(monkeypatch: pytest.MonkeyPatch, prefix: str) -> None:
    """A prefix that would not match the policy grant fails at configuration time."""
    _configure_s3(monkeypatch, CURATE_D_STAGING_S3_CONTENT_PREFIX=prefix)
    with pytest.raises(ValidationError, match="staging_s3_content_prefix"):
        Settings()


def test_empty_content_prefix_is_rejected_at_configuration_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refuse a prefix that would scope staging to the whole bucket, not at first use."""
    _configure_s3(monkeypatch, CURATE_D_STAGING_S3_CONTENT_PREFIX="")
    with pytest.raises(ValidationError, match="must not be empty"):
        Settings()


def test_s3_credentials_are_masked_in_representations(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neither credential appears in repr(), str(), or a non-serialized model dump."""
    _configure_s3(monkeypatch)
    settings = Settings()
    rendered = f"{settings!r} {settings} {settings.model_dump()}"
    assert "AKIAEXAMPLE" not in rendered
    assert "s3cret-value" not in rendered
    assert settings.staging_s3_secret_access_key is not None
    assert settings.staging_s3_secret_access_key.get_secret_value() == "s3cret-value"


def test_non_positive_transfer_timeout_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A zero or negative transfer timeout is rejected rather than disabling the bound."""
    monkeypatch.setenv("CURATE_D_STAGING_TRANSFER_TIMEOUT_SECONDS", "0")
    with pytest.raises(ValidationError):
        Settings()


def test_non_positive_local_scratch_bound_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """The local snapshot reservation ceiling must be a positive number of bytes."""
    monkeypatch.setenv("CURATE_D_STAGING_LOCAL_SCRATCH_MAX_BYTES", "0")
    with pytest.raises(ValidationError):
        Settings()


def test_environment_is_unset_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Leave the environment unset rather than defaulting to one that could be wrong."""
    monkeypatch.delenv("CURATE_D_ENVIRONMENT", raising=False)
    assert Settings().environment is None


def test_environment_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve CURATE_D_ENVIRONMENT to its deployment."""
    monkeypatch.setenv("CURATE_D_ENVIRONMENT", "dev")
    assert Settings().environment is DeploymentEnvironment.DEV


def test_unknown_environment_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reject an environment name the application does not deploy to."""
    monkeypatch.setenv("CURATE_D_ENVIRONMENT", "production")
    with pytest.raises(ValidationError, match="environment"):
        Settings()


def _assert_hidden(error: ValidationError, secret: str) -> None:
    """Assert `secret` is in neither the rendered error nor its structured details."""
    assert secret not in str(error)
    assert secret not in repr(error.errors())
    assert error.__context__ is None


def test_a_rejected_s3_configuration_keeps_the_secret_out_of_the_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A model-level staging error lists what is missing, never the credential it was given."""
    monkeypatch.setenv("CURATE_D_STAGING_S3_ENDPOINT_URL", "https://s3.example")
    monkeypatch.setenv("CURATE_D_STAGING_S3_SECRET_ACCESS_KEY", "s3cret-value")
    with pytest.raises(ValidationError, match="staging_s3_bucket") as raised:
        Settings()
    _assert_hidden(raised.value, "s3cret-value")


def test_a_rejected_database_url_keeps_its_password_out_of_the_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A field-level error names the field without echoing the value it rejected."""
    monkeypatch.setenv("CURATE_D_LEDGER_DATABASE_URL", "mysql://u:dsn-pw@db/ledger")
    with pytest.raises(ValidationError, match="ledger_database_url") as raised:
        Settings()
    _assert_hidden(raised.value, "dsn-pw")


@pytest.mark.parametrize(
    "cron", ["every ten minutes", "*/10 * * *", "61 * * * *", "0 0 24 * * *", ""]
)
def test_an_invalid_recovery_sweep_schedule_is_rejected(
    monkeypatch: pytest.MonkeyPatch, cron: str
) -> None:
    """A cron DBOS would refuse fails at settings load, naming the setting."""
    monkeypatch.setenv("CURATE_D_RECOVERY_SWEEP_SCHEDULE", cron)
    with pytest.raises(ValidationError, match="recovery_sweep_schedule"):
        Settings()


@pytest.mark.parametrize("cron", ["*/10 * * * *", "*/30 * * * * *", "0 59 23 * * *", "0 3 * * 1"])
def test_a_valid_recovery_sweep_schedule_is_kept(
    monkeypatch: pytest.MonkeyPatch, cron: str
) -> None:
    """Five fields, or six with seconds first, as DBOS reads them."""
    monkeypatch.setenv("CURATE_D_RECOVERY_SWEEP_SCHEDULE", cron)
    assert Settings().recovery_sweep_schedule == cron
