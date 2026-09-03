"""
Encryption for Real Wallet private keys.

THREAT MODEL / WHY THIS EXISTS
-------------------------------
AlphaPulse's Real Wallet needs to sign transactions on the user's behalf
for automation (auto-buy, DCA) to work at all — a purely non-custodial
"we never touch your key" design can't do that, since automation has no
human present to approve/sign each trade. So the private key has to live
somewhere the bot can reach it. This module is that "somewhere," and it's
built so the key is never at rest in plaintext:

  * A random 32-byte data key is generated per-wallet.
  * The private key is encrypted with that data key (AES-256-GCM via the
    `cryptography` package's Fernet-equivalent AESGCM, authenticated).
  * The data key itself is encrypted ("wrapped") by a master key, obtained
    through `MasterKeyProvider` — never stored next to the data it protects.
  * Only the wrapped data key + ciphertext are persisted (RealWallet model).
  * Decryption happens only in-process, only when a signature is actually
    needed, and the plaintext is discarded immediately after use — see
    services/real_trade_engine.py.

SWAPPING IN A REAL KMS
-----------------------
`EnvMasterKeyProvider` (default) reads a single master key from the
WALLET_MASTER_KEY environment variable. This is fine to get the feature
running, but an env var on the same host as the database is a single
point of failure — anyone with server/host access can decrypt every
wallet. For real production use with real user funds, swap in a provider
backed by a real KMS/secrets manager (AWS KMS, GCP KMS, HashiCorp Vault)
by implementing MasterKeyProvider.get_master_key() against that service
and pointing `get_key_provider()` at it — no other code in this file or
in real_trade_engine.py needs to change.
"""

import base64
import logging
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from infra.kms.provider import MasterKeyProvider, get_key_provider

logger = logging.getLogger("AlphaPulse.WalletCrypto")

# v4: MasterKeyProvider / EnvMasterKeyProvider / get_key_provider moved to
# infra/kms/provider.py + infra/kms/env_provider.py + infra/kms/kms_provider.py
# (§8 of the v4 Architecture Bible). Re-exported here so any code still
# importing MasterKeyProvider or get_key_provider from this module keeps
# working without changes.
__all__ = [
    "MasterKeyProvider",
    "get_key_provider",
    "encrypt_secret",
    "decrypt_secret",
]


def _aesgcm_encrypt(key: bytes, plaintext: bytes) -> tuple[bytes, bytes]:
    aesgcm = AESGCM(key)
    nonce = os.urandom(12)
    ciphertext = aesgcm.encrypt(nonce, plaintext, associated_data=None)
    return ciphertext, nonce


def _aesgcm_decrypt(key: bytes, ciphertext: bytes, nonce: bytes) -> bytes:
    aesgcm = AESGCM(key)
    return aesgcm.decrypt(nonce, ciphertext, associated_data=None)


def encrypt_secret(secret_bytes: bytes) -> tuple[str, str]:
    """
    Encrypts a wallet's raw secret key bytes for storage.

    Returns (encrypted_secret_b64, nonce_b64) — both safe to store as
    strings in RealWallet.encrypted_secret / RealWallet.encryption_nonce.

    Uses envelope encryption: a fresh per-wallet data key encrypts the
    secret, and the master key (from get_key_provider()) wraps that data
    key. Both the wrapped data key and its own nonce are packed into the
    stored ciphertext so decrypt_secret only needs the master key to
    reverse the whole thing.
    """
    master_key = get_key_provider().get_master_key()

    data_key = os.urandom(32)
    secret_ciphertext, secret_nonce = _aesgcm_encrypt(data_key, secret_bytes)
    wrapped_key, wrap_nonce = _aesgcm_encrypt(master_key, data_key)

    # Pack: wrap_nonce(12) | wrapped_key(48) | secret_ciphertext(var)
    # secret_nonce is returned separately (stored in its own column).
    blob = wrap_nonce + wrapped_key + secret_ciphertext
    return base64.b64encode(blob).decode(), base64.b64encode(secret_nonce).decode()


def decrypt_secret(encrypted_secret_b64: str, nonce_b64: str) -> bytes:
    """
    Reverses encrypt_secret(). Returns the raw secret key bytes.
    Caller is responsible for using them immediately and letting them go
    out of scope right after — never cache the return value.
    """
    master_key = get_key_provider().get_master_key()

    blob = base64.b64decode(encrypted_secret_b64)
    secret_nonce = base64.b64decode(nonce_b64)

    wrap_nonce, wrapped_key, secret_ciphertext = blob[:12], blob[12:60], blob[60:]

    data_key = _aesgcm_decrypt(master_key, wrapped_key, wrap_nonce)
    try:
        return _aesgcm_decrypt(data_key, secret_ciphertext, secret_nonce)
    finally:
        # Best-effort scrub; CPython strings/bytes are immutable so this
        # isn't a guarantee, but there's no reason to hold a reference.
        del data_key
