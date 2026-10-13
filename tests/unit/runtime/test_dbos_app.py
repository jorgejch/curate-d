"""DBOS configuration."""

import inspect
import os
import re
import subprocess
import sys
from importlib.metadata import version

import pytest
from dbos import DBOS

from curate_d.config import DeploymentEnvironment, Settings
from curate_d.runtime import dbos_app
from curate_d.runtime.dbos_app import (
    APPLICATION_VERSION,
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


# The local label a dev image adds: `dev.<14-digit UTC timestamp>.<hex sha>`.
_DEV_LABEL = re.compile(r"dev\.\d{14}\.[0-9a-f]{7,40}")


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
    image only appends a local label, so the package component is what must match.
    """
    package, _, label = APPLICATION_VERSION.partition("+")
    assert package == version("curate-d")
    assert not label or _DEV_LABEL.fullmatch(label), f"{label!r} is not a dev build label"


def test_the_application_version_is_a_release_version() -> None:
    """Check the application version's package component follows MAJOR.MINOR.PATCH."""
    package = APPLICATION_VERSION.partition("+")[0]
    parts = package.split(".")
    assert len(parts) == 3, f"{package!r} is not MAJOR.MINOR.PATCH"
    assert all(part.isdigit() for part in parts)


def test_a_release_image_imports_as_exactly_the_packaged_version() -> None:
    """Import without a build revision and get the bare packaged version."""
    result = _import_application_version(None)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == version("curate-d")


def test_a_dev_image_imports_as_its_build_of_the_packaged_version() -> None:
    """Import with a dev tag and get `<package>+dev.<timestamp>.<sha>`."""
    result = _import_application_version("dev-20261003201500-e69d801")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"{version('curate-d')}+dev.20261003201500.e69d801"


def test_a_malformed_build_revision_fails_the_import() -> None:
    """Import with a revision that is not a dev tag and fail with the ValueError."""
    result = _import_application_version("v0.1.1")
    assert result.returncode != 0
    assert result.stdout == ""
    assert "ValueError" in result.stderr
    assert "is not a dev image tag" in result.stderr


def test_a_release_image_runs_as_the_packaged_version() -> None:
    """No build revision, or an empty one from an unset build arg, adds nothing."""
    assert application_version("0.1.1", None) == "0.1.1"
    assert application_version("0.1.1", "") == "0.1.1"


def test_a_dev_image_runs_as_its_own_build_of_the_packaged_version() -> None:
    """The dev tag becomes a local version label, so each dev build is a distinct version."""
    assert (
        application_version("0.1.1", "dev-20261003201500-e69d801")
        == "0.1.1+dev.20261003201500.e69d801"
    )
    assert application_version("0.1.1", "dev-20261003201500-e69d801") != application_version(
        "0.1.1", "dev-20261003211500-0a1b2c3"
    )


@pytest.mark.parametrize(
    "revision", ["v0.1.1", "dev-2026-e69d801", "dev-20261003201500-E69D801", "e69d801"]
)
def test_a_build_revision_that_is_not_a_dev_tag_is_refused(revision: str) -> None:
    """A malformed revision fails at import rather than naming an untraceable version."""
    with pytest.raises(ValueError, match="not a dev image tag"):
        application_version("0.1.1", revision)


def test_the_system_schema_migration_binding_still_exists() -> None:
    """Pins the DBOS helper `migrations/env.py` calls; an upgrade that moves it fails here."""
    from dbos.cli.migration import run_dbos_database_migrations

    parameters = inspect.signature(run_dbos_database_migrations).parameters
    assert {"system_database_url", "schema"} <= parameters.keys()
