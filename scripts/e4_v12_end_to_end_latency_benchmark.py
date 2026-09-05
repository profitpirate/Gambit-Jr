#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

from aiohttp import web

from memecoin_bot import e4_transport_v12 as transport


def finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    rows = sorted(values)
    index = min(len(rows) - 1, max(0, round((len(rows) - 1) * fraction)))
    return rows[index]


def p95_candidates(value: Any, path: str = "") -> list[tuple[str, float]]:
    output: list[tuple[str, float]] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            child = f"{path}.{key}" if path else str(key)
            lower = str(key).lower()
            if "p95" in lower and ("ms" in lower or lower.endswith("p95")):
                number = finite(item, float("nan"))
                if math.isfinite(number):
                    output.append((child, number))
            output.extend(p95_candidates(item, child))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            output.extend(p95_candidates(item, f"{path}[{index}]"))
    return output


def run_guard_build_sign(iterations: int, warmup: int) -> tuple[dict[str, Any], float, list[tuple[str, float]]]:
    with tempfile.TemporaryDirectory(prefix="e4-v12-latency-") as directory:
        output = Path(directory) / "prebroadcast.json"
        command = [
            sys.executable,
            "-m",
            "scripts.e4_v12_hotpath_benchmark_v3",
            "--iterations",
            str(iterations),
            "--warmup",
            str(warmup),
            "--maximum-p95-ms",
            "1000",
            "--output",
            str(output),
        ]
        subprocess.run(command, check=True)
        payload = json.loads(output.read_text(encoding="utf-8"))
    candidates = p95_candidates(payload)
    # Prefer an explicitly named full/end-to-end metric. Otherwise use the
    # largest reported pre-broadcast p95 as a conservative bound.
    preferred = [
        value
        for name, value in candidates
        if any(term in name.lower() for term in ("full", "total", "end_to_end", "end-to-end", "prebroadcast", "pre_broadcast"))
    ]
    prebroadcast_p95 = max(preferred or [value for _, value in candidates] or [0.0])
    return payload, prebroadcast_p95, candidates


async def transport_benchmark(iterations: int, warmup: int) -> dict[str, Any]:
    receipts: list[tuple[str, int]] = []

    async def handler(request: web.Request) -> web.Response:
        if request.method == "POST":
            receipt_ns = time.perf_counter_ns()
            payload = await request.json()
            route = request.path
            receipts.append((route, receipt_ns))
            return web.json_response({"jsonrpc": "2.0", "id": payload.get("id", 1), "result": "S" * 88})
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    settings = SimpleNamespace(
        route_urls={"route-a": origin + "/a", "route-b": origin + "/b"},
        direct_rpc_route=False,
        rpc_url=origin + "/rpc",
        route_headers={},
        route_stagger_ms=25,
        confirmation_timeout_seconds=1.0,
    )
    sender = transport.WarmFanoutRouteSender(settings, SimpleNamespace())
    await sender.warm()
    routes = list(sender.routes)
    latencies: list[float] = []
    spreads: list[float] = []
    try:
        for index in range(warmup + iterations):
            before_count = len(receipts)
            started_ns = time.perf_counter_ns()
            results = await asyncio.gather(
                *(
                    sender._send(route_index, name, url, "signed-base64", "S" * 88)
                    for route_index, (name, url) in enumerate(routes)
                )
            )
            new_receipts = receipts[before_count:]
            if not all(result.success for result in results) or len(new_receipts) < len(routes):
                raise RuntimeError("warmed fanout did not reach every loopback route")
            receipt_times = [timestamp for _, timestamp in new_receipts]
            first_ms = max(0.0, (min(receipt_times) - started_ns) / 1_000_000.0)
            spread_ms = max(0.0, (max(receipt_times) - min(receipt_times)) / 1_000_000.0)
            if index >= warmup:
                latencies.append(first_ms)
                spreads.append(spread_ms)
    finally:
        await sender.close()
        await runner.cleanup()
    return {
        "iterations": iterations,
        "routes": len(routes),
        "first_socket_receipt_p50_ms": statistics.median(latencies) if latencies else None,
        "first_socket_receipt_p95_ms": percentile(latencies, 0.95),
        "first_socket_receipt_max_ms": max(latencies, default=0.0),
        "route_arrival_spread_p95_ms": percentile(spreads, 0.95),
        "route_arrival_spread_max_ms": max(spreads, default=0.0),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark V12 guard/build/sign plus warmed socket dispatch")
    parser.add_argument("--iterations", type=int, default=750)
    parser.add_argument("--warmup", type=int, default=75)
    parser.add_argument("--maximum-p95-ms", type=float, default=10.0)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    prebroadcast, prebroadcast_p95, candidates = run_guard_build_sign(args.iterations, args.warmup)
    dispatch = asyncio.run(transport_benchmark(args.iterations, args.warmup))
    dispatch_p95 = finite(dispatch.get("first_socket_receipt_p95_ms"))
    combined_upper_bound = prebroadcast_p95 + dispatch_p95
    result = {
        "version": "e4-v12-end-to-end-latency-benchmark-v1",
        "scope": "guarded request -> local build/sign -> warmed loopback socket receipt",
        "does_not_claim": "mainnet landing latency",
        "prebroadcast": prebroadcast,
        "prebroadcast_p95_candidates": [{"path": name, "value_ms": value} for name, value in candidates],
        "prebroadcast_selected_p95_ms": prebroadcast_p95,
        "dispatch": dispatch,
        "combined_p95_upper_bound_ms": combined_upper_bound,
        "maximum_p95_ms": args.maximum_p95_ms,
        "passed": combined_upper_bound < args.maximum_p95_ms,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({key: value for key, value in result.items() if key not in {"prebroadcast", "prebroadcast_p95_candidates"}}, indent=2, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
