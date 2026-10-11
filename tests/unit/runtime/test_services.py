"""The process-wide services seam, and how the worker fills it."""

from collections.abc import Iterator
from typing import cast

import pytest
from dbos import AsyncSQLAlchemyDatasource
from pydantic import SecretStr

from curate_d.config import Settings
from curate_d.providers.crypto import generate_key
from curate_d.runtime import services
from curate_d.runtime.profiles import WorkerProfile
from curate_d.worker import main as worker_main


class _Engine:
    """Records disposal."""

    def __init__(self) -> None:
        """Not yet disposed."""
        self.disposed = False

    async def dispose(self) -> None:
        """Record the disposal."""
        self.disposed = True


class _Datasource:
    """Stands in for the ledger datasource."""

    def __init__(self) -> None:
        """Own one engine."""
        self.engine = _Engine()


@pytest.fixture(autouse=True)
def _clean() -> Iterator[None]:
    """Leave no services installed between tests."""
    services.uninstall()
    yield
    services.uninstall()


def _fake_datasource() -> AsyncSQLAlchemyDatasource:
    """A datasource stand-in, typed as the real one for the dataclass."""
    return cast(AsyncSQLAlchemyDatasource, _Datasource())


def test_reading_services_before_installation_fails_loudly() -> None:
    """Workflow code in a process with no entry point wiring says what is missing."""
    with pytest.raises(services.ServicesNotInstalledError, match="install"):
        services.current()


def test_installed_services_are_what_workflow_code_sees() -> None:
    """The last installation wins and is returned."""
    installed = services.Services(settings=Settings(), datasource=_fake_datasource())
    services.install(installed)
    assert services.current() is installed


def test_a_process_without_a_key_has_no_cipher_to_hand_out() -> None:
    """Asking for the cipher without a key names the setting."""
    installed = services.Services(settings=Settings(), datasource=_fake_datasource())
    with pytest.raises(services.ServicesNotInstalledError, match="ENCRYPTION_KEY"):
        installed.require_cipher()


@pytest.fixture
def datasource(monkeypatch: pytest.MonkeyPatch) -> _Datasource:
    """Substitute datasource creation, returning the stand-in it will hand out."""
    created = _Datasource()

    async def create(settings: Settings) -> _Datasource:
        """Return the stand-in."""
        return created

    monkeypatch.setattr(worker_main, "create_ledger_datasource", create)
    return created


def test_the_io_worker_gets_a_cipher_and_a_disposed_creation_pool(
    datasource: _Datasource,
) -> None:
    """The creation loop's connections are released, and the credential key is loaded."""
    settings = Settings(credential_encryption_key=SecretStr(generate_key()))
    prepared = worker_main.prepare_services(settings, WorkerProfile.IO)
    assert datasource.engine.disposed
    assert prepared.cipher is not None
    assert services.current() is prepared


def test_the_cpu_worker_never_gets_a_cipher(datasource: _Datasource) -> None:
    """The decoding profile cannot decrypt provider credentials, even if handed the key."""
    settings = Settings(credential_encryption_key=SecretStr(generate_key()))
    assert worker_main.prepare_services(settings, WorkerProfile.CPU).cipher is None
