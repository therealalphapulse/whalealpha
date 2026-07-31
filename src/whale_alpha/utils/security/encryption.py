"""AES-256-GCM helpers — wire-compatible port of src/utils/security/encryption.ts.

IMPORTANT (carried over verbatim from the original): app-level encryption is a
baseline, not a substitute for a real secrets manager. For anything beyond a
hobby deployment, prefer never persisting private keys server-side at all
(favor client-side signing / wallet-adapter flows), or use a KMS (AWS KMS, GCP
KMS, HashiCorp Vault) so the raw key material never touches app memory outside
a hardware-backed boundary. See docs/SECURITY.md.

Wire format is identical to the TS version so encrypted values are a drop-in
match: `iv:authTag:ciphertext`, each hex-encoded. Node's crypto module appends
the GCM auth tag separately from the ciphertext (`cipher.getAuthTag()`); the
`cryptography` library's AESGCM appends the tag to the ciphertext it returns.
`encrypt_secret`/`decrypt_secret` below split/rejoin that so the *serialized*
format on the wire matches byte-for-byte what the TS code produces and
consumes — a value encrypted by either implementation decrypts correctly in
the other.

Key-handling note (requirement #2 from the porting brief): unlike Node's
`Buffer.fill(0)`, Python cannot guarantee that a `bytes`/`str` object's backing
memory is zeroed and freed on a fixed schedule — the interpreter may have made
copies, and the garbage collector reclaims memory as it sees fit, not on
demand. We use a mutable `bytearray` for any decrypted key material and
explicitly overwrite it immediately after use (see `decrypt_secret_bytes` and
`zero_bytearray`), which is the strongest guarantee CPython allows without a
native extension. Callers MUST prefer the `*_bytes` / `bytearray` variants
over the `str`-returning ones for actual key material, and must not retain a
reference to the plaintext beyond the signing call.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from whale_alpha.config import Env

_ALGORITHM_TAG_LENGTH = 16  # AES-GCM auth tag is always 16 bytes
_IV_LENGTH = 12  # recommended nonce length for GCM, matches the TS IV_LENGTH


@dataclass(frozen=True)
class EncryptedPayload:
    iv: str  # hex
    auth_tag: str  # hex
    ciphertext: str  # hex


def _key_bytes(env: Env) -> bytes:
    return bytes.fromhex(env.ENCRYPTION_KEY)


def encrypt_secret(plaintext: str, env: Env) -> EncryptedPayload:
    key = _key_bytes(env)
    iv = os.urandom(_IV_LENGTH)
    aesgcm = AESGCM(key)
    # cryptography's AESGCM.encrypt returns ciphertext with the 16-byte tag
    # appended; split it to match the TS shape (separate iv / authTag / ciphertext).
    sealed = aesgcm.encrypt(iv, plaintext.encode("utf-8"), None)
    ciphertext, auth_tag = sealed[:-_ALGORITHM_TAG_LENGTH], sealed[-_ALGORITHM_TAG_LENGTH:]
    return EncryptedPayload(
        iv=iv.hex(),
        auth_tag=auth_tag.hex(),
        ciphertext=ciphertext.hex(),
    )


def decrypt_secret(payload: EncryptedPayload, env: Env) -> str:
    """Convenience wrapper. Prefer `decrypt_secret_bytes` for private key material."""
    plaintext = decrypt_secret_bytes(payload, env)
    try:
        return plaintext.decode("utf-8")
    finally:
        zero_bytearray(plaintext)


def decrypt_secret_bytes(payload: EncryptedPayload, env: Env) -> bytearray:
    """Decrypts into a mutable bytearray so the caller can zero it after use."""
    key = _key_bytes(env)
    iv = bytes.fromhex(payload.iv)
    auth_tag = bytes.fromhex(payload.auth_tag)
    ciphertext = bytes.fromhex(payload.ciphertext)
    aesgcm = AESGCM(key)
    plaintext = aesgcm.decrypt(iv, ciphertext + auth_tag, None)
    return bytearray(plaintext)


def zero_bytearray(buf: bytearray) -> None:
    """Best-effort in-place zeroing. See module docstring for the CPython caveat."""
    for i in range(len(buf)):
        buf[i] = 0


def serialize_encrypted(payload: EncryptedPayload) -> str:
    return f"{payload.iv}:{payload.auth_tag}:{payload.ciphertext}"


def deserialize_encrypted(value: str) -> EncryptedPayload:
    parts = value.split(":")
    if len(parts) != 3 or not all(parts):
        raise ValueError("Malformed encrypted payload")
    iv, auth_tag, ciphertext = parts
    return EncryptedPayload(iv=iv, auth_tag=auth_tag, ciphertext=ciphertext)
