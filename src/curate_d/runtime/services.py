"""The collaborators workflows and steps reach at run time.

DBOS workflows are module-level functions, decorated at import time, while what they
need — the ledger datasource, the credential cipher, provider clients — only exists once
an entry point has read configuration. The entry point installs them here before
`DBOS.launch()`, and workflow code reads them back. Tests install substitutes the same way.

Installed once per process and read-only afterwards: a workflow never changes what is
installed, which keeps replay deterministic.
"""

from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

from dbos import AsyncSQLAlchemyDatasource

from curate_d.classification.remote import RemoteClassifier
from curate_d.clock import utc_now
from curate_d.config import Settings
from curate_d.domain import Provider
from curate_d.placement.admission import TransferAdmission
from curate_d.placement.remotes import RcloneAccess, RemoteResolver
from curate_d.providers.access import ProviderAccess
from curate_d.providers.crypto import CredentialCipher
from curate_d.providers.refresh import TokenRefresher
from curate_d.staging.factories import StagingReaderBackend, StagingWriterBackend
from curate_d.staging.snapshots import LocalScratchBudget


@runtime_checkable
class StagingAccess(Protocol):
    """This process's side of staging, and the loop its backends belong to.

    `StagingSupervisor` in a worker. A backend's SDK session and connection pool belong
    to the loop that created them, so work touching one is handed to `run`.
    """

    @property
    def writer(self) -> StagingWriterBackend | None:
        """The write side, on the I/O profile."""
        ...

    @property
    def reader(self) -> StagingReaderBackend | None:
        """The read side, on the CPU profile."""
        ...

    async def run[T](self, work: Coroutine[None, None, T]) -> T:
        """Run `work` on the staging loop and return its result."""
        ...


class ServicesNotInstalledError(RuntimeError):
    """Workflow code ran in a process whose entry point installed no services."""


@dataclass(frozen=True)
class Services:
    """Everything a workflow or step may reach outside its arguments."""

    settings: Settings
    datasource: AsyncSQLAlchemyDatasource
    cipher: CredentialCipher | None = None
    refreshers: Mapping[Provider, TokenRefresher] = field(default_factory=dict)
    provider_access: ProviderAccess | None = None
    staging: StagingAccess | None = None
    scratch_budget: LocalScratchBudget | None = None
    remote_classifier: RemoteClassifier | None = None
    rclone: RcloneAccess | None = None
    remotes: RemoteResolver | None = None
    admission: TransferAdmission | None = None
    clock: Callable[[], datetime] = utc_now

    def require_cipher(self) -> CredentialCipher:
        """Return the credential cipher, or explain why this process has none.

        Raises:
            ServicesNotInstalledError: no credential key is configured for this process.
        """
        if self.cipher is None:
            raise ServicesNotInstalledError(
                "this process has no credential cipher; set "
                "CURATE_D_CREDENTIAL_ENCRYPTION_KEY for any process that reads connections"
            )
        return self.cipher

    def require_provider_access(self) -> ProviderAccess:
        """Return provider access, or explain why this process has none.

        Raises:
            ServicesNotInstalledError: this process cannot reach providers.
        """
        if self.provider_access is None:
            raise ServicesNotInstalledError(
                "this process has no provider access; only the I/O profile reaches providers"
            )
        return self.provider_access

    def require_staging(self) -> StagingAccess:
        """Return this process's staging, or explain why there is none.

        Raises:
            ServicesNotInstalledError: no S3 staging endpoint is configured.
        """
        if self.staging is None:
            raise ServicesNotInstalledError(
                "this process has no staging; set CURATE_D_STAGING_S3_ENDPOINT_URL"
            )
        return self.staging

    def require_transfers(self) -> tuple[RcloneAccess, RemoteResolver, TransferAdmission]:
        """Return what transfers need, or explain why this process cannot transfer.

        Raises:
            ServicesNotInstalledError: no rclone daemon is configured for this process.
        """
        if self.rclone is None or self.remotes is None or self.admission is None:
            raise ServicesNotInstalledError(
                "this process cannot transfer; set CURATE_D_RCLONE_RC_URL on the I/O profile"
            )
        return self.rclone, self.remotes, self.admission

    def require_scratch_budget(self) -> LocalScratchBudget:
        """Return the local scratch budget.

        Raises:
            ServicesNotInstalledError: none was installed.
        """
        if self.scratch_budget is None:
            raise ServicesNotInstalledError("this process has no local scratch budget")
        return self.scratch_budget


_installed: list[Services] = []


def install(services: Services) -> None:
    """Make `services` what workflow code in this process sees."""
    _installed[:] = [services]


def uninstall() -> None:
    """Remove installed services; for test teardown."""
    _installed.clear()


def current() -> Services:
    """Return the installed services.

    Raises:
        ServicesNotInstalledError: nothing has been installed in this process.
    """
    if not _installed:
        raise ServicesNotInstalledError(
            "no services are installed; an entry point must call "
            "curate_d.runtime.services.install before DBOS.launch()"
        )
    return _installed[0]
