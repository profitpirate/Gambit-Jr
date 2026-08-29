from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlencode

import aiohttp

JsonRequester = Callable[..., Awaitable[tuple[Any, float]]]


@dataclass(frozen=True, slots=True)
class ProviderObservation:
    events_seen: int
    tokens_seen: int
    latencies_ms: tuple[float, ...]
    coverage: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)
    errors: tuple[str, ...] = ()


async def request_json(
    url: str,
    timeout: float,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[Any, float]:
    begun = time.perf_counter()
    client_timeout = aiohttp.ClientTimeout(total=timeout)
    async with (
        aiohttp.ClientSession(timeout=client_timeout) as session,
        session.request(method, url, json=body, headers=headers) as response,
    ):
        response.raise_for_status()
        payload = await response.json()
    return payload, (time.perf_counter() - begun) * 1000


async def websocket_slot(url: str, timeout: float) -> tuple[int, float]:
    """Prove one standard Solana slot subscription without retaining a credentialed URL."""
    begun = time.perf_counter()
    client_timeout = aiohttp.ClientTimeout(total=timeout)
    async with (
        aiohttp.ClientSession(timeout=client_timeout) as session,
        session.ws_connect(url, heartbeat=20, autoping=True) as websocket,
    ):
        await websocket.send_json(
            {"jsonrpc": "2.0", "id": 1, "method": "slotSubscribe", "params": []}
        )
        subscribed = await websocket.receive_json(timeout=timeout)
        if not subscribed.get("result"):
            raise ValueError("Solana WebSocket omitted subscription id")
        event = await websocket.receive_json(timeout=timeout)
    slot = int(((event.get("params") or {}).get("result") or {}).get("slot"))
    return slot, (time.perf_counter() - begun) * 1000


class BirdeyeClient:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://public-api.birdeye.so",
        requester: JsonRequester = request_json,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.requester = requester

    async def holder_profile(self, token_address: str, timeout: float) -> ProviderObservation:
        query = urlencode({"address": token_address})
        payload, latency = await self.requester(
            f"{self.base_url}/token/v1/holder-profile?{query}",
            timeout,
            headers={"X-API-KEY": self.api_key, "x-chain": "solana"},
        )
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise TypeError("Birdeye holder profile omitted data object")
        fields = set(data)
        label_terms = ("sniper", "insider", "bundle", "developer", "smart", "trader")
        label_fields = sorted(name for name in fields if any(term in name.lower() for term in label_terms))
        holder_count = next(
            (
                int(data[name])
                for name in ("holder_count", "holderCount", "total_holders", "totalHolders")
                if data.get(name) is not None
            ),
            None,
        )
        return ProviderObservation(
            events_seen=1,
            tokens_seen=1,
            latencies_ms=(latency,),
            coverage={
                "holder_profile": True,
                "holder_count_present": holder_count is not None,
                "label_fields_present": label_fields,
                "plan_dependent_fields_not_assumed": True,
            },
            evidence={"schema_parsed": True, "credential_redacted": True},
        )

    async def probe(self, token_address: str, timeout: float) -> ProviderObservation:
        query = urlencode({"address": token_address})
        payload, token_latency = await self.requester(
            f"{self.base_url}/defi/token_overview?{query}",
            timeout,
            headers={"X-API-KEY": self.api_key, "x-chain": "solana"},
        )
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict) or not data:
            raise TypeError("Birdeye token overview omitted data object")
        errors = []
        latencies = [token_latency]
        holder_coverage: dict[str, Any] = {"holder_profile": False}
        try:
            holder = await self.holder_profile(token_address, timeout)
            latencies.extend(holder.latencies_ms)
            holder_coverage = holder.coverage
        except aiohttp.ClientResponseError as error:
            errors.append(f"holder profile unavailable on current plan (HTTP {error.status})")
        return ProviderObservation(
            events_seen=1 + int(holder_coverage.get("holder_profile", False)),
            tokens_seen=1,
            latencies_ms=tuple(latencies),
            coverage={"token_overview": True, **holder_coverage},
            evidence={"authentication_proven": True, "credential_redacted": True},
            errors=tuple(errors),
        )

    async def holder_distribution(self, token_address: str, timeout: float) -> dict[str, Any]:
        payload, _latency = await self.requester(
            f"{self.base_url}/holder/v1/distribution?{urlencode({'address': token_address})}",
            timeout,
            headers={"X-API-KEY": self.api_key, "x-chain": "solana"},
        )
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise TypeError("Birdeye holder distribution omitted data object")
        return data

    async def wallet_token_activity(self, wallet: str, timeout: float) -> list[dict[str, Any]]:
        payload, _latency = await self.requester(
            f"{self.base_url}/v1/wallet/token_list?{urlencode({'wallet': wallet})}",
            timeout,
            headers={"X-API-KEY": self.api_key, "x-chain": "solana"},
        )
        data = payload.get("data") if isinstance(payload, dict) else None
        rows = data.get("items", data.get("tokens", [])) if isinstance(data, dict) else data
        if not isinstance(rows, list):
            raise TypeError("Birdeye wallet token activity omitted item list")
        return [row for row in rows if isinstance(row, dict)]


