#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import sys
import time
from collections import OrderedDict, defaultdict
from pathlib import Path
from typing import Any, Mapping, Sequence

import aiohttp

# The holdout must exercise the same policy stack as the funded runtime—not an
# older stress-only policy. Importing V7 patches the shared core before the base
# harness is loaded.
from memecoin_bot import e4_hardening_v7  # noqa: F401


def load_base():
    path = Path(__file__).with_name("e4_live_market_stress.py")
    name = "e4_300_launch_base"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(dict(payload), indent=2, default=str), encoding="utf-8")
    temporary.replace(path)


async def endpoint_worker(
    url: str,
    base: Any,
    queue: asyncio.Queue[tuple[str, Mapping[str, Any]]],
    stop: asyncio.Event,
    counters: dict[str, dict[str, int]],
    errors: list[str],
) -> None:
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=8, sock_read=20)
    endpoint = counters.setdefault(url, {"connections": 0, "messages": 0, "disconnects": 0})
    async with aiohttp.ClientSession(timeout=timeout) as session:
        while not stop.is_set():
            try:
                async with session.ws_connect(
                    url,
                    heartbeat=10,
                    max_msg_size=8 * 1024 * 1024,
                ) as ws:
                    endpoint["connections"] += 1
                    await ws.send_json(
                        {
                            "jsonrpc": "2.0",
                            "id": endpoint["connections"],
                            "method": "logsSubscribe",
                            "params": [
                                {"mentions": [base.PUMP_PROGRAM_ID]},
                                {"commitment": "processed"},
                            ],
                        }
                    )
                    while not stop.is_set():
                        try:
                            message = await asyncio.wait_for(ws.receive(), timeout=10.0)
                        except asyncio.TimeoutError:
                            continue
                        if message.type == aiohttp.WSMsgType.TEXT:
                            endpoint["messages"] += 1
                            try:
                                payload = json.loads(message.data)
                            except json.JSONDecodeError:
                                continue
                            await queue.put((url, payload))
                        elif message.type in {
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.ERROR,
                            aiohttp.WSMsgType.CLOSE,
                        }:
                            endpoint["disconnects"] += 1
                            break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                endpoint["disconnects"] += 1
                errors.append(f"{url}: {type(exc).__name__}: {exc}")
                if len(errors) > 200:
                    del errors[:-200]
                try:
                    await asyncio.wait_for(stop.wait(), timeout=0.35)
                except asyncio.TimeoutError:
                    pass


