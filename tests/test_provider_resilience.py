from __future__ import annotations

import unittest

from memecoin_bot.providers.base import ProviderError
from memecoin_bot.providers.dexscreener import DexScreenerProvider


class PartiallyFailingClient:
    async def request(self, url: str, method: str = "GET", payload: dict | None = None):
        if "token-profiles" in url:
            raise ProviderError("profile feed down")
        if "token-boosts" in url:
            return [{"chainId": "solana", "tokenAddress": "RealAddress", "amount": 1}]
        return []


class ProviderResilienceTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_discovery_feed_failure_does_not_crash(self) -> None:
        provider = DexScreenerProvider("https://example.invalid", PartiallyFailingClient())
        events = await provider.discover()
        self.assertEqual([event.token_address for event in events], ["RealAddress"])


if __name__ == "__main__":
    unittest.main()

