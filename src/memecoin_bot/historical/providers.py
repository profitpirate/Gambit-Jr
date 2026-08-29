from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiohttp

from .backfill import BackfillPage
from .store import RawEvidence

JsonFetcher = Callable[[str], Awaitable[Any]]
JsonRequester = Callable[[str, dict[str, Any]], Awaitable[Any]]


def _iso_from_milliseconds(value: int) -> str:
    return datetime.fromtimestamp(value / 1000, UTC).isoformat()


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


class PublicJsonClient:
    """Small credential-free client with explicit 429 handling and bounded retries."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 20,
        maximum_attempts: int = 6,
        minimum_interval_seconds: float = 2.1,
        extra_headers: dict[str, str] | None = None,
    ):
        self.timeout_seconds = timeout_seconds
        self.maximum_attempts = maximum_attempts
        self.minimum_interval_seconds = minimum_interval_seconds
        self.extra_headers = extra_headers or {}
        self._last_request = 0.0

    async def fetch(self, url: str) -> Any:
        for attempt in range(self.maximum_attempts):
            delay = self.minimum_interval_seconds - (time.monotonic() - self._last_request)
            if delay > 0:
                await __import__("asyncio").sleep(delay)
            timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
            async with aiohttp.ClientSession(timeout=timeout) as session, session.get(
                url,
                    headers={
                        "Accept": "application/json",
                        "User-Agent": "Gambit-Jr-Historical-Research/1.5",
                        **self.extra_headers,
                    },
            ) as response:
                self._last_request = time.monotonic()
                if response.status == 429:
                    retry_after = response.headers.get("Retry-After")
                    wait = float(retry_after) if retry_after else min(60, 5 * (attempt + 1))
                    await __import__("asyncio").sleep(wait)
                    continue
                response.raise_for_status()
                return await response.json(content_type=None)
        raise RuntimeError("public provider rate limit did not recover within bounded retries")


class BinanceKlineProvider:
    """True historical market-regime candles from Binance's public market-data API."""

    name = "binance_public_spot"

    def __init__(
        self,
        symbol: str,
        start: str,
        end: str,
        *,
        interval: str = "1d",
        fetch_json: JsonFetcher | None = None,
        base_url: str = "https://api.binance.com",
    ):
        self.symbol = symbol.upper()
        self.dataset_id = f"binance-{self.symbol.lower()}-{interval}-regime"
        self.start_ms = int(datetime.fromisoformat(start).astimezone(UTC).timestamp() * 1000)
        self.end_ms = int(datetime.fromisoformat(end).astimezone(UTC).timestamp() * 1000)
        self.interval = interval
        self.fetch_json = fetch_json or PublicJsonClient(minimum_interval_seconds=0.15).fetch
        self.base_url = base_url.rstrip("/")

    async def fetch_page(self, cursor: Any) -> BackfillPage:
        start_ms = int((cursor or {}).get("start_ms", self.start_ms))
        url = (
            f"{self.base_url}/api/v3/klines?symbol={self.symbol}&interval={self.interval}"
            f"&startTime={start_ms}&endTime={self.end_ms}&limit=1000"
        )
        payload = await self.fetch_json(url)
        records = []
        for row in payload:
            open_ms, close_ms = int(row[0]), int(row[6])
            records.append(
                RawEvidence(
                    dataset_id=self.dataset_id,
                    provider=self.name,
                    chain="market",
                    entity_type="market_regime",
                    entity_id=self.symbol,
                    source_timestamp=_iso_from_milliseconds(open_ms),
                    availability_timestamp=_iso_from_milliseconds(close_ms),
                    endpoint_type="public_spot_kline",
                    payload={
                        "symbol": self.symbol,
                        "interval": self.interval,
                        "open_time_ms": open_ms,
                        "open": row[1],
                        "high": row[2],
                        "low": row[3],
                        "close": row[4],
                        "volume": row[5],
                        "close_time_ms": close_ms,
                        "quote_volume": row[7],
                        "trade_count": row[8],
                        "taker_buy_base_volume": row[9],
                        "taker_buy_quote_volume": row[10],
                    },
                    schema_version="binance-kline-v3",
                    acquisition_version="v1.5-finalization-v1",
                    provenance={
                        "endpoint": "/api/v3/klines",
                        "credential_required": False,
                        "historical_semantics": "candle available only at close_time",
                    },
                )
            )
        next_start = int(payload[-1][6]) + 1 if payload else self.end_ms + 1
        return BackfillPage(
            records,
            {"start_ms": next_start} if next_start <= self.end_ms and payload else None,
            None,
            metadata={"request_url": url, "selection": "complete symbol time range"},
        )