class SolanaTrackerClient:
    def __init__(
        self,
        api_key: str,
        *,
        rpc_url: str | None = None,
        wss_url: str | None = None,
        data_url: str = "https://data.solanatracker.io",
        requester: JsonRequester = request_json,
    ):
        self.api_key = api_key
        self.rpc_url = rpc_url or f"https://rpc-mainnet.solanatracker.io?api_key={quote(api_key)}"
        self.wss_url = wss_url or f"wss://rpc-mainnet.solanatracker.io?api_key={quote(api_key)}"
        self.data_url = data_url.rstrip("/")
        self.requester = requester

    async def probe(self, token_address: str, timeout: float) -> ProviderObservation:
        rpc, rpc_latency = await self.requester(
            self.rpc_url,
            timeout,
            method="POST",
            body={"jsonrpc": "2.0", "id": 1, "method": "getSlot", "params": []},
        )
        slot = int(rpc["result"])
        indexed, indexed_latency = await self.requester(
            f"{self.data_url}/price?{urlencode({'token': token_address})}",
            timeout,
            headers={"x-api-key": self.api_key},
        )
        if not isinstance(indexed, dict) or not indexed:
            raise ValueError("Solana Tracker indexed token response was empty")
        token, token_latency = await self.token_intelligence(token_address, timeout)
        ws_slot, ws_latency = await websocket_slot(self.wss_url, timeout)
        return ProviderObservation(
            events_seen=4,
            tokens_seen=1,
            latencies_ms=(rpc_latency, indexed_latency, token_latency, ws_latency),
            coverage={
                "rpc_get_slot": True,
                "websocket_slot_subscription": True,
                "indexed_token_lookup": True,
                "market_cross_check": True,
                "token_metadata": bool(token),
            },
            evidence={
                "rpc_slot_observed": slot > 0,
                "websocket_slot_observed": ws_slot > 0,
                "credential_redacted": True,
            },
        )

    async def token_intelligence(
        self, token_address: str, timeout: float
    ) -> tuple[dict[str, Any], float]:
        payload, latency = await self.requester(
            f"{self.data_url}/tokens/{quote(token_address)}",
            timeout,
            headers={"x-api-key": self.api_key},
        )
        if not isinstance(payload, dict) or not payload:
            raise TypeError("Solana Tracker token intelligence response was empty")
        return payload, latency


class AlchemySolanaClient:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        rpc_url: str | None = None,
        wss_url: str | None = None,
        requester: JsonRequester = request_json,
    ):
        if not api_key and not rpc_url:
            raise ValueError("Alchemy requires ALCHEMY_API_KEY or ALCHEMY_SOLANA_RPC_URL")
        self.rpc_url = rpc_url or f"https://solana-mainnet.g.alchemy.com/v2/{quote(str(api_key))}"
        self.wss_url = wss_url or f"wss://solana-mainnet.g.alchemy.com/v2/{quote(str(api_key))}"
        self.requester = requester

    async def probe(self, timeout: float) -> ProviderObservation:
        payload, rpc_latency = await self.requester(
            self.rpc_url,
            timeout,
            method="POST",
            body={"jsonrpc": "2.0", "id": 1, "method": "getSlot", "params": []},
        )
        slot = int(payload["result"])
        latencies = [rpc_latency]
        websocket_ok = False
        errors = []
        if self.wss_url:
            try:
                ws_slot, ws_latency = await websocket_slot(self.wss_url, timeout)
                latencies.append(ws_latency)
                websocket_ok = ws_slot > 0
            except (aiohttp.ClientError, TimeoutError, TypeError, ValueError) as error:
                status = getattr(error, "status", None)
                suffix = f" (HTTP {status})" if status else ""
                errors.append(f"WebSocket subscription unavailable{suffix}")
        return ProviderObservation(
            events_seen=1 + int(websocket_ok),
            tokens_seen=0,
            latencies_ms=tuple(latencies),
            coverage={"rpc_get_slot": True, "websocket_slot_subscription": websocket_ok},
            evidence={"slot_observed": slot > 0, "credential_redacted": True},
            errors=tuple(errors),
        )


class ShyftSolanaClient:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        rpc_url: str | None = None,
        requester: JsonRequester = request_json,
    ):
        if not api_key and not rpc_url:
            raise ValueError("Shyft requires SHYFT_API_KEY or SHYFT_SOLANA_RPC_URL")
        self.rpc_url = rpc_url or f"https://rpc.shyft.to/?api_key={quote(str(api_key))}"
        self.requester = requester

    async def probe(self, timeout: float) -> ProviderObservation:
        payload, latency = await self.requester(
            self.rpc_url,
            timeout,
            method="POST",
            body={"jsonrpc": "2.0", "id": 1, "method": "getSlot", "params": []},
        )
        return ProviderObservation(
            events_seen=1,
            tokens_seen=0,
            latencies_ms=(latency,),
            coverage={"rpc_get_slot": True, "fallback_only": True},
            evidence={"slot_observed": int(payload["result"]) > 0, "credential_redacted": True},
        )


