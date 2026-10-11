"""Credential encryption.

The failure branches matter more than the success one: a cipher that silently accepts a
tampered ciphertext, or a codebase that quietly stores credentials in the clear when no
key is configured, both look like working systems.
"""

import pytest
from pydantic import SecretStr

from curate_d.config import Settings
from curate_d.providers.crypto import (
    CredentialCipher,
    CredentialEncryptionError,
    MissingEncryptionKeyError,
    generate_key,
)

_SECRET = "pcloud-refresh-token-value"


@pytest.fixture
def cipher() -> CredentialCipher:
    """A cipher built from a freshly generated key."""
    return CredentialCipher(SecretStr(generate_key()))


def test_a_credential_survives_an_encrypt_decrypt_round_trip(cipher: CredentialCipher) -> None:
    """A secret survives encryption and decryption unchanged."""
    ciphertext = cipher.encrypt(SecretStr(_SECRET))
    assert cipher.decrypt(ciphertext).get_secret_value() == _SECRET


def test_the_ciphertext_does_not_contain_the_plaintext(cipher: CredentialCipher) -> None:
    """The encrypted bytes do not contain the plaintext secret."""
    assert _SECRET.encode() not in cipher.encrypt(SecretStr(_SECRET))


def test_encrypting_the_same_value_twice_gives_different_ciphertexts(
    cipher: CredentialCipher,
) -> None:
    """Otherwise a database reader could tell two accounts share a credential."""
    assert cipher.encrypt(SecretStr(_SECRET)) != cipher.encrypt(SecretStr(_SECRET))


def test_credentials_are_unreadable_without_the_key() -> None:
    """Ciphertext from one key cannot be decrypted with a different key."""
    written = CredentialCipher(SecretStr(generate_key()))
    other = CredentialCipher(SecretStr(generate_key()))
    ciphertext = written.encrypt(SecretStr(_SECRET))

    with pytest.raises(CredentialEncryptionError, match="key does not match"):
        other.decrypt(ciphertext)


def test_a_tampered_ciphertext_is_rejected_rather_than_decrypted(
    cipher: CredentialCipher,
) -> None:
    """The plaintext becomes an Authorization header; a malleable cipher would let
    anyone with database write access redirect our requests."""
    ciphertext = bytearray(cipher.encrypt(SecretStr(_SECRET)))
    ciphertext[-1] ^= 0x01

    with pytest.raises(CredentialEncryptionError, match="modified"):
        cipher.decrypt(bytes(ciphertext))


def test_an_unconfigured_key_refuses_to_store_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Falling back to plaintext would work, and nothing would reveal it."""
    monkeypatch.delenv("CURATE_D_CREDENTIAL_ENCRYPTION_KEY", raising=False)
    # `Settings` also reads a local .env, which a developer's machine may have populated
    # with a real key; _env_file=None isolates this test from that machine-local state.
    with pytest.raises(MissingEncryptionKeyError, match="CREDENTIAL_ENCRYPTION_KEY"):
        CredentialCipher.from_settings(Settings(_env_file=None))


def test_a_configured_key_builds_a_working_cipher(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cipher built from configured settings encrypts and decrypts correctly."""
    monkeypatch.setenv("CURATE_D_CREDENTIAL_ENCRYPTION_KEY", generate_key())
    cipher = CredentialCipher.from_settings(Settings())
    assert cipher.decrypt(cipher.encrypt(SecretStr(_SECRET))).get_secret_value() == _SECRET


def test_a_malformed_key_is_rejected_without_echoing_it() -> None:
    """An error that quotes the key puts it in the log the error is written to."""
    bad = "not-a-fernet-key"
    with pytest.raises(CredentialEncryptionError) as caught:
        CredentialCipher(SecretStr(bad))

    assert bad not in str(caught.value)
    assert "generate_key" in str(caught.value)


def test_generated_keys_are_distinct() -> None:
    """Two calls to generate_key produce different keys."""
    assert generate_key() != generate_key()
