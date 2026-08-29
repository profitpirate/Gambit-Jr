from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from memecoin_bot.models import DiscoveryEvent, MarketSnapshot, iso
from memecoin_bot.providers.base import ProviderError, ResilientJsonClient


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _integer(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None else None


def _items(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict) and data.get("tokenAddress"):
        return [data]
    return []


class DexScreenerProvider:
    name = "dexscreener"

    def __init__(self, base_url: str, client: ResilientJsonClient):
        self.base_url = base_url.rstrip("/")
        self.client = client

    async def discover(self) -> list[DiscoveryEvent]:
        """Discover latest Solana/BSC profile, boost and takeover activations."""
        endpoints = {
            "dexscreener_profile": "/token-profiles/latest/v1",
            "dexscreener_boost": "/token-boosts/latest/v1",
            "dexscreener_takeover": "/community-takeovers/latest/v1",
        }
        events: dict[str, DiscoveryEvent] = {}
        for source, endpoint in endpoints.items():
            try:
                data = await self.client.request(self.base_url + endpoint)
            except ProviderError:
                # These are independent discovery feeds. A profile outage must not
                # suppress boost/takeover discovery (or crash the scanner).
                continue
            for item in _items(data):
                raw_chain = str(item.get("chainId", "")).lower()
                chain = {"solana": "solana", "bsc": "bsc"}.get(raw_chain)
                if chain is None:
                    continue
                address = item.get("tokenAddress")
                if not isinstance(address, str) or not address:
                    continue
                events.setdefault(
                    address,
                    DiscoveryEvent(
                        token_address=address,
                        chain=chain,
                        source=source,
                        metadata={
                            "description": item.get("description"),
                            "links": item.get("links") or [],
                            "profile_url": item.get("url"),
                            "boost_amount": item.get("amount"),
                            "boost_total": item.get("totalAmount"),
                            "claim_date": item.get("claimDate"),
                        },
                    ),
                )
        return list(events.values())

    async def market_snapshot(
        self, token_address: str, chain: str = "solana"
    ) -> MarketSnapshot | None:
        data = await self.client.request(f"{self.base_url}/token-pairs/v1/{chain}/{token_address}")
        return self._market_snapshot_from_pairs(
            token_address, chain, _items(data), allow_unmatched=True
        )

    async def market_snapshots(
        self, token_addresses: list[str], chain: str = "solana"
    ) -> dict[str, MarketSnapshot | None]:
        """Fetch candidate markets in DexScreener's documented 30-token batches.

        Missing tokens are returned as ``None``. A failed chunk is omitted so the
        caller can retry those addresses individually without turning one provider
        error into a batch-wide false negative.
        """
        addresses = list(dict.fromkeys(value for value in token_addresses if value))
        output: dict[str, MarketSnapshot | None] = {}
        for offset in range(0, len(addresses), 30):
            chunk = addresses[offset : offset + 30]
            try:
                data = await self.client.request(
                    f"{self.base_url}/tokens/v1/{chain}/{','.join(chunk)}"
                )
            except ProviderError:
                continue
            pairs = _items(data)
            for address in chunk:
                output[address] = self._market_snapshot_from_pairs(address, chain, pairs)
        return output

    def _market_snapshot_from_pairs(
        self,
        token_address: str,
        chain: str,
        pairs: list[dict[str, Any]],
        *,
        allow_unmatched: bool = False,
    ) -> MarketSnapshot | None:
        if not pairs:
            return None
        matching = [p for p in pairs if (p.get("baseToken") or {}).get("address") == token_address]
        if not matching and not allow_unmatched:
            return None
        matching = matching or pairs
        pair = max(matching, key=lambda p: _number((p.get("liquidity") or {}).get("usd")) or -1)
        base = pair.get("baseToken") or {}
        txns = pair.get("txns") or {}
        volume = pair.get("volume") or {}
        changes = pair.get("priceChange") or {}
        info = pair.get("info") or {}
        created_ms = _number(pair.get("pairCreatedAt"))
        created_at = None
        if created_ms is not None:
            created_at = datetime.fromtimestamp(created_ms / 1000, tz=UTC).isoformat()
        labels = [str(x).lower() for x in pair.get("labels") or []]
        launchpad = next((x for x in labels if "pump" in x or "moon" in x), None)
        socials = [
            {str(k): str(v) for k, v in social.items() if v is not None}
            for social in info.get("socials") or []
            if isinstance(social, dict)
        ]
        return MarketSnapshot(
            token_address=token_address,
            captured_at=iso(),
            source=self.name,
            chain=chain,
            pair_address=pair.get("pairAddress"),
            symbol=base.get("symbol"),
            name=base.get("name"),
            dex=pair.get("dexId"),
            launchpad=launchpad,
            pair_created_at=created_at,
            price_usd=_number(pair.get("priceUsd")),
            market_cap_usd=_number(pair.get("marketCap")),
            fdv_usd=_number(pair.get("fdv")),
            liquidity_usd=_number((pair.get("liquidity") or {}).get("usd")),
            volume_5m_usd=_number(volume.get("m5")),
            volume_1h_usd=_number(volume.get("h1")),
            buys_5m=_integer((txns.get("m5") or {}).get("buys")),
            sells_5m=_integer((txns.get("m5") or {}).get("sells")),
            price_change_5m=_number(changes.get("m5")),
            websites=[str(x.get("url")) for x in info.get("websites") or [] if x.get("url")],
            socials=socials,
        )