class SolscanClient:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://pro-api.solscan.io/v2.0",
        requester: JsonRequester = request_json,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.requester = requester

    async def token_holders(self, token_address: str, timeout: float) -> ProviderObservation:
        query = urlencode({"address": token_address, "page": 1, "page_size": 10})
        payload, latency = await self.requester(
            f"{self.base_url}/token/holders?{query}",
            timeout,
            headers={"token": self.api_key},
        )
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, (dict, list)):
            raise TypeError("Solscan token holder response omitted data")
        rows = data.get("items", data.get("data", [])) if isinstance(data, dict) else data
        return ProviderObservation(
            events_seen=max(1, len(rows) if isinstance(rows, list) else 1),
            tokens_seen=1,
            latencies_ms=(latency,),
            coverage={"indexed_holder_cross_check": True, "page_size": 10},
            evidence={"schema_parsed": True, "credential_redacted": True},
        )

    async def probe(self, token_address: str, timeout: float) -> ProviderObservation:
        query = urlencode({"address": token_address})
        payload, token_latency = await self.requester(
            f"{self.base_url}/token/meta?{query}", timeout, headers={"token": self.api_key}
        )
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict) or not data:
            raise TypeError("Solscan token metadata omitted data object")
        latencies = [token_latency]
        errors = []
        holder_available = False
        try:
            holder = await self.token_holders(token_address, timeout)
            latencies.extend(holder.latencies_ms)
            holder_available = True
        except aiohttp.ClientResponseError as error:
            errors.append(f"holder endpoint unavailable on current plan (HTTP {error.status})")
        return ProviderObservation(
            events_seen=1 + int(holder_available),
            tokens_seen=1,
            latencies_ms=tuple(latencies),
            coverage={"token_metadata": True, "indexed_holder_cross_check": holder_available},
            evidence={"authentication_proven": True, "credential_redacted": True},
            errors=tuple(errors),
        )

    async def token_metadata(self, token_address: str, timeout: float) -> dict[str, Any]:
        query = urlencode({"address": token_address})
        payload, _latency = await self.requester(
            f"{self.base_url}/token/meta?{query}", timeout, headers={"token": self.api_key}
        )
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise TypeError("Solscan token metadata omitted data object")
        return data

    async def transaction_detail(self, signature: str, timeout: float) -> dict[str, Any]:
        query = urlencode({"tx": signature})
        payload, _latency = await self.requester(
            f"{self.base_url}/transaction/detail?{query}",
            timeout,
            headers={"token": self.api_key},
        )
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise TypeError("Solscan transaction detail omitted data object")
        return data


class CoinGeckoContextClient:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = "https://api.coingecko.com/api/v3",
        requester: JsonRequester = request_json,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.requester = requester

    async def sol_context(self, timeout: float) -> ProviderObservation:
        headers = {"x-cg-demo-api-key": self.api_key} if self.api_key else None
        query = urlencode(
            {
                "ids": "solana",
                "vs_currencies": "usd",
                "include_24hr_change": "true",
                "include_last_updated_at": "true",
            }
        )
        payload, latency = await self.requester(
            f"{self.base_url}/simple/price?{query}", timeout, headers=headers
        )
        solana = payload.get("solana") if isinstance(payload, dict) else None
        if not isinstance(solana, dict) or solana.get("usd") is None:
            raise ValueError("CoinGecko SOL context response omitted USD price")
        return ProviderObservation(
            events_seen=1,
            tokens_seen=1,
            latencies_ms=(latency,),
            coverage={
                "sol_price": True,
                "sol_24h_change": solana.get("usd_24h_change") is not None,
                "slow_context_only": True,
            },
            evidence={"schema_parsed": True, "credential_redacted": bool(self.api_key)},
        )

    async def sol_regime(self, timeout: float, *, days: int = 7) -> dict[str, Any]:
        headers = {"x-cg-demo-api-key": self.api_key} if self.api_key else None
        query = urlencode(
            {"vs_currency": "usd", "days": min(max(int(days), 2), 90), "interval": "daily"}
        )
        payload, latency = await self.requester(
            f"{self.base_url}/coins/solana/market_chart?{query}", timeout, headers=headers
        )
        prices = payload.get("prices") if isinstance(payload, dict) else None
        if not isinstance(prices, list) or len(prices) < 2:
            raise TypeError("CoinGecko SOL market chart omitted price history")
        values = [float(item[1]) for item in prices if isinstance(item, list) and len(item) > 1]
        returns = [values[index] / values[index - 1] - 1 for index in range(1, len(values))]
        mean = sum(returns) / len(returns)
        variance = sum((value - mean) ** 2 for value in returns) / len(returns)
        return {
            "sol_trend": values[-1] / values[0] - 1,
            "sol_volatility": variance**0.5,
            "risk_on": values[-1] > values[0],
            "observations": len(values),
            "latency_ms": latency,
            "slow_context_only": True,
        }
