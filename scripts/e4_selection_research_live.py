#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import statistics
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

import aiohttp


def load_base():
    path = Path(__file__).with_name("e4_live_market_stress.py")
    name = "e4_live_market_stress_selection_base"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    import sys
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


base = load_base()


def host(value: Any) -> str:
    if not value:
        return "unknown"
    try:
        return (urlparse(str(value)).hostname or "unknown").lower()
    except ValueError:
        return "unknown"


def median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


class Api:
    def __init__(self, concurrency: int = 16):
        self.semaphore = asyncio.Semaphore(concurrency)
        self.timeout = aiohttp.ClientTimeout(total=8)
        self.session: aiohttp.ClientSession | None = None
        self.errors: list[str] = []

    async def __aenter__(self) -> "Api":
        self.session = aiohttp.ClientSession(
            timeout=self.timeout,
            headers={"accept": "application/json", "user-agent": "Gambit-E4-Research/4"},
        )
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self.session:
            await self.session.close()

    async def coin(self, mint: str) -> dict[str, Any]:
        assert self.session
        url = f"https://frontend-api-v3.pump.fun/coins-v2/{mint}"
        async with self.semaphore:
            for attempt in range(3):
                try:
                    async with self.session.get(url) as response:
                        text = await response.text()
                        if response.status == 404:
                            return {}
                        if response.status == 429 or response.status >= 500:
                            raise RuntimeError(f"HTTP {response.status}")
                        if response.status >= 400:
                            raise RuntimeError(f"HTTP {response.status}: {text[:120]}")
                        value = json.loads(text)
                        if isinstance(value, Mapping) and isinstance(value.get("data"), Mapping):
                            value = value["data"]
                        return dict(value) if isinstance(value, Mapping) else {}
                except Exception as exc:
                    if attempt == 2:
                        self.errors.append(f"{mint}: {exc}")
                    await asyncio.sleep(0.2 * (2**attempt))
        return {}


def create_features(events: list[Any]) -> dict[str, Any]:
    create = next((event for event in events if event.kind == base.core.EventKind.CREATE.value), None)
    if create is None:
        return {}
    raw = dict(create.raw)
    buys = [event for event in events if event.kind == base.core.EventKind.BUY.value]
    creator_buys = [
        event for event in buys if event.trader and event.trader == create.creator
    ]
    first_250 = [
        event for event in buys
        if 0 <= (event.received_ns - create.received_ns) / 1_000_000 <= 250
    ]
    first_1000 = [
        event for event in buys
        if 0 <= (event.received_ns - create.received_ns) / 1_000_000 <= 1_000
    ]
    return {
        "mint": create.mint,
        "create_received_ns": create.received_ns,
        "creator": create.creator,
        "name": raw.get("name"),
        "symbol": raw.get("symbol"),
        "metadata_uri": raw.get("uri"),
        "metadata_host": host(raw.get("uri")),
        "cashback": bool(raw.get("is_cashback_enabled")),
        "mayhem": bool(raw.get("is_mayhem_mode")),
        "creator_buy_sol": sum(event.sol_amount for event in creator_buys),
        "creator_buy_count": len(creator_buys),
        "buyers_250ms": len({event.trader for event in first_250 if event.trader}),
        "buy_sol_250ms": sum(event.sol_amount for event in first_250),
        "buyers_1s": len({event.trader for event in first_1000 if event.trader}),
        "buy_sol_1s": sum(event.sol_amount for event in first_1000),
        "sells_1s": sum(
            event.kind == base.core.EventKind.SELL.value
            and 0 <= (event.received_ns - create.received_ns) / 1_000_000 <= 1_000
            for event in events
        ),
    }


def compare(selected: list[dict[str, Any]], controls: list[dict[str, Any]]) -> dict[str, Any]:
    numeric = (
        "creator_buy_sol",
        "creator_buy_count",
        "buyers_250ms",
        "buy_sol_250ms",
        "buyers_1s",
        "buy_sol_1s",
        "sells_1s",
    )
    result: dict[str, Any] = {}
    for key in numeric:
        left = [float(row.get(key) or 0) for row in selected]
        right = [float(row.get(key) or 0) for row in controls]
        result[key] = {
            "selected_median": median(left),
            "controls_median": median(right),
            "selected_mean": statistics.fmean(left) if left else None,
            "controls_mean": statistics.fmean(right) if right else None,
        }
    result["metadata_hosts_selected"] = Counter(
        row.get("metadata_host") or "unknown" for row in selected
    ).most_common()
    result["metadata_hosts_controls"] = Counter(
        row.get("metadata_host") or "unknown" for row in controls
    ).most_common(20)
    result["cashback_selected_fraction"] = (
        sum(bool(row.get("cashback")) for row in selected) / len(selected)
        if selected else None
    )
    result["cashback_controls_fraction"] = (
        sum(bool(row.get("cashback")) for row in controls) / len(controls)
        if controls else None
    )
    return result