class GeckoTerminalPoolProvider:
    """Current pool discovery with acquisition-time truth; never backdates live values."""

    name = "coingecko_geckoterminal_public"

    def __init__(
        self,
        network: str,
        *,
        endpoint: str = "new_pools",
        maximum_pages: int = 10,
        fetch_json: JsonFetcher | None = None,
        base_url: str = "https://api.geckoterminal.com/api/v2",
    ):
        if endpoint not in {"new_pools", "pools"}:
            raise ValueError("unsupported GeckoTerminal pool endpoint")
        self.network = network
        self.endpoint = endpoint
        self.maximum_pages = min(10, maximum_pages)
        self.dataset_id = f"geckoterminal-{network}-{endpoint}-snapshots"
        self.fetch_json = fetch_json or PublicJsonClient().fetch
        self.base_url = base_url.rstrip("/")
        self.discovered: list[dict[str, Any]] = []

    async def fetch_page(self, cursor: Any) -> BackfillPage:
        page = int((cursor or {}).get("page", 1))
        url = (
            f"{self.base_url}/networks/{self.network}/{self.endpoint}?page={page}"
            "&include=base_token%2Cquote_token%2Cdex"
        )
        response = await self.fetch_json(url)
        acquired_at = _utc_now()
        rows = response.get("data") or []
        self.discovered.extend(rows)
        records = []
        for row in rows:
            attributes = row.get("attributes") or {}
            pool = str(attributes.get("address") or row.get("id"))
            records.append(
                RawEvidence(
                    dataset_id=self.dataset_id,
                    provider=self.name,
                    chain=self.network,
                    entity_type="dex_pool",
                    entity_id=pool,
                    # Pool creation is metadata. Current liquidity/volume did not exist then.
                    source_timestamp=acquired_at,
                    availability_timestamp=acquired_at,
                    endpoint_type=f"current_{self.endpoint}_snapshot",
                    payload=row,
                    schema_version="geckoterminal-json-api-v2",
                    acquisition_version="v1.5-finalization-v1",
                    provenance={
                        "endpoint": f"/networks/{self.network}/{self.endpoint}",
                        "page": page,
                        "credential_required": False,
                        "selection_bias": (
                            "latest-window" if self.endpoint == "new_pools" else "provider-ranked"
                        ),
                        "pool_created_at_is_metadata_not_observation_time": True,
                    },
                )
            )
        next_page = page + 1
        return BackfillPage(
            records,
            {"page": next_page}
            if rows and next_page <= self.maximum_pages
            else None,
            None,
            metadata={"request_url": url, "maximum_free_page": 10},
        )


