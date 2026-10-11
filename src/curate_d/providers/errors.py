"""Provider failures, classified by what the caller should do next.

Every error names the provider, the account and the operation, and nothing else that
arrived from the wire unexamined: a provider error body can echo a request parameter, and
a request parameter can be a token. The reason text is passed through redaction before it
becomes part of the message.
"""

from curate_d.domain import Provider
from curate_d.observability.redaction import redact_text

UNKNOWN_ACCOUNT = "(account not yet known)"


class ProviderError(RuntimeError):
    """A provider operation failed. Carries enough context to act on without a token."""

    def __init__(
        self, provider: Provider, account: str | None, operation: str, reason: str
    ) -> None:
        """Build the message from the operation's context, redacting the reason."""
        self.provider = provider
        self.account = account if account is not None else UNKNOWN_ACCOUNT
        self.operation = operation
        self.reason = redact_text(reason)
        super().__init__(
            f"{provider.value} account {self.account}: {operation} failed: {self.reason}"
        )

    def __reduce__(
        self,
    ) -> tuple[type["ProviderError"], tuple[Provider, str, str, str], dict[str, object]]:
        """Rebuild from the constructor's arguments.

        DBOS checkpoints a step's exception by pickling it, and the default reduction
        passes only the formatted message back to a four-argument constructor.
        """
        return (
            type(self),
            (self.provider, self.account, self.operation, self.reason),
            self.__dict__,
        )


class ProviderUnavailableError(ProviderError):
    """Transient: the provider could not be reached or answered with a server error.

    Retrying later is correct; nothing about the request itself was wrong.
    """


class RateLimitedError(ProviderUnavailableError):
    """The provider asked us to slow down."""

    def __init__(
        self,
        provider: Provider,
        account: str | None,
        operation: str,
        reason: str,
        *,
        retry_after_seconds: float | None = None,
    ) -> None:
        """Record the provider's requested delay, if it gave one."""
        super().__init__(provider, account, operation, reason)
        self.retry_after_seconds = retry_after_seconds


class CredentialsRejectedError(ProviderError):
    """The provider refused the credentials. Only a person can restore the connection."""


class ConnectionRequiresReauthenticationError(ProviderError):
    """The connection is already flagged for re-authentication; work must stop."""


class ProviderRequestError(ProviderError):
    """The provider rejected the request itself. Retrying unchanged will fail again."""


class ProviderNotFoundError(ProviderRequestError):
    """The file or folder the request names does not exist, or no longer does."""
