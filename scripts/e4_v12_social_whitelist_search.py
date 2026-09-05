#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any, Mapping

import e4_v12_prelaunch_social_search as social
import e4_v12_true_latency_replay as replay


def seed_prior_history(
    snapshots: list[dict[str, Any]],
    registry: Mapping[str, Any],
) -> None:
    creators = registry.get("creators") if isinstance(registry, Mapping) else {}
    creators = creators if isinstance(creators, Mapping) else {}
    for row in snapshots:
        prior = creators.get(str(row.get("creator") or ""), {})
        if not isinstance(prior, Mapping):
            continue
        wins = social.integer(row.get("creator_wins")) + social.integer(prior.get("wins"))
        losses = social.integer(row.get("creator_losses")) + social.integer(prior.get("losses"))
        trades = wins + losses
        row["creator_wins"] = wins
        row["creator_losses"] = losses
        row["creator_rate"] = wins / trades if trades else 0.0
        row["creator_prior_registry_wins"] = social.integer(prior.get("wins"))
        row["creator_prior_registry_losses"] = social.integer(prior.get("losses"))


async def async_main(args: argparse.Namespace) -> int:
    runs = [replay.load_run(*replay.parse_pair(value)) for value in args.pair]
    if len(runs) < 8:
        raise SystemExit("at least eight chronological runs are required")
    registry = json.loads(args.prior_registry.read_text(encoding="utf-8"))
    launches = social.launch_rows(runs)
    metadata = social.scan_cache(args.metadata_cache)
    for launch in launches.values():
        if launch.get("embedded_social") and launch["mint"] not in metadata:
            metadata[launch["mint"]] = dict(launch["embedded_social"])
    metadata = await social.fill_metadata(
        launches,
        metadata,
        concurrency=args.metadata_concurrency,
        timeout_seconds=args.metadata_timeout_seconds,
    )
    snapshots = social.build_snapshots(runs, launches, metadata)
    seed_prior_history(snapshots, registry)
    latencies = [social.finite(value) for value in args.latencies_ms.split(",") if value.strip()]
    payload = social.search(runs, snapshots, latencies)
    payload["version"] = "e4-v12-social-whitelist-thesis-v1"
    payload["prior_registry_ref"] = registry.get("causal_ref")
    payload["prior_creator_count"] = registry.get("creator_count")
    payload["metadata_records"] = len(metadata)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    args.metadata_output.parent.mkdir(parents=True, exist_ok=True)
    args.metadata_output.write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(payload, indent=2, sort_keys=True), flush=True)
    return 0 if payload.get("status") == "HISTORICAL_GOLDEN_CONFIRMED" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Search prior-whitelist plus prelaunch-social entries")
    parser.add_argument("--pair", action="append", default=[])
    parser.add_argument("--prior-registry", type=Path, required=True)
    parser.add_argument("--metadata-cache", action="append", default=[], type=Path)
    parser.add_argument("--metadata-output", type=Path, required=True)
    parser.add_argument("--metadata-concurrency", type=int, default=128)
    parser.add_argument("--metadata-timeout-seconds", type=float, default=2.5)
    parser.add_argument("--latencies-ms", default="0,1,2,5,10")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    return asyncio.run(async_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