class GeckoTerminalOhlcvProvider:
    """Historical OHLCV for an explicitly recorded, survivor-biased pool sample."""

    name = "coingecko_geckoterminal_public"

    def __init__(
        self,
        network: str,
        pools: list[dict[str, Any]],
        *,
        timeframe: str = "day",
        aggregate: int = 1,
        limit: int = 1000,
        fetch_json: JsonFetcher | None = None,
        base_url: str = "https://api.geckoterminal.com/api/v2",
    ):
        self.network = network
        self.pools = pools
        self.timeframe = timeframe
        self.aggregate = aggregate
        self.limit = min(1000, limit)
        self.dataset_id = f"geckoterminal-{network}-ranked-pool-{timeframe}-ohlcv"
        self.fetch_json = fetch_json or PublicJsonClient().fetch
        self.base_url = base_url.rstrip("/")

    async def fetch_page(self, cursor: Any) -> BackfillPage:
        index = int((cursor or {}).get("pool_index", 0))
        if index >= len(self.pools):
            return BackfillPage([], None)
        pool = self.pools[index]
        attributes = pool.get("attributes") or {}
        address = str(attributes.get("address") or pool.get("address") or pool.get("id"))
        url = (
            f"{self.base_url}/networks/{self.network}/pools/{address}/ohlcv/"
            f"{self.timeframe}?aggregate={self.aggregate}&limit={self.limit}"
            "&currency=usd&token=base"
        )
        response = await self.fetch_json(url)
        rows = ((response.get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
        interval = {"minute": 60, "hour": 3600, "day": 86400}[self.timeframe] * self.aggregate
        records = []
        for candle in rows:
            opened = datetime.fromtimestamp(int(candle[0]), UTC)
            available = opened + timedelta(seconds=interval)
            records.append(
                RawEvidence(
                    dataset_id=self.dataset_id,
                    provider=self.name,
                    chain=self.network,
                    entity_type="dex_pool",
                    entity_id=address,
                    source_timestamp=opened.isoformat(),
                    availability_timestamp=available.isoformat(),
                    endpoint_type="historical_pool_ohlcv",
                    payload={
                        "pool_address": address,
                        "pool_name": attributes.get("name") or pool.get("name"),
                        "pool_created_at": attributes.get("pool_created_at")
                        or pool.get("pool_created_at"),
                        "timeframe": self.timeframe,
                        "aggregate": self.aggregate,
                        "timestamp": int(candle[0]),
                        "open": candle[1],
                        "high": candle[2],
                        "low": candle[3],
                        "close": candle[4],
                        "volume": candle[5],
                    },
                    schema_version="geckoterminal-ohlcv-v2",
                    acquisition_version="v1.5-finalization-v1",
                    provenance={
                        "endpoint": "pool OHLCV",
                        "credential_required": False,
                        "universe_selection": "current provider-ranked pools",
                        "survivorship_bias": "HIGH",
                        "not_launch_complete": True,
                    },
                )
            )
        next_index = index + 1
        return BackfillPage(
            records,
            {"pool_index": next_index} if next_index < len(self.pools) else None,
            len(self.pools) - next_index,
            metadata={"request_url": url, "pool_address": address},
        )


class DuneQueryProvider:
    """Checkpointed Dune result adapter; rows without availability use acquisition time."""

    name = "dune_api"

    def __init__(
        self,
        query_id: int,
        api_key: str | None,
        *,
        chain: str,
        entity_field: str,
        observed_at_field: str,
        available_at_field: str | None = None,
        page_size: int = 1000,
        fetch_json: JsonFetcher | None = None,
        base_url: str = "https://api.dune.com/api/v1",
    ):
        if not api_key and fetch_json is None:
            raise ValueError("DUNE_API_KEY is required")
        self.query_id = int(query_id)
        self.dataset_id = f"dune-query-{self.query_id}"
        self.chain = chain
        self.entity_field = entity_field
        self.observed_at_field = observed_at_field
        self.available_at_field = available_at_field
        self.page_size = min(1000, page_size)
        self.fetch_json = fetch_json or PublicJsonClient(
            minimum_interval_seconds=1,
            extra_headers={"X-Dune-API-Key": str(api_key)},
        ).fetch
        self.base_url = base_url.rstrip("/")

    async def fetch_page(self, cursor: Any) -> BackfillPage:
        offset = int((cursor or {}).get("offset", 0))
        url = (
            f"{self.base_url}/query/{self.query_id}/results?limit={self.page_size}"
            f"&offset={offset}&allow_partial_results=false"
        )
        response = await self.fetch_json(url)
        result = response.get("result") or {}
        rows = result.get("rows") or []
        acquired_at = _utc_now()
        records = []
        for row in rows:
            observed = str(row[self.observed_at_field])
            available = (
                str(row[self.available_at_field])
                if self.available_at_field and row.get(self.available_at_field)
                else acquired_at
            )
            records.append(
                RawEvidence(
                    dataset_id=self.dataset_id,
                    provider=self.name,
                    chain=self.chain,
                    entity_type="dune_query_row",
                    entity_id=str(row[self.entity_field]),
                    source_timestamp=observed,
                    availability_timestamp=available,
                    endpoint_type="dune_query_result",
                    payload=row,
                    schema_version=f"dune-query-{self.query_id}",
                    acquisition_version="v1.5-finalization-v1",
                    provenance={
                        "query_id": self.query_id,
                        "offset": offset,
                        "availability_from_source": bool(self.available_at_field),
                        "credential_name": "DUNE_API_KEY",
                    },
                )
            )
        total = int(result.get("metadata", {}).get("total_row_count") or len(rows) + offset)
        next_offset = offset + len(rows)
        return BackfillPage(
            records,
            {"offset": next_offset} if rows and next_offset < total else None,
            max(0, total - next_offset),
            metadata={"query_id": self.query_id, "total_rows": total},
        )


class DuneExecutionClient:
    """Bounded Dune execute/poll/result client for reviewed, parameterized queries."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.dune.com/api/v1",
        timeout_seconds: float = 30,
        poll_seconds: float = 2,
        maximum_polls: int = 900,
    ):
        if not api_key:
            raise ValueError("DUNE_API_KEY is required")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.poll_seconds = poll_seconds
        self.maximum_polls = maximum_polls

    async def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        headers = {"X-Dune-API-Key": self.api_key}
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            for attempt in range(6):
                async with session.request(
                    method, f"{self.base_url}{path}", json=body
                ) as response:
                    if response.status == 429:
                        retry = min(float(response.headers.get("Retry-After", 2**attempt)), 60)
                        await asyncio.sleep(retry)
                        continue
                    response.raise_for_status()
                    payload = await response.json()
                    if not isinstance(payload, dict):
                        raise TypeError("Dune API returned a non-object response")
                    return payload
        raise RuntimeError("Dune API rate limit did not recover within bounded retries")

    async def execute(self, query_id: int, parameters: dict[str, Any]) -> str:
        payload = await self._request(
            "POST",
            f"/query/{int(query_id)}/execute",
            body={"query_parameters": parameters, "performance": "medium"},
        )
        execution_id = payload.get("execution_id")
        if not execution_id:
            raise ValueError("Dune execution response omitted execution_id")
        return str(execution_id)

    async def wait(self, execution_id: str) -> dict[str, Any]:
        for _attempt in range(self.maximum_polls):
            payload = await self._request("GET", f"/execution/{execution_id}/status")
            state = str(payload.get("state") or "")
            if state in {"QUERY_STATE_COMPLETED", "QUERY_STATE_SUCCESS"}:
                return payload
            if state in {
                "QUERY_STATE_FAILED",
                "QUERY_STATE_CANCELLED",
                "QUERY_STATE_EXPIRED",
            }:
                detail = payload.get("error") or state
                raise RuntimeError(f"Dune execution failed: {detail}")
            await asyncio.sleep(self.poll_seconds)
        raise TimeoutError("Dune execution did not complete within bounded polling")

    async def results(self, execution_id: str, offset: int, limit: int) -> dict[str, Any]:
        return await self._request(
            "GET",
            f"/execution/{execution_id}/results?limit={limit}&offset={offset}"
            "&allow_partial_results=false",
        )


class DuneMonthHistoricalProvider:
    """Targeted month partition backed by one reviewed Dune saved query.

    The query must accept ``month_start`` and ``month_end`` parameters and return
    the configured entity/observed columns. Missing availability is conservatively
    set to acquisition time; it is never backdated to the event.
    """

    name = "dune_month_partition"

    def __init__(
        self,
        query_id: int,
        month: str,
        api_key: str | None,
        *,
        chain: str = "solana",
        entity_field: str = "token_address",
        observed_at_field: str = "observed_at",
        available_at_field: str | None = None,
        page_size: int = 5_000,
        client: DuneExecutionClient | None = None,
    ):
        start = datetime.strptime(month, "%Y-%m").replace(tzinfo=UTC)
        if start.month == 12:
            end = start.replace(year=start.year + 1, month=1)
        else:
            end = start.replace(month=start.month + 1)
        if not api_key and client is None:
            raise ValueError("DUNE_API_KEY is required")
        self.query_id = int(query_id)
        self.month = month
        self.start = start.isoformat()
        self.end = end.isoformat()
        self.dataset_id = f"dune-pumpfun-{month}"
        self.chain = chain
        self.entity_field = entity_field
        self.observed_at_field = observed_at_field
        self.available_at_field = available_at_field
        self.page_size = min(max(int(page_size), 1), 10_000)
        self.client = client or DuneExecutionClient(str(api_key))

    async def fetch_page(self, cursor: Any) -> BackfillPage:
        checkpoint = dict(cursor or {})
        execution_id = checkpoint.get("execution_id")
        if not execution_id:
            execution_id = await self.client.execute(
                self.query_id,
                {"month_start": self.start, "month_end": self.end},
            )
            await self.client.wait(str(execution_id))
        offset = int(checkpoint.get("offset", 0))
        response = await self.client.results(str(execution_id), offset, self.page_size)
        result = response.get("result") or {}
        rows = result.get("rows") or []
        metadata = result.get("metadata") or response.get("result_metadata") or {}
        total = int(metadata.get("total_row_count") or offset + len(rows))
        acquired_at = _utc_now()
        records = []
        for row in rows:
            observed = _coerce_utc(row[self.observed_at_field])
            available = (
                _coerce_utc(row[self.available_at_field])
                if self.available_at_field and row.get(self.available_at_field)
                else acquired_at
            )
            records.append(
                RawEvidence(
                    dataset_id=self.dataset_id,
                    provider=self.name,
                    chain=self.chain,
                    entity_type="pumpfun_launch_evidence",
                    entity_id=str(row[self.entity_field]),
                    source_timestamp=observed,
                    availability_timestamp=available,
                    endpoint_type="dune_parameterized_month_result",
                    payload=row,
                    schema_version=f"dune-query-{self.query_id}",
                    acquisition_version="v1.5-convergence-v1",
                    provenance={
                        "query_id": self.query_id,
                        "execution_id": str(execution_id),
                        "month": self.month,
                        "offset": offset,
                        "availability_from_source": bool(self.available_at_field),
                        "partial_results_allowed": False,
                        "credential_name": "DUNE_API_KEY",
                    },
                )
            )
        next_offset = offset + len(rows)
        next_cursor = (
            {"execution_id": str(execution_id), "offset": next_offset}
            if rows and next_offset < total
            else None
        )
        return BackfillPage(
            records,
            next_cursor,
            max(0, total - next_offset),
            metadata={
                "query_id": self.query_id,
                "execution_id": str(execution_id),
                "month": self.month,
                "total_rows": total,
            },
        )


def _coerce_utc(value: Any) -> str:
    text = str(value).strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()


class BirdeyeOhlcvProvider:
    """Per-token Birdeye OHLCV adapter with restart-safe token checkpoints."""

    name = "birdeye_api"

    def __init__(
        self,
        addresses: list[str],
        start: str,
        end: str,
        api_key: str | None,
        *,
        chain: str = "solana",
        candle_type: str = "1D",
        fetch_json: JsonFetcher | None = None,
        base_url: str = "https://public-api.birdeye.so",
    ):
        if not api_key and fetch_json is None:
            raise ValueError("BIRDEYE_API_KEY is required")
        self.addresses = addresses
        self.start = int(datetime.fromisoformat(start).astimezone(UTC).timestamp())
        self.end = int(datetime.fromisoformat(end).astimezone(UTC).timestamp())
        self.chain = chain
        self.candle_type = candle_type
        self.dataset_id = f"birdeye-{chain}-{candle_type.lower()}-ohlcv"
        self.fetch_json = fetch_json or PublicJsonClient(
            minimum_interval_seconds=1,
            extra_headers={"X-API-KEY": str(api_key), "x-chain": chain},
        ).fetch
        self.base_url = base_url.rstrip("/")

    async def fetch_page(self, cursor: Any) -> BackfillPage:
        index = int((cursor or {}).get("address_index", 0))
        if index >= len(self.addresses):
            return BackfillPage([], None)
        address = self.addresses[index]
        url = (
            f"{self.base_url}/defi/ohlcv?address={address}&type={self.candle_type}"
            f"&time_from={self.start}&time_to={self.end}"
        )
        response = await self.fetch_json(url)
        rows = ((response.get("data") or {}).get("items") or [])
        interval_seconds = {"1H": 3600, "4H": 14_400, "1D": 86_400}.get(
            self.candle_type, 86_400
        )
        records = []
        for row in rows:
            opened = datetime.fromtimestamp(int(row["unixTime"]), UTC)
            records.append(
                RawEvidence(
                    dataset_id=self.dataset_id,
                    provider=self.name,
                    chain=self.chain,
                    entity_type="token",
                    entity_id=address,
                    source_timestamp=opened.isoformat(),
                    availability_timestamp=(
                        opened + timedelta(seconds=interval_seconds)
                    ).isoformat(),
                    endpoint_type="historical_token_ohlcv",
                    payload={"address": address, **row},
                    schema_version="birdeye-defi-ohlcv-v1",
                    acquisition_version="v1.5-finalization-v1",
                    provenance={
                        "credential_name": "BIRDEYE_API_KEY",
                        "candle_type": self.candle_type,
                        "licensing_review_required": True,
                    },
                )
            )
        next_index = index + 1
        return BackfillPage(
            records,
            {"address_index": next_index} if next_index < len(self.addresses) else None,
            len(self.addresses) - next_index,
            metadata={"address": address, "candle_type": self.candle_type},
        )


class JsonlHistoricalProvider:
    """Adapter for legitimate provider exports using the RawEvidence field contract."""

    def __init__(self, path: str | Path, dataset_id: str, name: str, page_size: int = 500):
        self.path = Path(path)
        self.dataset_id = dataset_id
        self.name = name
        self.page_size = page_size

    async def fetch_page(self, cursor: Any) -> BackfillPage:
        offset = int((cursor or {}).get("offset", 0))
        records = []
        next_offset = offset
        with self.path.open(encoding="utf-8") as source:
            for index, line in enumerate(source):
                if index < offset:
                    continue
                if len(records) >= self.page_size:
                    break
                raw = json.loads(line)
                if raw.get("dataset_id") not in {None, self.dataset_id}:
                    raise ValueError("JSONL record dataset does not match the configured dataset")
                records.append(
                    RawEvidence(
                        dataset_id=self.dataset_id,
                        provider=self.name,
                        chain=raw["chain"],
                        entity_type=raw["entity_type"],
                        entity_id=raw["entity_id"],
                        source_timestamp=raw["source_timestamp"],
                        availability_timestamp=raw["availability_timestamp"],
                        endpoint_type=raw["endpoint_type"],
                        payload=raw["payload"],
                        schema_version=raw["schema_version"],
                        acquisition_version=raw["acquisition_version"],
                        quality_state=raw.get("quality_state", "KNOWN"),
                        provenance=raw.get("provenance") or {},
                    )
                )
                next_offset = index + 1
        has_more = len(records) == self.page_size
        return BackfillPage(
            records,
            {"offset": next_offset} if has_more else None,
            None,
        )


class OperationalSnapshotProvider:
    """Moves Jr's actually observed live snapshots into the offline raw archive."""

    name = "gambit_jr_operational_store"

    def __init__(
        self,
        database_path: str | Path,
        dataset_id: str = "gambit-jr-observed-market",
        page_size: int = 500,
    ):
        self.database_path = Path(database_path).resolve()
        self.dataset_id = dataset_id
        self.page_size = page_size

    async def fetch_page(self, cursor: Any) -> BackfillPage:
        last_id = int((cursor or {}).get("last_id", 0))
        connection = sqlite3.connect(f"file:{self.database_path.as_posix()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                "SELECT s.*,t.chain,t.token_address,t.symbol,t.name,t.first_discovered_at "
                "FROM token_snapshots s JOIN tokens t ON t.id=s.token_id WHERE s.id>? "
                "ORDER BY s.id LIMIT ?",
                (last_id, self.page_size),
            ).fetchall()
        finally:
            connection.close()
        records = [
            RawEvidence(
                dataset_id=self.dataset_id,
                provider=self.name,
                chain=row["chain"],
                entity_type="token",
                entity_id=row["token_address"],
                source_timestamp=row["captured_at"],
                availability_timestamp=row["captured_at"],
                endpoint_type="operational_market_snapshot",
                payload=dict(row),
                schema_version="operational-snapshot-v1",
                acquisition_version="v1.5-history-transfer-v1",
                provenance={
                    "source_database": self.database_path.name,
                    "originally_observed_live": True,
                },
            )
            for row in rows
        ]
        has_more = len(rows) == self.page_size
        return BackfillPage(
            records,
            {"last_id": int(rows[-1]["id"])} if rows and has_more else None,
            None,
        )


class OperationalHistoryProvider:
    """Read-only import of Jr evidence tables without copying Discord/user configuration."""

    name = "gambit_jr_operational_store"
    dataset_id = "gambit-jr-operational-evidence"
    TABLES = (
        "tokens",
        "token_snapshots",
        "launch_events",
        "candidates",
        "candidate_transitions",
        "radar_events",
        "signals",
        "milestones",
        "token_outcomes",
        "evaluation_stages_v14",
        "immutable_call_snapshots",
        "provider_evidence_v15",
        "v15_decisions",
        "v15_t0_calls",
        "tradeability_v15",
        "wallet_nodes",
        "wallet_edges",
        "wallet_clusters",
        "wallet_cluster_members",
        "buyer_cohorts",
        "creator_profiles_v14",
        "creator_launches_v14",
        "narratives_v14",
        "narrative_members_v14",
        "capital_rotation_snapshots",
        "missed_runner_audits_v14",
        "latency_observations_v14",
        "adverse_excursions_v14",
        "lifecycle_transitions_v14",
    )
    TIMESTAMPS = (
        "observed_at",
        "captured_at",
        "created_at",
        "discovered_at",
        "first_discovered_at",
        "launched_at",
        "triggered_at",
        "evaluated_at",
        "occurred_at",
        "recorded_at",
        "updated_at",
    )
    ENTITY_COLUMNS = (
        "token_address",
        "wallet_address",
        "creator_address",
        "pair_address",
        "candidate_id",
        "token_id",
        "signal_id",
        "id",
    )

    def __init__(self, database_path: str | Path, page_size: int = 500):
        self.database_path = Path(database_path).resolve()
        self.page_size = page_size

    async def fetch_page(self, cursor: Any) -> BackfillPage:
        table_index = int((cursor or {}).get("table_index", 0))
        last_rowid = int((cursor or {}).get("last_rowid", 0))
        connection = sqlite3.connect(f"file:{self.database_path.as_posix()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            existing = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            while table_index < len(self.TABLES):
                table = self.TABLES[table_index]
                if table not in existing:
                    table_index += 1
                    last_rowid = 0
                    continue
                columns = {
                    row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')
                }
                timestamp_column = next((name for name in self.TIMESTAMPS if name in columns), None)
                entity_column = next((name for name in self.ENTITY_COLUMNS if name in columns), None)
                if timestamp_column is None or entity_column is None:
                    table_index += 1
                    last_rowid = 0
                    continue
                rows = connection.execute(
                    f'SELECT rowid AS _source_rowid,* FROM "{table}" '
                    "WHERE rowid>? ORDER BY rowid LIMIT ?",
                    (last_rowid, self.page_size),
                ).fetchall()
                if not rows:
                    table_index += 1
                    last_rowid = 0
                    continue
                records = []
                for row in rows:
                    payload = dict(row)
                    timestamp = payload.get(timestamp_column)
                    if not timestamp:
                        continue
                    try:
                        parsed = datetime.fromisoformat(str(timestamp))
                    except ValueError:
                        continue
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=UTC)
                    observed_at = parsed.astimezone(UTC).isoformat()
                    entity_id = f"{table}:{payload.get(entity_column)}"
                    records.append(
                        RawEvidence(
                            dataset_id=self.dataset_id,
                            provider=self.name,
                            chain=str(payload.get("chain") or "unknown"),
                            entity_type="operational_record",
                            entity_id=entity_id,
                            source_timestamp=observed_at,
                            availability_timestamp=observed_at,
                            endpoint_type=f"operational_table:{table}",
                            payload=payload,
                            schema_version="operational-database-v1.5",
                            acquisition_version="v1.5-finalization-v1",
                            provenance={
                                "source_database": self.database_path.name,
                                "source_table": table,
                                "source_rowid": payload["_source_rowid"],
                                "timestamp_column": timestamp_column,
                                "originally_observed_live": True,
                            },
                        )
                    )
                next_rowid = int(rows[-1]["_source_rowid"])
                table_complete = len(rows) < self.page_size
                next_cursor = (
                    {"table_index": table_index + 1, "last_rowid": 0}
                    if table_complete
                    else {"table_index": table_index, "last_rowid": next_rowid}
                )
                if next_cursor["table_index"] >= len(self.TABLES):
                    next_cursor = None
                return BackfillPage(
                    records,
                    next_cursor,
                    None,
                    metadata={"table": table, "read_only": True},
                )
            return BackfillPage([], None)
        finally:
            connection.close()