async def run(args: argparse.Namespace) -> dict[str, Any]:
    ws_urls = base.DEFAULT_WS_RPCS
    rpc_urls = base.DEFAULT_HTTP_RPCS
    started = int(time.time())
    events, diagnostics = await base.capture_native_pump(args.capture_seconds, ws_urls)
    ended = int(time.time())
    launches = [event for event in events if event.kind == base.core.EventKind.CREATE.value]
    if len(launches) < args.minimum_launches:
        async with base.RpcPool(rpc_urls, timeout=10) as rpc:
            backfill = await base.backfill_pump_events(rpc, started, ended, 1000)
        keys = {(event.signature, event.event_index, event.kind) for event in events}
        for event in backfill:
            key = (event.signature, event.event_index, event.kind)
            if key not in keys:
                events.append(event)
                keys.add(key)
        events.sort(key=lambda event: (event.received_ns, event.slot, event.event_index))

    grouped: dict[str, list[Any]] = defaultdict(list)
    for event in events:
        grouped[event.mint].append(event)
    for values in grouped.values():
        values.sort(key=lambda event: (event.received_ns, event.event_index))

    async with base.RpcPool(rpc_urls, timeout=10) as rpc:
        fresh = await base.fetch_e4_wallet_sample(rpc, args.wallet_signatures)
        rpc_errors = list(rpc.errors)

    selected_live: list[dict[str, Any]] = []
    for mint, values in grouped.items():
        oracle_buy = next(
            (
                event for event in values
                if event.kind == base.core.EventKind.BUY.value
                and event.trader == base.E4_WALLET
            ),
            None,
        )
        if oracle_buy is None:
            continue
        features = create_features(values)
        create_ns = int(features.get("create_received_ns") or oracle_buy.received_ns)
        prior = [
            event for event in values
            if event.kind == base.core.EventKind.BUY.value
            and event.received_ns < oracle_buy.received_ns
        ]
        features.update(
            {
                "e4_buy_received_ns": oracle_buy.received_ns,
                "e4_create_to_buy_ms": (oracle_buy.received_ns - create_ns) / 1_000_000,
                "e4_buy_sol": oracle_buy.sol_amount,
                "e4_entry_fdv_usd": oracle_buy.fdv_usd,
                "e4_buy_rank": len(prior) + 1,
                "prior_buyers": len({event.trader for event in prior if event.trader}),
                "prior_buy_sol": sum(event.sol_amount for event in prior),
            }
        )
        selected_live.append(features)

    active_centers = [row["create_received_ns"] for row in selected_live]
    active_radius_ns = int(args.active_radius_seconds * 1_000_000_000)
    controls = []
    selected_mints = {row["mint"] for row in selected_live}
    for mint, values in grouped.items():
        if mint in selected_mints:
            continue
        features = create_features(values)
        created = int(features.get("create_received_ns") or 0)
        if active_centers and any(abs(created - center) <= active_radius_ns for center in active_centers):
            controls.append(features)

    recent_mints = [row["mint"] for row in fresh.get("positions") or []]
    async with Api(args.api_concurrency) as api:
        coin_rows = await asyncio.gather(*(api.coin(mint) for mint in recent_mints))
        api_errors = list(api.errors)
    recent_enriched = []
    for position, coin in zip(fresh.get("positions") or [], coin_rows):
        recent_enriched.append(
            {
                **position,
                "name": coin.get("name"),
                "symbol": coin.get("symbol"),
                "creator": coin.get("creator"),
                "metadata_uri": coin.get("metadata_uri"),
                "metadata_host": host(coin.get("metadata_uri")),
                "twitter": coin.get("twitter"),
                "website": coin.get("website"),
                "telegram": coin.get("telegram"),
                "cashback": coin.get("is_cashback_coin"),
                "mayhem": coin.get("is_mayhem_mode"),
            }
        )
    creators = Counter(
        str(row.get("creator"))
        for row in recent_enriched
        if row.get("creator")
    )

    return {
        "generated_at": int(time.time()),
        "hypothesis_only": True,
        "mainnet_transactions_sent": 0,
        "capture": {
            **diagnostics,
            "events": len(events),
            "launches": sum(event.kind == base.core.EventKind.CREATE.value for event in events),
            "trade_events": sum(
                event.kind in {base.core.EventKind.BUY.value, base.core.EventKind.SELL.value}
                for event in events
            ),
        },
        "same_window": {
            "e4_selected": selected_live,
            "active_window_controls": controls,
            "comparison": compare(selected_live, controls),
        },
        "recent_e4_sample": {
            **{key: value for key, value in fresh.items() if key != "positions"},
            "unique_creators": len(creators),
            "repeated_creators": [
                {"creator": creator, "selected_tokens": count}
                for creator, count in creators.most_common()
                if count > 1
            ],
            "metadata_hosts": Counter(
                row.get("metadata_host") or "unknown" for row in recent_enriched
            ).most_common(),
            "social_presence_fraction": (
                sum(any(row.get(key) for key in ("twitter", "website", "telegram")) for row in recent_enriched)
                / len(recent_enriched)
                if recent_enriched else None
            ),
            "positions": recent_enriched,
        },
        "errors": {
            "rpc": rpc_errors[-50:],
            "api": api_errors[-50:],
        },
    }


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser()
    value.add_argument("--capture-seconds", type=float, default=900)
    value.add_argument("--minimum-launches", type=int, default=100)
    value.add_argument("--wallet-signatures", type=int, default=350)
    value.add_argument("--active-radius-seconds", type=float, default=180)
    value.add_argument("--api-concurrency", type=int, default=16)
    value.add_argument("--output", default="outputs/e4-selection-live-research.json")
    return value


def main() -> None:
    args = parser().parse_args()
    report = asyncio.run(run(args))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, default=str))
    print(json.dumps({k: v for k, v in report.items() if k != "same_window"}, indent=2, default=str))


if __name__ == "__main__":
    main()
