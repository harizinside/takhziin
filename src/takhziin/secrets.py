"""AES-GCM symmetric encryption for credentials at rest.

A single 32-byte master key lives at ``$config_dir/master.key`` with mode 0o600.
Every secret value is encrypted with a fresh 12-byte nonce and stored as
``base64(nonce || ciphertext || tag)`` so the ciphertext blob carries its own IV.

Loss of the master key is unrecoverable — operators are warned once on creation.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

NONCE_LEN = 12
KEY_LEN = 32
HEADER = b"takhziin-v1:"  # versioned magic header for forward-compat detection
PLACEHOLDER = "<encrypted>"


class SecretsError(RuntimeError):
    """Raised when a secret cannot be encrypted or decrypted."""


@dataclass
class Secrets:
    """AES-GCM encryption helper tied to a single master key file."""

    master_key_file: Path

    _key: bytes | None = None

    def _load_or_create_key(self) -> bytes:
        """Load master key from disk; create a fresh 32-byte key if absent."""
        path = self.master_key_file
        if path.exists():
            raw = path.read_bytes()
            if len(raw) != KEY_LEN:
                raise SecretsError(
                    f"master key at {path} has length {len(raw)}; expected {KEY_LEN}"
                )
            return raw
        path.parent.mkdir(parents=True, exist_ok=True)
        key = os.urandom(KEY_LEN)
        # write atomically: write to tmp then rename
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(key)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        os.chmod(path, 0o600)
        return key

    @property
    def key(self) -> bytes:
        if self._key is None:
            self._key = self._load_or_create_key()
        return self._key

    def encrypt(self, plaintext: str) -> str:
        """Encrypt ``plaintext`` and return a base64 string with header."""
        if plaintext is None:
            raise SecretsError("cannot encrypt None")
        nonce = os.urandom(NONCE_LEN)
        ct = AESGCM(self.key).encrypt(nonce, plaintext.encode("utf-8"), HEADER)
        return base64.b64encode(nonce + ct).decode("ascii")

    def decrypt(self, token: str) -> str:
        """Reverse of :meth:`encrypt`."""
        if not token:
            raise SecretsError("empty ciphertext")
        try:
            blob = base64.b64decode(token.encode("ascii"), validate=True)
        except (ValueError, base64.binascii.Error) as exc:
            raise SecretsError(f"invalid base64 ciphertext: {exc}") from exc
        if len(blob) < NONCE_LEN + 16:
            raise SecretsError("ciphertext too short")
        nonce, ct = blob[:NONCE_LEN], blob[NONCE_LEN:]
        try:
            pt = AESGCM(self.key).decrypt(nonce, ct, HEADER)
        except Exception as exc:  # cryptography raises InvalidTag
            raise SecretsError(f"decryption failed: {exc}") from exc
        return pt.decode("utf-8")

    def encrypt_optional(self, plaintext: str | None) -> str | None:
        if plaintext is None or plaintext == "":
            return None
        return self.encrypt(plaintext)

    def decrypt_optional(self, token: str | None) -> str | None:
        if token is None or token == "":
            return None
        return self.decrypt(token)


def mask_token(token: str) -> str:
    """Return a human-friendly masked version of an API token."""
    if not token or len(token) < 6:
        return "****"
    return f"{token[:6]}:****"


__all__ = [
    "KEY_LEN",
    "NONCE_LEN",
    "PLACEHOLDER",
    "Secrets",
    "SecretsError",
    "mask_token",
]
