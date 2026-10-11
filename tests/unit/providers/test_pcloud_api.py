"""pCloud response classification.

The classification decides whether a failure is retried, stops work for a person, or
fails the request outright, so each branch is pinned here.
"""

import pickle

import httpx
import pytest

from curate_d.domain import Provider
from curate_d.providers.errors import (
    CredentialsRejectedError,
    ProviderError,
    ProviderNotFoundError,
    ProviderRequestError,
    ProviderUnavailableError,
    RateLimitedError,
)
from curate_d.providers.pcloud.api import call, payload, raise_for_result, require_api_host


def _response(status: int = 200, **kwargs: object) -> httpx.Response:
    """A response as pCloud would send it."""
    return httpx.Response(status, **kwargs)  # type: ignore[arg-type]


def test_a_zero_result_returns_the_body() -> None:
    """A successful response is decoded and returned."""
    body = payload(_response(json={"result": 0, "x": 1}), account="a", operation="op")
    assert body["x"] == 1


@pytest.mark.parametrize("code", [1000, 2000, 2094, 2095])
def test_credential_errors_ask_for_reauthentication(code: int) -> None:
    """Invalid, expired and revoked tokens are credential rejections."""
    with pytest.raises(CredentialsRejectedError):
        raise_for_result({"result": code, "error": "x"}, account="a", operation="op")


@pytest.mark.parametrize("code", [4000, 4001])
def test_throttling_results_are_rate_limits(code: int) -> None:
    """pCloud's throttling codes back off rather than fail."""
    with pytest.raises(RateLimitedError):
        raise_for_result({"result": code}, account="a", operation="op")


def test_internal_errors_are_transient() -> None:
    """A 5xxx result is retried later."""
    with pytest.raises(ProviderUnavailableError):
        raise_for_result({"result": 5000, "error": "Internal error"}, account="a", operation="op")


@pytest.mark.parametrize("code", [2002, 2005, 2009])
def test_missing_files_and_folders_are_not_found(code: int) -> None:
    """A file or folder that does not exist is told apart from other rejected requests."""
    with pytest.raises(ProviderNotFoundError, match=f"result {code}"):
        raise_for_result({"result": code, "error": "x"}, account="a", operation="stat")


def test_other_results_reject_the_request() -> None:
    """A 2xxx result that is not about credentials or a missing object is the request's fault."""
    with pytest.raises(ProviderRequestError, match="result 2003: Access denied") as caught:
        raise_for_result({"result": 2003, "error": "Access denied."}, account="a", operation="stat")
    assert not isinstance(caught.value, ProviderNotFoundError)


def test_a_body_without_a_result_code_is_transient() -> None:
    """A body pCloud would never send is treated as a bad response, not a success."""
    with pytest.raises(ProviderUnavailableError, match="no result code"):
        raise_for_result({"error": "?"}, account="a", operation="op")


def test_http_429_carries_the_retry_after() -> None:
    """The provider's requested delay reaches the caller."""
    with pytest.raises(RateLimitedError) as caught:
        payload(_response(429, headers={"retry-after": "7"}), account="a", operation="op")
    assert caught.value.retry_after_seconds == 7.0


@pytest.mark.parametrize("header", ["soon", "-1", "nan"])
def test_an_unusable_retry_after_is_ignored(header: str) -> None:
    """A Retry-After that is not a delay in seconds is dropped, not trusted."""
    with pytest.raises(RateLimitedError) as caught:
        payload(_response(429, headers={"retry-after": header}), account="a", operation="op")
    assert caught.value.retry_after_seconds is None


def test_http_5xx_is_transient() -> None:
    """A server error is retried later."""
    with pytest.raises(ProviderUnavailableError, match="HTTP 503"):
        payload(_response(503), account="a", operation="op")


def test_other_http_errors_reject_the_request() -> None:
    """A 4xx other than 429 is the request's fault."""
    with pytest.raises(ProviderRequestError, match="HTTP 404"):
        payload(_response(404), account="a", operation="op")


def test_a_non_json_body_is_transient() -> None:
    """An HTML error page from a proxy is not mistaken for a result."""
    with pytest.raises(ProviderUnavailableError, match="not JSON"):
        payload(_response(text="<html>"), account="a", operation="op")


def test_a_json_body_that_is_not_an_object_is_transient() -> None:
    """A JSON array is not a pCloud response."""
    with pytest.raises(ProviderUnavailableError, match="not a JSON object"):
        payload(_response(json=[1]), account="a", operation="op")


def test_errors_name_provider_account_and_operation() -> None:
    """The message says whose call failed and doing what."""
    with pytest.raises(ProviderError) as caught:
        raise_for_result({"result": 2009}, account="acct-7", operation="stat file")
    assert str(caught.value).startswith("pcloud account acct-7: stat file failed:")
    assert caught.value.provider is Provider.PCLOUD


def test_errors_redact_a_token_echoed_by_the_provider() -> None:
    """A token in the provider's error text does not reach the message."""
    with pytest.raises(ProviderError) as caught:
        raise_for_result(
            {"result": 2094, "error": "Invalid access_token=abcdef123456 provided"},
            account="a",
            operation="op",
        )
    assert "abcdef123456" not in str(caught.value)


def test_only_pcloud_hosts_are_accepted() -> None:
    """A host from the browser that is not pCloud's is refused."""
    assert require_api_host("eapi.pcloud.com", account=None, operation="op") == "eapi.pcloud.com"
    with pytest.raises(ProviderRequestError, match="refusing API host"):
        require_api_host("evil.example", account=None, operation="op")


async def test_a_transport_failure_is_transient_and_hides_the_url() -> None:
    """A connection failure is retried, and its message carries no request detail."""

    def refuse(request: httpx.Request) -> httpx.Response:
        """Fail every request at the transport level."""
        raise httpx.ConnectError(f"cannot reach {request.url}", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(refuse)) as http:
        with pytest.raises(ProviderUnavailableError) as caught:
            await call(
                http,
                "GET",
                "https://api.pcloud.com/x?access_token=secretvalue",
                account="a",
                operation="op",
            )
    assert "secretvalue" not in str(caught.value)
    assert "ConnectError" in str(caught.value)


@pytest.mark.parametrize(
    "error",
    [
        CredentialsRejectedError(Provider.PCLOUD, "a", "list folder", "result 2094"),
        ProviderUnavailableError(Provider.PCLOUD, None, "exchange", "HTTP 503"),
        RateLimitedError(Provider.PCLOUD, "a", "list folder", "HTTP 429", retry_after_seconds=7),
    ],
    ids=lambda e: type(e).__name__,
)
def test_errors_survive_the_pickling_dbos_checkpoints_them_with(error: ProviderError) -> None:
    """A step's exception is pickled; it must come back as the same class and context."""
    restored = pickle.loads(pickle.dumps(error))
    assert type(restored) is type(error)
    assert str(restored) == str(error)
    assert (restored.provider, restored.account, restored.operation) == (
        error.provider,
        error.account,
        error.operation,
    )
    if isinstance(error, RateLimitedError):
        assert isinstance(restored, RateLimitedError)
        assert restored.retry_after_seconds == 7
