"""pCloud response classification.

pCloud answers most failures with HTTP 200 and a JSON body whose `result` is non-zero, so
the status code alone says little. The codes below come from pCloud's API documentation
(docs.pcloud.com, "Errors") and from rclone's pCloud backend, which retries the same set.
"""

import math
from collections.abc import Mapping
from typing import Final

import httpx

from curate_d.domain import Provider
from curate_d.providers.errors import (
    CredentialsRejectedError,
    ProviderError,
    ProviderNotFoundError,
    ProviderRequestError,
    ProviderUnavailableError,
    RateLimitedError,
)

# Hosts that may receive a pCloud credential. The OAuth callback names the account's
# regional host, and that value arrives through the browser: anything not listed here
# would be a request to send our client secret, or a user's token, somewhere else.
US_API_HOST: Final = "api.pcloud.com"
EU_API_HOST: Final = "eapi.pcloud.com"
API_HOSTS: Final = frozenset({US_API_HOST, EU_API_HOST})

# "Log in required", "Log in failed", "Invalid 'access_token' provided",
# "Access token expired/revoked".
_CREDENTIAL_ERRORS: Final = frozenset({1000, 2000, 2094, 2095})
# "Too many login tries from this IP address", "Please try again later".
_THROTTLED: Final = frozenset({4000, 4001})
# "A component of parent directory does not exist", "Directory does not exist",
# "File not found".
_NOT_FOUND: Final = frozenset({2002, 2005, 2009})

type JsonObject = Mapping[str, object]


def require_api_host(hostname: str, *, account: str | None, operation: str) -> str:
    """Return `hostname` if it is a pCloud API host.

    Raises:
        ProviderRequestError: `hostname` is not one of pCloud's API hosts.
    """
    if hostname not in API_HOSTS:
        raise ProviderRequestError(
            Provider.PCLOUD,
            account,
            operation,
            f"refusing API host {hostname!r}; expected one of {', '.join(sorted(API_HOSTS))}",
        )
    return hostname


def _retry_after(response: httpx.Response) -> float | None:
    """Seconds from a Retry-After header in its delta-seconds form, if present."""
    raw = response.headers.get("retry-after")
    if raw is None:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if math.isfinite(value) and value >= 0 else None


def classify_http(response: httpx.Response, *, account: str | None, operation: str) -> None:
    """Raise for an HTTP status that pCloud uses to signal a failure.

    Raises:
        RateLimitedError: HTTP 429.
        ProviderUnavailableError: HTTP 5xx.
        ProviderRequestError: any other non-2xx status.
    """
    status = response.status_code
    if status == httpx.codes.TOO_MANY_REQUESTS:
        raise RateLimitedError(
            Provider.PCLOUD,
            account,
            operation,
            "HTTP 429",
            retry_after_seconds=_retry_after(response),
        )
    if status >= httpx.codes.INTERNAL_SERVER_ERROR:
        raise ProviderUnavailableError(Provider.PCLOUD, account, operation, f"HTTP {status}")
    if not response.is_success:
        raise ProviderRequestError(Provider.PCLOUD, account, operation, f"HTTP {status}")


def payload(response: httpx.Response, *, account: str | None, operation: str) -> JsonObject:
    """Decode a pCloud response body and raise for a non-zero `result`.

    Raises:
        ProviderError: the status or the `result` code signals a failure, classified by
            what the caller should do next.
    """
    classify_http(response, account=account, operation=operation)
    try:
        body = response.json()
    except ValueError:
        raise ProviderUnavailableError(
            Provider.PCLOUD, account, operation, "response body is not JSON"
        ) from None
    if not isinstance(body, Mapping):
        raise ProviderUnavailableError(
            Provider.PCLOUD, account, operation, "response body is not a JSON object"
        )
    raise_for_result(body, account=account, operation=operation)
    return body


def raise_for_result(body: JsonObject, *, account: str | None, operation: str) -> None:
    """Raise the error a non-zero pCloud `result` code stands for.

    Raises:
        CredentialsRejectedError: the credentials are invalid, expired or revoked.
        RateLimitedError: pCloud asked us to slow down.
        ProviderUnavailableError: an internal pCloud error (5xxx), or no result code.
        ProviderNotFoundError: the file or folder named does not exist.
        ProviderRequestError: pCloud rejected the request itself.
    """
    result = body.get("result")
    if result == 0:
        return
    error = body.get("error")
    reason = f"result {result}: {error}" if isinstance(error, str) else f"result {result}"
    exception: ProviderError
    if not isinstance(result, int) or isinstance(result, bool):
        exception = ProviderUnavailableError(
            Provider.PCLOUD, account, operation, "response carries no result code"
        )
    elif result in _CREDENTIAL_ERRORS:
        exception = CredentialsRejectedError(Provider.PCLOUD, account, operation, reason)
    elif result in _THROTTLED:
        exception = RateLimitedError(Provider.PCLOUD, account, operation, reason)
    elif result in _NOT_FOUND:
        exception = ProviderNotFoundError(Provider.PCLOUD, account, operation, reason)
    elif 5000 <= result < 6000:
        exception = ProviderUnavailableError(Provider.PCLOUD, account, operation, reason)
    else:
        exception = ProviderRequestError(Provider.PCLOUD, account, operation, reason)
    raise exception


async def call(
    http: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    account: str | None,
    operation: str,
    params: Mapping[str, str] | None = None,
    data: Mapping[str, str] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JsonObject:
    """Issue one pCloud request and return its decoded, successful body.

    Raises:
        ProviderUnavailableError: the request could not be completed at the transport
            level, or pCloud reported a transient failure.
        ProviderError: pCloud rejected the request; see `raise_for_result`.
    """
    try:
        response = await http.request(method, url, params=params, data=data, headers=headers)
    except httpx.TransportError as error:
        # The transport error's text can carry the request URL, and a URL can carry a
        # parameter; only the type crosses the boundary.
        raise ProviderUnavailableError(
            Provider.PCLOUD, account, operation, f"transport failure: {type(error).__name__}"
        ) from None
    return payload(response, account=account, operation=operation)
