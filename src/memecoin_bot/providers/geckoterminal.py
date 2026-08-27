from __future__ import annotations

from memecoin_bot.models import DiscoveryEvent, iso
from memecoin_bot.providers.base import ResilientJsonClient


class GeckoTerminalDiscoveryProvider:
    """Keyless public new-pool discovery; market snapshots remain canonical DexScreener data."""

    def __init__(self, base_url: str, client: ResilientJsonClient, chain: str):
        self.base_url = base_url.rstrip("/")
        self.client = client
        self.chain = chain
        self.network = {"solana": "solana", "bsc": "bsc"}[chain]
        self.name = f"geckoterminal_{chain}_new_pools"

    async def discover(self) -> list[DiscoveryEvent]:
        data = await self.client.request(f"{self.base_url}/networks/{self.network}/new_pools")
        included = (
            {
                str(item.get("id")): item
                for item in data.get("included", [])
                if isinstance(item, dict)
            }
            if isinstance(data, dict)
            else {}
        )
        events: list[DiscoveryEvent] = []
        for pool in data.get("data", []) if isinstance(data, dict) else []:
            if not isinstance(pool, dict):
                continue
            attrs = pool.get("attributes") or {}
            relationships = pool.get("relationships") or {}
            base_id = ((relationships.get("base_token") or {}).get("data") or {}).get("id")
            base = included.get(str(base_id), {}).get("attributes", {})
            address = base.get("address") or (str(base_id).split("_", 1)[-1] if base_id else None)
            if not address:
                continue
            events.append(
                DiscoveryEvent(
                    token_address=str(address),
                    chain=self.chain,
                    symbol=base.get("symbol"),
                    name=base.get("name"),
                    source=self.name,
                    discovered_at=iso(),
                    estimated_creation_timestamp=attrs.get("pool_created_at"),
                    pair_address=attrs.get("address"),
                    metadata={
                        "description": None,
                        "pool_name": attrs.get("name"),
                        "pool_created_at": attrs.get("pool_created_at"),
                        "source_pool_id": pool.get("id"),
                    },
                )
            )
        return events
