"""Focused tests for the Codex holder fallback adapter."""

import os
import unittest
from unittest.mock import AsyncMock, patch

import domain.intelligence._codex_holder_fallback as codex


class CodexHolderFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_key_skips_provider(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(await codex.fetch_token_holders("Mint"))

    async def test_extracts_and_sorts_holder_accounts(self):
        body = {
            "data": {
                "holders": {
                    "items": [
                        {"address": "wallet-b", "balance": "20"},
                        {"address": "wallet-a", "balance": "100"},
                        {"address": "wallet-zero", "balance": "0"},
                    ],
                    "count": 2,
                    "status": "ENABLED",
                    "top10HoldersPercent": 44.2,
                }
            }
        }
        with patch.dict(os.environ, {"CODEX_API_KEY": "test-key"}, clear=True), \
             patch("aiohttp.ClientSession") as session_cls:
            response = AsyncMock()
            response.status = 200
            response.json = AsyncMock(return_value=body)
            session = AsyncMock()
            session.post.return_value.__aenter__.return_value = response
            session_cls.return_value.__aenter__.return_value = session
            result = await codex.fetch_token_holders("Mint")

        self.assertEqual(result, [
            {"owner": "wallet-a", "amount": "100"},
            {"owner": "wallet-b", "amount": "20"},
        ])

    async def test_http_failure_is_safe(self):
        with patch.dict(os.environ, {"CODEX_API_KEY": "test-key"}, clear=True), \
             patch("aiohttp.ClientSession") as session_cls:
            response = AsyncMock()
            response.status = 403
            response.json = AsyncMock(return_value={"errors": [{"message": "not authorized"}]})
            session = AsyncMock()
            session.post.return_value.__aenter__.return_value = response
            session_cls.return_value.__aenter__.return_value = session
            self.assertIsNone(await codex.fetch_token_holders("Mint"))

    async def test_graphql_failure_is_safe(self):
        body = {"errors": [{"message": "Not authorized: please upgrade your plan"}]}
        with patch.dict(os.environ, {"CODEX_API_KEY": "test-key"}, clear=True), \
             patch("aiohttp.ClientSession") as session_cls:
            response = AsyncMock()
            response.status = 200
            response.json = AsyncMock(return_value=body)
            session = AsyncMock()
            session.post.return_value.__aenter__.return_value = response
            session_cls.return_value.__aenter__.return_value = session
            self.assertIsNone(await codex.fetch_token_holders("Mint"))

    async def test_install_wraps_existing_fetch_only_when_codex_has_key(self):
        import domain.intelligence.holders as holders

        original = holders._fetch_token_accounts
        try:
            with patch.dict(os.environ, {"CODEX_API_KEY": "test-key"}, clear=True), \
                 patch.object(codex, "fetch_token_holders", AsyncMock(return_value=[{"owner": "w", "amount": "10"}])):
                codex.install()
                result = await holders._fetch_token_accounts("Mint")
            self.assertEqual(result.accounts[0]["owner"], "w")
        finally:
            holders._fetch_token_accounts = original


if __name__ == "__main__":
    unittest.main()