async def capture_exact_launch_cohort(
    base: Any,
    seconds: float,
    ws_urls: Sequence[str],
    *,
    target_launches: int,
    tail_seconds: float,
    heartbeat_seconds: float,
    progress_path: Path,
) -> tuple[list[Any], dict[str, Any]]:
    if target_launches <= 0:
        raise ValueError("target_launches must be positive")
    if not ws_urls:
        raise ValueError("at least one WebSocket endpoint is required")

    started_monotonic = time.monotonic()
    hard_deadline = started_monotonic + seconds
    target_reached_at: float | None = None
    tail_deadline: float | None = None
    last_heartbeat = 0.0
    last_heartbeat_count = -1

    queue: asyncio.Queue[tuple[str, Mapping[str, Any]]] = asyncio.Queue(maxsize=20_000)
    stop = asyncio.Event()
    endpoint_counters: dict[str, dict[str, int]] = {}
    errors: list[str] = []
    dedupe: set[tuple[str, int, str]] = set()
    cohort: OrderedDict[str, dict[str, Any]] = OrderedDict()
    events: list[Any] = []
    events_per_mint: dict[str, int] = defaultdict(int)

    workers = [
        asyncio.create_task(
            endpoint_worker(url, base, queue, stop, endpoint_counters, errors),
            name=f"e4-300-ws-{index}",
        )
        for index, url in enumerate(dict.fromkeys(ws_urls))
    ]

    def progress(now: float, final: bool = False) -> dict[str, Any]:
        elapsed = now - started_monotonic
        remaining = None
        if tail_deadline is not None:
            remaining = max(0.0, tail_deadline - now)
        payload = {
            "status": (
                "complete"
                if final and len(cohort) >= target_launches
                else "failed"
                if final
                else "tail"
                if target_reached_at is not None
                else "collecting"
            ),
            "target_launches": target_launches,
            "captured_launches": len(cohort),
            "captured_events": len(events),
            "elapsed_seconds": round(elapsed, 3),
            "tail_remaining_seconds": None if remaining is None else round(remaining, 3),
            "queue_depth": queue.qsize(),
            "endpoints": endpoint_counters,
            "recent_errors": errors[-20:],
            "last_mint": next(reversed(cohort), None) if cohort else None,
            "timestamp_ns": time.time_ns(),
        }
        atomic_json(progress_path, payload)
        print("E4_300_PROGRESS " + json.dumps(payload, separators=(",", ":")), flush=True)
        return payload

    try:
        while True:
            now = time.monotonic()
            if now >= hard_deadline:
                break
            if tail_deadline is not None and now >= tail_deadline:
                break

            timeout = min(
                1.0,
                max(0.01, hard_deadline - now),
                max(0.01, tail_deadline - now) if tail_deadline is not None else 1.0,
            )
            try:
                _endpoint, payload = await asyncio.wait_for(queue.get(), timeout=timeout)
            except asyncio.TimeoutError:
                payload = None

            if payload is not None:
                incoming: list[Any] = []
                base.decode_log_payload(payload, incoming, dedupe)

                # First register CREATE events so all economic events from the
                # same transaction are retained even when the 300th mint lands.
                for item in incoming:
                    if item.kind != base.core.EventKind.CREATE.value:
                        continue
                    if item.mint in cohort or len(cohort) >= target_launches:
                        continue
                    cohort[item.mint] = {
                        "ordinal": len(cohort) + 1,
                        "received_ns": item.received_ns,
                        "slot": item.slot,
                        "signature": item.signature,
                    }
                    if len(cohort) >= target_launches and target_reached_at is None:
                        target_reached_at = time.monotonic()
                        tail_deadline = min(hard_deadline, target_reached_at + tail_seconds)

                for item in incoming:
                    if item.mint not in cohort:
                        continue
                    item.event_id = len(events) + 1
                    events.append(item)
                    events_per_mint[item.mint] += 1

            now = time.monotonic()
            should_heartbeat = (
                now - last_heartbeat >= heartbeat_seconds
                or len(cohort) >= last_heartbeat_count + 25
                or (target_reached_at is not None and last_heartbeat_count < target_launches)
            )
            if should_heartbeat:
                progress(now)
                last_heartbeat = now
                last_heartbeat_count = len(cohort)
    finally:
        stop.set()
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)

    events.sort(key=lambda item: (item.received_ns, item.slot, item.event_index))
    for index, event in enumerate(events, start=1):
        event.event_id = index

    finished = time.monotonic()
    final_progress = progress(finished, final=True)
    diagnostics = {
        "mode": "exact-live-launch-cohort",
        "target_launches": target_launches,
        "unique_launches": len(cohort),
        "target_reached": len(cohort) >= target_launches,
        "duration_seconds": finished - started_monotonic,
        "tail_seconds_requested": tail_seconds,
        "tail_seconds_observed": (
            max(0.0, finished - target_reached_at) if target_reached_at is not None else 0.0
        ),
        "decoded_events": len(events),
        "events_per_mint_min": min(events_per_mint.values(), default=0),
        "events_per_mint_max": max(events_per_mint.values(), default=0),
        "endpoints": endpoint_counters,
        "errors": errors[-50:],
        "progress_path": str(progress_path),
        "final_progress": final_progress,
        "cohort": [
            {"mint": mint, **metadata, "events": events_per_mint.get(mint, 0)}
            for mint, metadata in cohort.items()
        ],
    }
    if len(cohort) < target_launches:
        raise RuntimeError(
            f"live holdout ended with {len(cohort)}/{target_launches} launches; "
            f"see {progress_path}"
        )
    return events, diagnostics


async def main() -> int:
    base = load_base()
    parser: argparse.ArgumentParser = base.parser()
    parser.description = "E4 V7 hypothesis-only holdout over exactly 300 live Pump launches"
    parser.add_argument("--target-launches", type=int, default=300)
    parser.add_argument("--tail-seconds", type=float, default=240.0)
    parser.add_argument("--heartbeat-seconds", type=float, default=30.0)
    args = parser.parse_args()

    output = Path(args.output)
    progress_path = Path(
        os.getenv(
            "E4_HOLDOUT_PROGRESS_PATH",
            str(output.with_name(output.stem + "-progress.json")),
        )
    )

    async def capture(seconds: float, ws_urls: Sequence[str]):
        return await capture_exact_launch_cohort(
            base,
            seconds,
            ws_urls,
            target_launches=args.target_launches,
            tail_seconds=args.tail_seconds,
            heartbeat_seconds=args.heartbeat_seconds,
            progress_path=progress_path,
        )

    base.capture_native_pump = capture
    args.minimum_launches = args.target_launches
    return await base.main_async(args)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
