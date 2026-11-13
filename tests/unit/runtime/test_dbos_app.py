"""DBOS configuration."""

import hashlib
import inspect
import os
import re
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

import pytest
from dbos import DBOS
from sqlalchemy.engine import make_url

from curate_d.config import DeploymentEnvironment, Settings
from curate_d.runtime import dbos_app
from curate_d.runtime.dbos_app import (
    APPLICATION_VERSION,
    BUILD_ENVIRONMENT_PATH,
    BUILD_REVISION_ENV,
    SYSTEM_SCHEMA,
    application_name,
    application_version,
    build_dbos_config,
    client,
    configure,
    system_database_url,
)
from curate_d.runtime.identity import (
    EXECUTOR_ID_ENV,
    POD_NAME_ENV,
    ExecutorIdentityError,
    resolve_executor_id,
)


def test_the_dbos_system_url_keeps_the_ledger_options(monkeypatch: pytest.MonkeyPatch) -> None:
    """Options built into the ledger URL reach DBOS's configuration unchanged."""
    monkeypatch.delenv("CURATE_D_LEDGER_DATABASE_URL", raising=False)
    monkeypatch.setenv("CURATE_D_LEDGER_DATABASE_OPTIONS", "sslmode=disable")
    config = build_dbos_config(Settings(_env_file=None), executor_id="worker-1")
    system_url = config["system_database_url"]
    assert system_url is not None
    assert make_url(system_url).query == {"sslmode": "disable"}


def test_the_system_database_is_the_ledger_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """DBOS's tables follow the ledger URL; there is no separate system database setting."""
    monkeypatch.setenv(
        "CURATE_D_LEDGER_DATABASE_URL",
        "postgresql+psycopg://curate_d:curate_d@ledger-host:5432/curate_d",
    )
    monkeypatch.setenv(
        "CURATE_D_DBOS_SYSTEM_DATABASE_URL",
        "postgresql+psycopg://curate_d:curate_d@elsewhere:5432/dbos",
    )
    settings = Settings()
    assert system_database_url(settings) == str(settings.ledger_database_url)
    assert build_dbos_config(settings, executor_id="worker-1")["system_database_url"] == str(
        settings.ledger_database_url
    )


def test_the_configuration_pins_a_version_and_a_schema() -> None:
    """An auto-derived source hash would make a version bump an accident, not a decision."""
    config = build_dbos_config(Settings(), executor_id="worker-1")
    assert config["application_version"] == APPLICATION_VERSION
    assert config["dbos_system_schema"] == SYSTEM_SCHEMA
    assert config["executor_id"] == "worker-1"


def test_the_configuration_exports_spans_and_logs_through_the_global_providers() -> None:
    """DBOS exports onto whatever providers Logfire installed, with semconv attribute names."""
    config = build_dbos_config(Settings(), executor_id="worker-1")
    assert config["enable_otlp"] is True
    assert config["otel_attribute_format"] == "semconv"
    assert "otlp_traces_endpoints" not in config
    assert "otlp_logs_endpoints" not in config


