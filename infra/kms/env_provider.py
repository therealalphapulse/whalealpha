"""
infra/kms/env_provider.py

The original v3 `EnvMasterKeyProvider`, unchanged, moved here as part of
the v4 KMS-ready split (§8). Suitable for local development and for this
sandbox; not recommended for production custody of real user funds — see
`kms_provider.py` and the v4 Architecture Bible §8 for the production path.
"""

import base64
import os

from infra.kms.provider import MasterKeyProvider


class EnvMasterKeyProvider(MasterKeyProvider):
    """
    Reads WALLET_MASTER_KEY (a base64-encoded 32-byte key) from the
    environment. Suitable for getting started; a real KMS is strongly
    recommended before handling meaningful amounts of real user funds.
    """

    def get_master_key(self) -> bytes:
        raw = os.getenv("WALLET_MASTER_KEY")
        if not raw:
            raise RuntimeError(
                "WALLET_MASTER_KEY is missing. Generate one with:\n"
                "  python -c \"import secrets,base64; print(base64.b64encode(secrets.token_bytes(32)).decode())\"\n"
                "and set it as an environment variable. AlphaPulse will not "
                "start Real Wallet features without it."
            )
        try:
            key = base64.b64decode(raw)
        except Exception as e:
            raise RuntimeError(f"WALLET_MASTER_KEY is not valid base64: {e}")

        if len(key) != 32:
            raise RuntimeError(
                f"WALLET_MASTER_KEY must decode to exactly 32 bytes, got {len(key)}."
            )
        return key
