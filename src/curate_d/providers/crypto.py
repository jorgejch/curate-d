"""Encryption for stored provider credentials.

Fernet: AES-128-CBC with an HMAC, so a tampered ciphertext fails to decrypt rather than
decrypting to something attacker-chosen. Authentication matters here because the
plaintext becomes an Authorization header — an unauthenticated cipher would let anyone
with database write access redirect our requests.

The key comes from the environment and is never persisted alongside what it protects. A
database dump without the key is therefore not a credential leak, which is the property
that makes routine backups safe to hold.
"""

from cryptography.fernet import Fernet, InvalidToken
from pydantic import SecretStr

from curate_d.config import Settings


class CredentialEncryptionError(RuntimeError):
    """Credentials could not be encrypted or decrypted."""


class MissingEncryptionKeyError(CredentialEncryptionError):
    """No key is configured. Refusing to store credentials in the clear."""


def generate_key() -> str:
    """Generate a fresh key for bootstrapping a deployment. Store it in the secret manager."""
    return Fernet.generate_key().decode("ascii")


class CredentialCipher:
    """Encrypts and decrypts credential material with the configured key."""

    def __init__(self, key: SecretStr) -> None:
        """Build the cipher from a Fernet key.

        Raises:
            CredentialEncryptionError: `key` is not a valid Fernet key.
        """
        try:
            self._fernet = Fernet(key.get_secret_value().encode("ascii"))
        except (ValueError, TypeError) as error:
            # Deliberately does not echo the key, not even its length.
            raise CredentialEncryptionError(
                "the configured credential encryption key is not a valid Fernet key; "
                "generate one with curate_d.providers.crypto.generate_key"
            ) from error

    @classmethod
    def from_settings(cls, settings: Settings) -> "CredentialCipher":
        """Build from configuration, refusing to run without a key.

        Falling back to storing credentials unencrypted would be the worst possible
        default: it works, and nothing about the system's behaviour reveals it.
        """
        if settings.credential_encryption_key is None:
            raise MissingEncryptionKeyError(
                "CURATE_D_CREDENTIAL_ENCRYPTION_KEY is not set; provider credentials "
                "cannot be stored without it"
            )
        return cls(settings.credential_encryption_key)

    def encrypt(self, plaintext: SecretStr) -> bytes:
        """Encrypt a credential value into Fernet ciphertext."""
        return self._fernet.encrypt(plaintext.get_secret_value().encode("utf-8"))

    def decrypt(self, ciphertext: bytes, *, max_age_seconds: int | None = None) -> SecretStr:
        """Recover the plaintext, or fail without revealing anything about the attempt.

        Args:
            ciphertext: Fernet token produced by `encrypt`.
            max_age_seconds: Reject a token older than this. Fernet embeds its creation
                time, which is what lets a short-lived value such as an OAuth state need
                no table of its own.

        Raises:
            CredentialEncryptionError: the key does not match, the ciphertext was
                modified, or it is older than `max_age_seconds`.
        """
        try:
            return SecretStr(self._fernet.decrypt(ciphertext, ttl=max_age_seconds).decode("utf-8"))
        except InvalidToken as error:
            raise CredentialEncryptionError(
                "stored credentials could not be decrypted: the key does not match, the "
                "ciphertext was modified, or it has expired"
            ) from error
