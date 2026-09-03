"""
infra/kms/provider.py

Split out of the original wallet_crypto.py (v3) as part of v4 §8 — Security
Architecture. The abstraction itself is UNCHANGED: this is the exact seam
the v3 module was already designed around (see its docstring: "no other
code in this file or in real_trade_engine.py needs to change" to swap in a
real KMS). v4 formalizes that seam as its own infra module and adds the
second implementation the docstring was written for.
"""

import logging
import os
from abc import ABC, abstractmethod

logger = logging.getLogger("AlphaPulse.KMS")


class MasterKeyProvider(ABC):
    @abstractmethod
    def get_master_key(self) -> bytes:
        """Return a 32-byte master key used to wrap/unwrap per-wallet data keys."""
        raise NotImplementedError


_provider: MasterKeyProvider | None = None


def get_key_provider() -> MasterKeyProvider:
    """
    Single place that decides which MasterKeyProvider is active.

    v4: selection is now driven by KMS_PROVIDER (env var), not a hardcoded
    default — production deployments set KMS_PROVIDER=aws_kms (or gcp_kms /
    vault) and the required credentials; anything else (including unset,
    e.g. local dev or this sandbox) falls back to the env-var provider that
    existed in v3, unchanged.
    """
    global _provider
    if _provider is not None:
        return _provider

    backend = os.getenv("KMS_PROVIDER", "env").strip().lower()

    if backend in ("", "env"):
        from infra.kms.env_provider import EnvMasterKeyProvider

        _provider = EnvMasterKeyProvider()
    else:
        from infra.kms.kms_provider import KMSMasterKeyProvider

        _provider = KMSMasterKeyProvider(backend=backend)

    logger.info("KMS master key provider active: %s", type(_provider).__name__)
    return _provider
