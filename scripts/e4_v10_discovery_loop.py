#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import time
from pathlib import Path

import e4_v10_discovery_worker as worker


def notify_reload(host: str, port: int) -> None:
    payload = json.dumps({"kind": "model_reload", "observed_ns": time.time_ns()}).encode("utf-8")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.sendto(payload, (host, port))
    finally:
        sock.close()


async def main_async(args: argparse.Namespace) -> int:
    worker_args = worker.parser().parse_args([])
    worker_args.queue = args.queue
    worker_args.output = args.output
    worker_args.rpc_urls = args.rpc_urls
    worker_args.timeout = args.timeout
    worker_args.rpc_concurrency = args.rpc_concurrency
    worker_args.creator_concurrency = args.creator_concurrency
    worker_args.launch_concurrency = args.launch_concurrency
    worker_args.tx_concurrency = args.tx_concurrency
    worker_args.creator_signatures = args.creator_signatures
    worker_args.mint_signatures = args.mint_signatures
    worker_args.max_launches = args.max_launches
    worker_args.creator = ""

    last_queue_size = -1
    while True:
        size = args.queue.stat().st_size if args.queue.exists() else 0
        if size != last_queue_size:
            try:
                report = await worker.run(worker_args)
                worker._atomic_json(args.output, report)
                notify_reload(args.udp_host, args.udp_port)
                print(
                    json.dumps(
                        {
                            "updated": str(args.output),
                            "queued": report["queued_creators"],
                            "approved": report["approved_creators"],
                            "negative": report["negative_creators"],
                        }
                    ),
                    flush=True,
                )
                last_queue_size = size
            except Exception as exc:
                print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}), flush=True)
        await asyncio.sleep(args.interval)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Continuously enrich E4's unknown creators")
    value.add_argument("--interval", type=float, default=60.0)
    value.add_argument("--queue", type=Path, default=Path(os.getenv("E4_DISCOVERY_QUEUE_PATH", "runtime/e4-discovery-queue.jsonl")))
    value.add_argument("--output", type=Path, default=Path(os.getenv("E4_DISCOVERED_CREATORS_PATH", "models/e4/e4-discovered-known-creators.json")))
    value.add_argument("--rpc-urls", default=os.getenv("E4_DISCOVERY_RPC_URLS", ",".join(worker.DEFAULT_RPCS)))
    value.add_argument("--timeout", type=float, default=6.0)
    value.add_argument("--rpc-concurrency", type=int, default=16)
    value.add_argument("--creator-concurrency", type=int, default=2)
    value.add_argument("--launch-concurrency", type=int, default=4)
    value.add_argument("--tx-concurrency", type=int, default=20)
    value.add_argument("--creator-signatures", type=int, default=500)
    value.add_argument("--mint-signatures", type=int, default=250)
    value.add_argument("--max-launches", type=int, default=20)
    value.add_argument("--udp-host", default=os.getenv("E4_PIPELINE_UDP_HOST", "127.0.0.1"))
    value.add_argument("--udp-port", type=int, default=int(os.getenv("E4_PIPELINE_UDP_PORT", "19104")))
    return value


def main() -> int:
    return asyncio.run(main_async(parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