@pytest.mark.parametrize("environment", list(DeploymentEnvironment))
def test_dbos_runs_under_the_environment_s_name(
    environment: DeploymentEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DBOS and its client run as `curate-d-<environment>`."""
    monkeypatch.setenv("CURATE_D_ENVIRONMENT", environment.value)
    settings = Settings()
    name = f"curate-d-{environment.value}"
    built: list[dict[str, object]] = []
    monkeypatch.setattr(dbos_app, "DBOSClient", lambda **options: built.append(options))
    assert application_name(environment) == name
    assert build_dbos_config(settings, executor_id="worker-1")["name"] == name
    client(settings)
    assert built[0]["application_name"] == name


def test_the_client_retries_lost_connections_unless_told_not_to(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller with a deadline, such as the worker's drain, opts out of blocking retries."""
    built: list[dict[str, object]] = []
    monkeypatch.setattr(dbos_app, "DBOSClient", lambda **options: built.append(options))
    client(Settings())
    client(Settings(), retry_connection_errors=False)
    assert [options["retry_connection_errors"] for options in built] == [True, False]
    assert all(options["lazy"] for options in built)


def test_an_unset_environment_runs_as_local(monkeypatch: pytest.MonkeyPatch) -> None:
    """No environment names the application `curate-d-local`."""
    monkeypatch.delenv("CURATE_D_ENVIRONMENT", raising=False)
    settings = Settings(_env_file=None)
    assert build_dbos_config(settings, executor_id="worker-1")["name"] == "curate-d-local"


def test_the_configured_executor_id_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prefer the explicit executor id env var over the pod name."""
    monkeypatch.setenv(EXECUTOR_ID_ENV, "explicit")
    monkeypatch.setenv(POD_NAME_ENV, "pod-7")
    assert resolve_executor_id() == "explicit"


def test_the_pod_name_is_used_when_no_executor_id_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fall back to the pod name when no executor id env var is set."""
    monkeypatch.delenv(EXECUTOR_ID_ENV, raising=False)
    monkeypatch.setenv(POD_NAME_ENV, "pod-7")
    assert resolve_executor_id() == "pod-7"


def test_a_blank_value_does_not_count_as_an_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Treat a whitespace-only executor id as unset and fall back to the pod name."""
    monkeypatch.setenv(EXECUTOR_ID_ENV, "   ")
    monkeypatch.setenv(POD_NAME_ENV, "pod-7")
    assert resolve_executor_id() == "pod-7"


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_a_missing_identity_fails_loudly(
    monkeypatch: pytest.MonkeyPatch, blank: str | None
) -> None:
    """Unset, DBOS falls back to the shared identity 'local'.

    A restarting pod would then recover PENDING workflows belonging to live peers and
    re-dispatch work that is currently running elsewhere.
    """
    for variable in (EXECUTOR_ID_ENV, POD_NAME_ENV):
        if blank is None:
            monkeypatch.delenv(variable, raising=False)
        else:
            monkeypatch.setenv(variable, blank)
    with pytest.raises(ExecutorIdentityError, match="live peers"):
        resolve_executor_id()


def test_the_hostname_is_never_an_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Under a Deployment the hostname is a random pod name that never comes back."""
    monkeypatch.delenv(EXECUTOR_ID_ENV, raising=False)
    monkeypatch.delenv(POD_NAME_ENV, raising=False)
    monkeypatch.setattr("socket.gethostname", lambda: "curate-d-worker-7d9f8-x2k4q")
    with pytest.raises(ExecutorIdentityError):
        resolve_executor_id()


def test_building_the_configuration_requires_an_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refuse to build a DBOS configuration without a stable executor identity."""
    monkeypatch.delenv(EXECUTOR_ID_ENV, raising=False)
    monkeypatch.delenv(POD_NAME_ENV, raising=False)
    with pytest.raises(ExecutorIdentityError):
        build_dbos_config(Settings())


def test_the_configured_executor_id_overrides_dbos_vmid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pins DBOS: ``DBOSConfig.executor_id`` wins over the ``DBOS__VMID`` environment.

    Were it the other way round, a stray DBOS__VMID in a pod spec would silently replace
    the leased identity with one no lease covers.
    """
    monkeypatch.setenv("DBOS__VMID", "from-environment")
    DBOS.destroy()
    try:
        configure(Settings(), executor_id="worker-io-0")
        assert DBOS.executor_id == "worker-io-0"
    finally:
        DBOS.destroy()


# A dev image's version, its own tag:
# `dev-<14-digit UTC commit timestamp>-<hex sha>-<12-digit hex environment hash>`.
_DEV_VERSION = re.compile(r"dev-\d{14}-[0-9a-f]{7,12}-[0-9a-f]{12}")

# A Kubernetes label value: at most 63 characters, alphanumerics, `-`, `_` and `.`,
# starting and ending with an alphanumeric.
_LABEL_VALUE = re.compile(r"[A-Za-z0-9]([A-Za-z0-9_.-]{0,61}[A-Za-z0-9])?")

_PACKAGES = b"libpq5:amd64=17.6-0+deb13u1\ntesseract-ocr:amd64=5.5.0-1+b1\n"
_PACKAGES_HASH = hashlib.sha256(_PACKAGES).hexdigest()[:12]
_DEV_TAG = f"dev-20261003201500-e69d801-{_PACKAGES_HASH}"


@pytest.fixture
def build_environment(tmp_path: Path) -> Path:
    """Write an image's list of installed system packages and return its path."""
    path = tmp_path / "build-environment"
    path.write_bytes(_PACKAGES)
    return path


def _import_application_version(build_revision: str | None) -> subprocess.CompletedProcess[str]:
    """Import `dbos_app` in a fresh interpreter with the given build revision, or none."""
    env = {k: v for k, v in os.environ.items() if k != BUILD_REVISION_ENV}
    if build_revision is not None:
        env[BUILD_REVISION_ENV] = build_revision
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "from curate_d.runtime.dbos_app import APPLICATION_VERSION; print(APPLICATION_VERSION)",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def test_the_application_version_comes_from_package_metadata() -> None:
    """DBOS pins in-flight workflows to this value.

    A constant written here could drift from the released version, and a v0.2.0 image
    would go on enqueueing and recovering work as 0.1.0 with nothing reporting it. A dev
    image runs as its own tag instead, which names the commit it was built from.
    """
    assert version("curate-d") == APPLICATION_VERSION or _DEV_VERSION.fullmatch(
        APPLICATION_VERSION
    ), f"{APPLICATION_VERSION!r} is neither the packaged version nor a dev tag"


def test_the_application_version_is_a_release_version() -> None:
    """Check a release application version follows MAJOR.MINOR.PATCH."""
    if _DEV_VERSION.fullmatch(APPLICATION_VERSION):
        pytest.skip("a dev image runs as its tag, not a release version")
    parts = APPLICATION_VERSION.split(".")
    assert len(parts) == 3, f"{APPLICATION_VERSION!r} is not MAJOR.MINOR.PATCH"
    assert all(part.isdigit() for part in parts)


def test_a_release_image_imports_as_exactly_the_packaged_version() -> None:
    """Import without a build revision and get the bare packaged version."""
    result = _import_application_version(None)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == version("curate-d")


def test_a_dev_tag_without_the_images_package_list_fails_the_import() -> None:
    """Outside an image there is no package list, so a dev tag can't be checked and fails."""
    if BUILD_ENVIRONMENT_PATH.exists():
        pytest.skip(f"{BUILD_ENVIRONMENT_PATH} exists: this runs inside an image")
    result = _import_application_version(_DEV_TAG)
    assert result.returncode != 0
    assert result.stdout == ""
    assert "ValueError" in result.stderr
    assert "can't be read" in result.stderr


def test_a_malformed_build_revision_fails_the_import() -> None:
    """Import with a revision that is not a dev tag and fail with the ValueError."""
    result = _import_application_version("v0.1.1")
    assert result.returncode != 0
    assert result.stdout == ""
    assert "ValueError" in result.stderr
    assert "is not a dev image tag" in result.stderr


def test_a_release_image_runs_as_the_packaged_version(tmp_path: Path) -> None:
    """No build revision, or an empty one from an unset build arg, adds nothing."""
    missing = tmp_path / "absent"
    assert application_version("0.1.1", None, missing) == "0.1.1"
    assert application_version("0.1.1", "", missing) == "0.1.1"


def test_a_dev_image_runs_as_its_tag(build_environment: Path) -> None:
    """The dev tag is the version, so each dev commit is a distinct version."""
    assert application_version("0.1.1", _DEV_TAG, build_environment) == _DEV_TAG
    other_commit = f"dev-20261003211500-0a1b2c3-{_PACKAGES_HASH}"
    assert application_version("0.1.1", other_commit, build_environment) == other_commit


def test_a_dev_tag_hashed_from_other_packages_is_refused(build_environment: Path) -> None:
    """An image whose packages are not the ones its tag names can't run as that tag."""
    with pytest.raises(ValueError, match=f"but this image's is {_PACKAGES_HASH}"):
        application_version("0.1.1", "dev-20261003201500-e69d801-000000000000", build_environment)


def test_a_dev_tag_without_a_readable_package_list_is_refused(tmp_path: Path) -> None:
    """A missing package list fails closed rather than skipping the check."""
    with pytest.raises(ValueError, match="can't be read"):
        application_version("0.1.1", _DEV_TAG, tmp_path / "absent")


def test_a_dev_version_is_a_kubernetes_label_value() -> None:
    """A per-version worker controller labels and names its resources by version.

    A `+` local version label is valid in neither, which is why a dev image runs as its tag.
    The longest tag the version check accepts must fit both.
    """
    longest = f"dev-20261003201500-{'a' * 12}-{'b' * 12}"
    assert _DEV_VERSION.fullmatch(longest)
    assert _LABEL_VALUE.fullmatch(longest), longest
    # dbos-version-controller's `spec.version` pattern.
    assert re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,48}", longest), longest


@pytest.mark.parametrize(
    "revision",
    [
        "v0.1.1",
        "dev-2026-e69d801",
        "dev-20261003201500-e69d801",
        "dev-20261003201500-E69D801-0123456789ab",
        f"dev-20261003201500-{'a' * 40}-0123456789ab",
        "dev-20261003201500-e69d801-0123456789",
        "e69d801",
    ],
)
def test_a_build_revision_that_is_not_a_dev_tag_is_refused(
    revision: str, build_environment: Path
) -> None:
    """A malformed revision fails at import rather than naming an untraceable version."""
    with pytest.raises(ValueError, match="not a dev image tag"):
        application_version("0.1.1", revision, build_environment)


def test_the_system_schema_migration_binding_still_exists() -> None:
    """Pins the DBOS helper `migrations/env.py` calls; an upgrade that moves it fails here."""
    from dbos import run_dbos_database_migrations

    parameters = inspect.signature(run_dbos_database_migrations).parameters
    assert {"system_database_url", "schema"} <= parameters.keys()
