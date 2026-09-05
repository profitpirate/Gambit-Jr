#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import math
import os
import shlex
import statistics
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Sequence

from aiohttp import web
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

from memecoin_bot import e4_sub10ms_repairs_v12 as repairs


def percentile(values: Sequence[float], quantile: float) -> float | None:
    rows = sorted(float(value) for value in values if math.isfinite(float(value)))
    if not rows:
        return None
    index = min(len(rows) - 1, max(0, math.ceil(quantile * len(rows)) - 1))
    return rows[index]


def summary(values: Sequence[float]) -> dict[str, float | int | None]:
    rows = [float(value) for value in values if math.isfinite(float(value))]
    return {
        "count": len(rows),
        "median": statistics.median(rows) if rows else None,
        "p95": percentile(rows, 0.95),
        "p99": percentile(rows, 0.99),
        "max": max(rows) if rows else None,
    }


async def start_route_server() -> tuple[web.AppRunner, str, list[int]]:
    receipts: list[int] = []

    async def receive(request: web.Request) -> web.Response:
        receipts.append(time.perf_counter_ns())
        payload = await request.json()
        params = payload.get("params") or []
        signature = request.headers.get("x-e4-signature", "")
        if not signature and params:
            signature = "accepted"
        return web.json_response({"jsonrpc": "2.0", "id": payload.get("id"), "result": signature or "accepted"})

    app = web.Application()
    app.router.add_post("/", receive)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    sockets = list(site._server.sockets)  # type: ignore[union-attr]
    port = sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}/", receipts


async def main_async(args: argparse.Namespace) -> int:
    keypair = Keypair()
    command = tuple(shlex.split(os.getenv(
        "E4_BUILDER_COMMAND",
        "node tools/e4-builder/race-proxy-v3.mjs",
    )))
    process = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert process.stdin and process.stdout and process.stderr

    runner_a, route_a, receipts_a = await start_route_server()
    runner_b, route_b, receipts_b = await start_route_server()
    settings = SimpleNamespace(
        route_urls={"local_a": route_a, "local_b": route_b},
        direct_rpc_route=False,
        rpc_url="",
        route_headers={},
        route_stagger_ms=0,
        confirmation_timeout_seconds=0.25,
    )
    sender = repairs.FastPersistentRouteSender(settings, SimpleNamespace())
    await sender.warm()

    build_ms: list[float] = []
    sign_ms: list[float] = []
    dispatch_ms: list[float] = []
    end_to_end_ms: list[float] = []
    errors: list[str] = []
    total = args.warmup + args.probes
    try:
        for index in range(total):
            request_id = f"sub10-{index}"
            request = {
                "request_id": request_id,
                "side": "BUY",
                "mint": "3hCyCV1JhuF6Rup98djLbh1fyKxHyQjTcTGEQcA1pump",
                "public_key": str(keypair.pubkey()),
                "amount": 0.0555,
                "denominated_in_sol": True,
                "slippage_bps": repairs.max_output_shortfall_bps(),
                "priority_fee_sol": 0.00005,
                "tip_sol": 0.00001,
                "pool": "pump",
                "metadata": {
                    "creator": "D9gQ6RhKEpnobPBUdWY5bPQt2p3zGk3iVz6ChpUi2ArA",
                    "virtual_sol_reserves": 30_000_000_000,
                    "virtual_token_reserves": 1_073_000_000_000_000,
                    "real_token_reserves": 793_100_000_000_000,
                    "recent_blockhash": "11111111111111111111111111111111",
                },
            }
            started = time.perf_counter_ns()
            try:
                process.stdin.write(json.dumps(request, separators=(",", ":")).encode() + b"\n")
                await process.stdin.drain()
                line = await asyncio.wait_for(process.stdout.readline(), timeout=3.0)
                built = time.perf_counter_ns()
                if not line:
                    raise RuntimeError("builder closed stdout")
                response = json.loads(line)
                if response.get("error"):
                    raise RuntimeError(str(response["error"]))
                raw = base64.b64decode(response["transaction_base64"], validate=True)
                unsigned = VersionedTransaction.from_bytes(raw)
                sign_started = time.perf_counter_ns()
                signed = VersionedTransaction(unsigned.message, [keypair])
                signed_at = time.perf_counter_ns()
                signature = str(signed.signatures[0])
                encoded = base64.b64encode(bytes(signed)).decode()
                route_started = time.perf_counter_ns()
                results = await asyncio.gather(
                    sender._send(0, "local_a", route_a, encoded, signature),
                    sender._send(1, "local_b", route_b, encoded, signature),
                )
                route_done = time.perf_counter_ns()
                if not all(item.accepted for item in results):
                    raise RuntimeError("local route rejected benchmark transaction")
                if index >= args.warmup:
                    build_ms.append((built - started) / 1e6)
                    sign_ms.append((signed_at - sign_started) / 1e6)
                    dispatch_ms.append((route_done - route_started) / 1e6)
                    end_to_end_ms.append((route_done - started) / 1e6)
            except Exception as exc:
                errors.append(f"probe-{index}:{exc}")
    finally:
        await sender.close()
        await runner_a.cleanup()
        await runner_b.cleanup()
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            process.kill()
        stderr = (await process.stderr.read()).decode(errors="replace")[-4_000:]

    result = {
        "version": "e4-v12-sub10ms-hot-path-v1",
        "target": "decision-ready quote -> built -> signed -> all warm route requests acknowledged",
        "warmup": args.warmup,
        "probes": args.probes,
        "output_shortfall_bps": repairs.max_output_shortfall_bps(),
        "build_ms": summary(build_ms),
        "sign_ms": summary(sign_ms),
        "warm_local_route_ms": summary(dispatch_ms),
        "quote_to_route_ack_ms": summary(end_to_end_ms),
        "route_receipts": {"a": len(receipts_a), "b": len(receipts_b)},
        "errors": errors[-20:],
        "stderr_tail": stderr,
    }
    p95 = float((result["quote_to_route_ack_ms"] or {}).get("p95") or float("inf"))
    result["sub10ms_p95_pass"] = bool(len(end_to_end_ms) == args.probes and not errors and p95 < 10.0)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True), flush=True)
    return 0 if result["sub10ms_p95_pass"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark the repaired V12 internal hot path")
    parser.add_argument("--warmup", type=int, default=40)
    parser.add_argument("--probes", type=int, default=250)
    parser.add_argument("--output", type=Path, required=True)
    return asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
