"""
infra/kms/kms_provider.py

NEW in v4 (§8 — Security Architecture). Implements the same
`MasterKeyProvider` interface as `EnvMasterKeyProvider` — nothing in
`wallet_crypto.py` (encrypt_secret/decrypt_secret) changes to use this.

Design: the 32-byte wallet master key is generated once (offline, by an
operator) and encrypted under a cloud KMS Customer Master Key (CMK). Only
the *ciphertext* of that key is ever stored, in
`WALLET_MASTER_KEY_ENCRYPTED` (base64 KMS ciphertext blob). On process
startup, this provider calls KMS `Decrypt` once, holds the plaintext
master key in memory for the process lifetime (the same threat-model
trade-off `wallet_crypto.py` already accepts for per-wallet data keys —
see its docstring), and never touches the KMS API again per-request. This
means:

  * The env var alone is useless to an attacker without also having IAM
    access to call Decrypt against the specific CMK — closing the exact
    gap the v3 docstring named ("anyone with server/host access can
    decrypt every wallet").
  * No per-signature latency hit from calling out to KMS on every trade.
  * Key rotation is a KMS-side operation (re-encrypt WALLET_MASTER_KEY
    under a new CMK version) without touching wallet_crypto.py at all.

Requires `boto3` and valid AWS credentials/IAM permissions for the AWS
backend; this module lazy-imports boto3 so a deployment using
KMS_PROVIDER=env (the default) never needs it installed. GCP KMS and
HashiCorp Vault backends follow the same shape and are stubbed below —
implement `_decrypt_via_gcp_kms` / `_decrypt_via_vault` analogously when
adopted; the public interface (`get_master_key`) does not change.

NOTE: this class has not been exercised against a live KMS endpoint in
this environment (no network access, no cloud credentials available here)
— it is written to the documented AWS KMS `Decrypt` API contract, but per
the same discipline the audit applied to lp_lock_checker.py and similar
self-flagged-unverified modules, it should be treated as
NOT-YET-LIVE-VALIDATED until it has been run once against a real CMK in
staging, before any production wallet is migrated to it.
"""

import base64
import logging
import os

from infra.kms.provider import MasterKeyProvider

logger = logging.getLogger("AlphaPulse.KMS")


class KMSMasterKeyProvider(MasterKeyProvider):
    def __init__(self, backend: str = "aws_kms") -> None:
        self._backend = backend
        self._cached_key: bytes | None = None

    def get_master_key(self) -> bytes:
        if self._cached_key is not None:
            return self._cached_key

        if self._backend == "aws_kms":
            self._cached_key = self._decrypt_via_aws_kms()
        elif self._backend == "gcp_kms":
            self._cached_key = self._decrypt_via_gcp_kms()
        elif self._backend == "vault":
            self._cached_key = self._decrypt_via_vault()
        else:
            raise RuntimeError(
                f"Unknown KMS_PROVIDER backend '{self._backend}'. "
                "Expected one of: aws_kms, gcp_kms, vault."
            )

        if len(self._cached_key) != 32:
            raise RuntimeError(
                f"Decrypted master key must be exactly 32 bytes, got {len(self._cached_key)}."
            )
        return self._cached_key

    def _decrypt_via_aws_kms(self) -> bytes:
        try:
            import boto3
        except ImportError as e:
            raise RuntimeError(
                "KMS_PROVIDER=aws_kms requires the 'boto3' package. "
                "Install it (pip install boto3) or set KMS_PROVIDER=env "
                "for local/dev use."
            ) from e

        ciphertext_b64 = os.getenv("WALLET_MASTER_KEY_ENCRYPTED")
        if not ciphertext_b64:
            raise RuntimeError(
                "WALLET_MASTER_KEY_ENCRYPTED is missing. Encrypt your "
                "32-byte master key under an AWS KMS CMK first, e.g.:\n"
                "  aws kms encrypt --key-id <cmk-id> "
                "--plaintext fileb://master.key --output text "
                "--query CiphertextBlob\n"
                "and set the resulting base64 blob as this env var."
            )

        region = os.getenv("AWS_REGION", "us-east-1")
        client = boto3.client("kms", region_name=region)

        response = client.decrypt(CiphertextBlob=base64.b64decode(ciphertext_b64))
        plaintext = response["Plaintext"]
        logger.info("Wallet master key decrypted via AWS KMS (region=%s)", region)
        return plaintext

    def _decrypt_via_gcp_kms(self) -> bytes:
        raise NotImplementedError(
            "GCP KMS backend not yet implemented. Follow the same shape as "
            "_decrypt_via_aws_kms using google-cloud-kms's "
            "KeyManagementServiceClient.decrypt(), reading the ciphertext "
            "from WALLET_MASTER_KEY_ENCRYPTED and the key resource name "
            "from an env var (e.g. GCP_KMS_KEY_NAME)."
        )

    def _decrypt_via_vault(self) -> bytes:
        raise NotImplementedError(
            "HashiCorp Vault backend not yet implemented. Follow the same "
            "shape as _decrypt_via_aws_kms using hvac's "
            "transit.decrypt_data(), reading the ciphertext from "
            "WALLET_MASTER_KEY_ENCRYPTED and the Vault transit key name "
            "from an env var (e.g. VAULT_TRANSIT_KEY)."
        )
