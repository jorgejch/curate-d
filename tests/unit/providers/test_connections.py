"""Connection row translation.

A dict stands in for the row. What matters is that no plaintext token reaches a column,
that tokens come back only under the key they were stored with, and that the connection
record itself carries no credential at all.
"""

from datetime import UTC, datetime

import pytest
from pydantic import SecretStr

from curate_d.domain import ConnectionState, OAuthTokens, Provider
from curate_d.ledger.mapping import ColumnValues, LedgerRowError
from curate_d.providers.connections import (
    connection_values,
    to_oauth_tokens,
    to_provider_connection,
)
from curate_d.providers.crypto import CredentialCipher, CredentialEncryptionError, generate_key

_ACCESS = "access-token-plaintext"
_REFRESH = "refresh-token-plaintext"
_WHEN = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


@pytest.fixture
def cipher() -> CredentialCipher:
    """A cipher built from a freshly generated key."""
    return CredentialCipher(SecretStr(generate_key()))


def _tokens(*, refresh: str | None = _REFRESH) -> OAuthTokens:
    """Tokens as a provider would issue them."""
    return OAuthTokens(
        access_token=SecretStr(_ACCESS),
        refresh_token=None if refresh is None else SecretStr(refresh),
        expires_at=_WHEN,
        scope="files",
    )


def _row(cipher: CredentialCipher, **overrides: object) -> ColumnValues:
    """A stored row, built through the same path a save takes."""
    row = connection_values(
        Provider.PCLOUD, "acct-1", _tokens(), cipher, api_host="eapi.pcloud.com", refreshed_at=_WHEN
    )
    row.update(overrides)
    return row


def test_no_plaintext_token_reaches_a_column(cipher: CredentialCipher) -> None:
    """Neither token appears in any stored value, encoded or not."""
    row = _row(cipher)
    for value in row.values():
        rendered = value if isinstance(value, bytes) else str(value).encode()
        assert _ACCESS.encode() not in rendered
        assert _REFRESH.encode() not in rendered


def test_stored_tokens_decrypt_under_the_same_key(cipher: CredentialCipher) -> None:
    """Tokens read back equal the tokens that were stored."""
    tokens = to_oauth_tokens(_row(cipher), cipher)
    assert tokens.access_token.get_secret_value() == _ACCESS
    assert tokens.refresh_token is not None
    assert tokens.refresh_token.get_secret_value() == _REFRESH
    assert tokens.expires_at == _WHEN
    assert tokens.scope == "files"


def test_stored_tokens_are_unreadable_without_the_key(cipher: CredentialCipher) -> None:
    """A different key cannot decrypt stored tokens."""
    other = CredentialCipher(SecretStr(generate_key()))
    with pytest.raises(CredentialEncryptionError):
        to_oauth_tokens(_row(cipher), other)


def test_a_connection_without_a_refresh_token_stores_none(cipher: CredentialCipher) -> None:
    """A provider that issues no refresh token leaves the column empty."""
    row = connection_values(
        Provider.PCLOUD, "acct-1", _tokens(refresh=None), cipher, api_host=None, refreshed_at=None
    )
    assert row["refresh_token_ciphertext"] is None
    assert to_oauth_tokens(row, cipher).refresh_token is None


def test_saving_fresh_tokens_makes_the_connection_healthy(cipher: CredentialCipher) -> None:
    """New tokens clear any earlier re-authentication state and reason."""
    row = _row(cipher)
    assert row["state"] == ConnectionState.HEALTHY.value
    assert row["failure_reason"] is None


def test_the_connection_record_carries_no_credential(cipher: CredentialCipher) -> None:
    """Neither the record nor its string form contains a token."""
    record = to_provider_connection(_row(cipher))
    assert record.provider is Provider.PCLOUD
    assert record.account == "acct-1"
    assert record.state is ConnectionState.HEALTHY
    assert record.api_host == "eapi.pcloud.com"
    assert record.expires_at == _WHEN
    assert record.last_refreshed_at == _WHEN
    for rendered in (str(record), repr(record), record.model_dump_json()):
        assert _ACCESS not in rendered
        assert _REFRESH not in rendered


def test_the_tokens_string_form_is_redacted(cipher: CredentialCipher) -> None:
    """Decrypted tokens still print a placeholder rather than their value."""
    tokens = to_oauth_tokens(_row(cipher), cipher)
    assert _ACCESS not in repr(tokens)
    assert _REFRESH not in str(tokens)


def test_a_ciphertext_column_of_the_wrong_type_is_named(cipher: CredentialCipher) -> None:
    """A text value where bytes belong fails naming the column."""
    with pytest.raises(LedgerRowError, match="access_token_ciphertext"):
        to_oauth_tokens(_row(cipher, access_token_ciphertext="not-bytes"), cipher)
