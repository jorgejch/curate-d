"""Reaching a connected account's files.

Everything that calls a provider asks this layer for a client by provider and account.
It refuses a connection flagged for re-authentication before any request is made, so work
that depends on it stops instead of retrying against credentials known to be bad. Every
request the returned client makes is paced against the provider's quota and waits out
rate-limit answers.
"""

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Protocol

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine

from curate_d.domain import PendingFolder, Provider, StrongFingerprint
from curate_d.providers.connections import ConnectionRepository
from curate_d.providers.crypto import CredentialCipher
from curate_d.providers.errors import (
    ConnectionRequiresReauthenticationError,
    ProviderRequestError,
)
from curate_d.providers.pacing import Backoff, Pacer, paced
from curate_d.providers.pcloud.files import FolderListing, PCloudFiles

_HTTP_TIMEOUT = httpx.Timeout(30.0, read=300.0)


class ProviderFiles(Protocol):
    """File metadata and content for one connected account."""

    async def resolve_folder(self, path: str) -> PendingFolder:
        """The folder at a configured path."""
        ...

    async def list_folder(self, folder: PendingFolder) -> FolderListing:
        """One folder's direct contents."""
        ...

    async def reported_digests(self, native_id: str) -> tuple[StrongFingerprint, ...]:
        """The digests the provider reports for a file."""
        ...

    def open_content(self, native_id: str) -> AbstractAsyncContextManager[AsyncIterator[bytes]]:
        """A file's content as a stream of chunks."""
        ...


class ProviderAccess(Protocol):
    """Hands out clients for connected accounts."""

    async def files(self, provider: Provider, account: str) -> ProviderFiles:
        """A client for one account's files.

        Raises:
            ConnectionRequiresReauthenticationError: the connection is flagged for a person.
            ProviderRequestError: the account has never been authorized.
        """
        ...


class PacedFiles:
    """Wraps a client so each request is paced and rate limits are waited out."""

    def __init__(self, inner: ProviderFiles, pacer: Pacer, backoff: Backoff) -> None:
        """Wrap `inner`; `pacer` is shared by every client for the same provider."""
        self._inner = inner
        self._pacer = pacer
        self._backoff = backoff

    async def resolve_folder(self, path: str) -> PendingFolder:
        """Paced `resolve_folder`."""
        return await paced(self._pacer, self._backoff, lambda: self._inner.resolve_folder(path))

    async def list_folder(self, folder: PendingFolder) -> FolderListing:
        """Paced `list_folder`."""
        return await paced(self._pacer, self._backoff, lambda: self._inner.list_folder(folder))

    async def reported_digests(self, native_id: str) -> tuple[StrongFingerprint, ...]:
        """Paced `reported_digests`."""
        return await paced(
            self._pacer, self._backoff, lambda: self._inner.reported_digests(native_id)
        )

    @asynccontextmanager
    async def open_content(self, native_id: str) -> AsyncGenerator[AsyncIterator[bytes], None]:
        """`open_content`, admitted by the pacer before the link is requested."""
        await self._pacer.admit()
        async with self._inner.open_content(native_id) as chunks:
            yield chunks


class ConnectedAccounts:
    """Production `ProviderAccess`: connections from the ledger, clients over HTTPS."""

    def __init__(
        self,
        engine: AsyncEngine,
        cipher: CredentialCipher,
        pacers: Mapping[Provider, Pacer],
        *,
        backoff: Backoff | None = None,
    ) -> None:
        """Bind where connections are read from, the key they are sealed with, and pacers."""
        self._engine = engine
        self._cipher = cipher
        self._pacers = pacers
        self._backoff = backoff or Backoff()
        self._clients: dict[asyncio.AbstractEventLoop, httpx.AsyncClient] = {}

    def _http(self) -> httpx.AsyncClient:
        """One HTTP client per event loop: a client's pool belongs to the loop it was made on."""
        loop = asyncio.get_running_loop()
        client = self._clients.get(loop)
        if client is None or client.is_closed:
            client = httpx.AsyncClient(timeout=_HTTP_TIMEOUT, follow_redirects=False)
            self._clients[loop] = client
        return client

    async def files(self, provider: Provider, account: str) -> ProviderFiles:
        """A paced client for one account.

        Raises:
            ConnectionRequiresReauthenticationError: the connection is flagged for a person.
            ProviderRequestError: the account has never been authorized, or has no host.
        """
        async with self._engine.connect() as connection:
            repository = ConnectionRepository(connection, self._cipher)
            record = await repository.get(provider, account)
            tokens = await repository.tokens(provider, account)
        if record is None or tokens is None:
            raise ProviderRequestError(provider, account, "connect", "account is not connected")
        if not record.is_usable:
            raise ConnectionRequiresReauthenticationError(
                provider, account, "connect", record.failure_reason or "re-authorization needed"
            )
        if record.api_host is None:
            raise ProviderRequestError(provider, account, "connect", "no API host is recorded")
        return PacedFiles(
            PCloudFiles(
                self._http(),
                api_host=record.api_host,
                account=account,
                access_token=tokens.access_token,
            ),
            self._pacers[provider],
            self._backoff,
        )

    async def aclose(self) -> None:
        """Close every HTTP client this instance opened on the running loop."""
        loop = asyncio.get_running_loop()
        client = self._clients.pop(loop, None)
        if client is not None:
            await client.aclose()
