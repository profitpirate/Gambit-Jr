from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

from memecoin_bot.models import iso
from memecoin_bot.providers.base import ProviderError, ResilientJsonClient


READ_ONLY_ROUTES = {
    "info": "/v1/token/info",
    "security": "/v1/token/security",
    "pool": "/v1/token/pool_info",
    "holders": "/v1/market/token_top_holders",
    "traders": "/v1/market/token_top_traders",
}


@dataclass(slots=True)
class GmgnSnapshot:
    chain: str
    token_address: str
    retrieved_at: str
    info: dict[str, Any] | None = None
    security: dict[str, Any] | None = None
    pool: dict[str, Any] | None = None
    holders: list[dict[str, Any]] | None = None
    traders: list[dict[str, Any]] | None = None
    unavailable: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__slots__}


class GmgnProvider:
    """Optional fail-open client for GMGN's official API-key-only read routes."""

    name = "gmgn"

    def __init__(self, base_url: str, api_key: str, client: ResilientJsonClient,
                 cache_ttl: float = 120, concurrency: int = 4):
        if not api_key:
            raise ValueError("A GMGN read-only API key is required")
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.client = client
        self.cache_ttl = cache_ttl
        self._cache: dict[tuple[str, str], tuple[float, GmgnSnapshot]] = {}
        self._inflight: dict[tuple[str, str], asyncio.Task[GmgnSnapshot]] = {}
        self._semaphore = asyncio.Semaphore(concurrency)

    @staticmethod
    def chain_name(chain: str) -> str:
        value = {"solana": "sol", "sol": "sol", "bsc": "bsc"}.get(chain.lower())
        if not value:
            raise ValueError(f"GMGN chain not supported: {chain}")
        return value

    def _headers(self) -> dict[str, str]:
        return {"X-APIKEY": self._api_key}

    async def _get(self, route: str, chain: str, address: str, limit: int | None = None) -> Any:
        if route not in READ_ONLY_ROUTES:
            raise ValueError("GMGN provider permits read-only token routes only")
        # Official exist-auth requires a fresh Unix timestamp and UUID client_id; no signature.
        query: dict[str, Any] = {"chain": self.chain_name(chain), "address": address,
                                 "timestamp": int(time.time()), "client_id": str(uuid.uuid4())}
        if limit is not None:
            query["limit"] = limit
        url = f"{self.base_url}{READ_ONLY_ROUTES[route]}?{urlencode(query)}"
        response = await self.client.request(url, headers=self._headers())
        if not isinstance(response, dict):
            raise ProviderError("GMGN returned a malformed response")
        code = response.get("code", 0)
        if code not in (0, 200, "0", "200", None):
            raise ProviderError(f"GMGN request rejected with code {code}")
        return response.get("data", response)

    async def enrich(self, chain: str, address: str) -> GmgnSnapshot:
        key = (self.chain_name(chain), address)
        cached = self._cache.get(key)
        if cached and time.monotonic() - cached[0] < self.cache_ttl:
            return cached[1]
        if key in self._inflight:
            return await self._inflight[key]
        task = asyncio.create_task(self._enrich_uncached(chain, address))
        self._inflight[key] = task
        try:
            result = await task
            self._cache[key] = (time.monotonic(), result)
            return result
        finally:
            self._inflight.pop(key, None)

    async def _enrich_uncached(self, chain: str, address: str) -> GmgnSnapshot:
        result = GmgnSnapshot(self.chain_name(chain), address, iso())
        async with self._semaphore:
            values = await asyncio.gather(
                self._get("info", chain, address), self._get("security", chain, address),
                self._get("pool", chain, address), self._get("holders", chain, address, 100),
                self._get("traders", chain, address, 100), return_exceptions=True,
            )
        for name, value in zip(("info", "security", "pool", "holders", "traders"), values):
            if isinstance(value, Exception):
                result.unavailable.append(name.upper())
            elif name in {"holders", "traders"}:
                if isinstance(value, dict):
                    value = value.get("list") or value.get("items") or value.get("holders") or value.get("traders")
                setattr(result, name, value if isinstance(value, list) else None)
                if not isinstance(value, list):
                    result.unavailable.append(name.upper())
            else:
                setattr(result, name, value if isinstance(value, dict) else None)
                if not isinstance(value, dict):
                    result.unavailable.append(name.upper())
        return result

    def redacted_config(self) -> dict[str, Any]:
        return {"enabled": True, "base_url": self.base_url, "cache_ttl_seconds": self.cache_ttl,
                "authentication": "X-APIKEY (redacted)", "mode": "READ_ONLY"}
