"""
tests/test_wallet_crypto.py

Part of the v4.0 "Foundation" test-suite bootstrap (Bible §12), targeting
`infra/kms/wallet_crypto.py` first — the audit named it as one of the
highest-risk, highest-value modules to cover since it protects real user
funds and had zero test coverage in v3.

This suite only depends on `cryptography` and stdlib, so it runs anywhere,
including environments with no network access and none of the project's
other third-party dependencies installed.
"""

import base64
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["WALLET_MASTER_KEY"] = base64.b64encode(b"0" * 32).decode()
os.environ["KMS_PROVIDER"] = "env"

from infra.kms import wallet_crypto  # noqa: E402
from infra.kms.provider import get_key_provider  # noqa: E402
from infra.kms.env_provider import EnvMasterKeyProvider  # noqa: E402


def test_encrypt_decrypt_round_trip():
    secret = b"this-is-a-fake-32-byte-secretkey"
    enc_b64, nonce_b64 = wallet_crypto.encrypt_secret(secret)
    assert enc_b64 != base64.b64encode(secret).decode(), "ciphertext must not equal plaintext"

    recovered = wallet_crypto.decrypt_secret(enc_b64, nonce_b64)
    assert recovered == secret


def test_each_encryption_uses_a_fresh_data_key():
    """Encrypting the same secret twice must not produce identical
    ciphertext — confirms the per-call random data key/nonce generation
    the module's docstring claims is actually happening."""
    secret = b"same-secret-both-times-000000000"
    enc_a, _ = wallet_crypto.encrypt_secret(secret)
    enc_b, _ = wallet_crypto.encrypt_secret(secret)
    assert enc_a != enc_b


def test_wrong_master_key_fails_to_decrypt():
    secret = b"another-fake-secret-key-32bytes!"
    enc_b64, nonce_b64 = wallet_crypto.encrypt_secret(secret)

    # Swap in a different master key and confirm decryption fails loudly
    # rather than silently returning garbage.
    original_key = os.environ["WALLET_MASTER_KEY"]
    os.environ["WALLET_MASTER_KEY"] = base64.b64encode(b"1" * 32).decode()
    wallet_crypto_provider_reset()

    try:
        raised = False
        try:
            wallet_crypto.decrypt_secret(enc_b64, nonce_b64)
        except Exception:
            raised = True
        assert raised, "decrypting with the wrong master key must raise, not succeed"
    finally:
        os.environ["WALLET_MASTER_KEY"] = original_key
        wallet_crypto_provider_reset()


def wallet_crypto_provider_reset():
    """Test helper: infra.kms.provider caches the active provider as a
    module-level singleton by design (get_key_provider()) — reset it
    between cases that swap WALLET_MASTER_KEY so each test observes the
    env change instead of a stale cached instance."""
    import infra.kms.provider as provider_module

    provider_module._provider = None


def test_get_key_provider_defaults_to_env_backend():
    wallet_crypto_provider_reset()
    provider = get_key_provider()
    assert isinstance(provider, EnvMasterKeyProvider)


def test_env_provider_rejects_missing_key():
    original = os.environ.pop("WALLET_MASTER_KEY", None)
    try:
        raised = False
        try:
            EnvMasterKeyProvider().get_master_key()
        except RuntimeError:
            raised = True
        assert raised
    finally:
        if original is not None:
            os.environ["WALLET_MASTER_KEY"] = original


def test_env_provider_rejects_wrong_length_key():
    original = os.environ.get("WALLET_MASTER_KEY")
    os.environ["WALLET_MASTER_KEY"] = base64.b64encode(b"too-short").decode()
    try:
        raised = False
        try:
            EnvMasterKeyProvider().get_master_key()
        except RuntimeError:
            raised = True
        assert raised
    finally:
        if original is not None:
            os.environ["WALLET_MASTER_KEY"] = original


if __name__ == "__main__":
    # Runnable directly with `python tests/test_wallet_crypto.py` in
    # environments without pytest installed (e.g. this sandbox).
    tests = [
        test_encrypt_decrypt_round_trip,
        test_each_encryption_uses_a_fresh_data_key,
        test_wrong_master_key_fails_to_decrypt,
        test_get_key_provider_defaults_to_env_backend,
        test_env_provider_rejects_missing_key,
        test_env_provider_rejects_wrong_length_key,
    ]
    passed = 0
    for t in tests:
        wallet_crypto_provider_reset()
        t()
        passed += 1
        print(f"PASS  {t.__name__}")
    print(f"\n{passed}/{len(tests)} tests passed")
