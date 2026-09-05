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
from typing import Any, Sequence

from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

from memecoin_bot import e4_strict_output_v12 as strict


def percentile(values: Sequence[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def summary(values: Sequence[float]) -> dict[str, Any]:
    clean = [float(value) for value in values if math.isfinite(float(value))]
    return {
        "count": len(clean),
        "minimum_ms": min(clean) if clean else None,
        "median_ms": statistics.median(clean) if clean else None,
        "p95_ms": percentile(clean, 0.95),
        "p99_ms": percentile(clean, 0.99),
        "maximum_ms": max(clean) if clean else None,
        "mean_ms": statistics.fmean(clean) if clean else None,
    }


async def benchmark(iterations: int, warmup: int) -> dict[str, Any]:
    command = tuple(
        shlex.split(
            os.getenv(
                "E4_BUILDER_COMMAND",
                "node tools/e4-builder/race-proxy-v3.mjs",
            )
        )
    )
    if not command:
        raise RuntimeError("empty E4 builder command")
    process = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert process.stdin and process.stdout and process.stderr
    keypair = Keypair()
    guard_ms: list[float] = []
    build_ms: list[float] = []
    sign_ms: list[float] = []
    prebroadcast_ms: list[float] = []
    errors: list[str] = []

    try:
        total = warmup + iterations
        for index in range(total):
            request = {
                "request_id": f"v12-hotpath-{index}",
                "side": "BUY",
                "mint": "3hCyCV1JhuF6Rup98djLbh1fyKxHyQjTcTGEQcA1pump",
                "public_key": str(keypair.pubkey()),
                "amount": 0.0555,
                "denominated_in_sol": True,
                "slippage_bps": 9_000,
                "priority_fee_sol": 0.00001,
                "tip_sol": 0.000001,
                "pool": "pump",
                "metadata": {
                    "creator": "D9gQ6RhKEpnobPBUdWY5bPQt2p3zGk3iVz6ChpUi2ArA",
                    "virtual_sol_reserves": 30_000_000_000,
                    "virtual_token_reserves": 1_073_000_000_000_000,
                    "real_token_reserves": 793_100_000_000_000,
                    "recent_blockhash": "11111111111111111111111111111111",
                    "token_program": "TokenzQdY9rKXbX7mBfYvKz2zZ1zV7P3GmZrM7vQqWk",
                    "e4_preimpact": True,
                    "total_fee_bps": 125,
                },
            }
            started = time.perf_counter_ns()
            guard_started = started
            guarded = strict.guarded_request(request)
            guard_completed = time.perf_counter_ns()
            process.stdin.write(
                json.dumps(guarded, separators=(",", ":")).encode("utf-8") + b"\n"
            )
            await process.stdin.drain()
            line = await asyncio.wait_for(process.stdout.readline(), timeout=5.0)
            build_completed = time.perf_counter_ns()
            if not line:
                raise RuntimeError("builder closed stdout")
            response = json.loads(line)
            if response.get("error"):
                raise RuntimeError(str(response["error"]))
            raw = base64.b64decode(response["transaction_base64"], validate=True)
            transaction = VersionedTransaction.from_bytes(raw)
            sign_started = time.perf_counter_ns()
            signed = VersionedTransaction(transaction.message, [keypair])
            sign_completed = time.perf_counter_ns()
            if not str(signed.signatures[0]):
                raise RuntimeError("empty signature")
            if index >= warmup:
                guard_ms.append((guard_completed - guard_started) / 1_000_000.0)
                build_ms.append((build_completed - guard_completed) / 1_000_000.0)
                sign_ms.append((sign_completed - sign_started) / 1_000_000.0)
                prebroadcast_ms.append((sign_completed - started) / 1_000_000.0)
    except Exception as exc:
        errors.append(str(exc))
    finally:
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            process.kill()
        stderr = (await process.stderr.read()).decode(errors="replace")[-4_000:]

    return {
        "version": "e4-v12-hotpath-benchmark-v1",
        "iterations": iterations,
        "warmup": warmup,
        "guard": summary(guard_ms),
        "build": summary(build_ms),
        "sign": summary(sign_ms),
        "prebroadcast": summary(prebroadcast_ms),
        "errors": errors,
        "stderr_tail": stderr,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark V12 decision guard through signed transaction")
    parser.add_argument("--iterations", type=int, default=500)
    parser.add_argument("--warmup", type=int, default=50)
    parser.add_argument("--maximum-p95-ms", type=float, default=10.0)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = asyncio.run(benchmark(max(1, args.iterations), max(0, args.warmup)))
    p95 = result["prebroadcast"].get("p95_ms")
    result["maximum_p95_ms"] = args.maximum_p95_ms
    result["passed"] = bool(
        not result["errors"]
        and p95 is not None
        and float(p95) < args.maximum_p95_ms
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
