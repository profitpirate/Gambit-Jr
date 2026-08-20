from __future__ import annotations

from memecoin_bot.models import DiscoveryEvent
from memecoin_bot.providers.dexscreener import DexScreenerProvider


class DiscoveryPoller:
    def __init__(self, provider: DexScreenerProvider):
        self.provider = provider

    async def poll(self) -> list[DiscoveryEvent]:
        return await self.provider.discover()
