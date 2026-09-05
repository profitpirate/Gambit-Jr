#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
import statistics
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlparse

import aiohttp

import e4_v12_golden_thesis_search_v2 as golden
import e4_v12_true_latency_replay as replay

X_EPOCH_MS = 1_288_834_974_657
STATUS_RE = re.compile(r"(?:x|twitter)\.com/([^/?#]+)/status(?:es)?/(\d+)", re.I)
HANDLE_RE = re.compile(r"(?:x|twitter)\.com/([^/?#]+)", re.I)


def finite(value: Any, default: float = 0.0) -> float:
    return replay.finite(value, default)


def integer(value: Any, default: int = 0) -> int:
    return replay.integer(value, default)


def wilson_lower(wins: int, total: int, z: float = 1.96) -> float:
    if total <= 0:
        return 0.0
    p = wins / total
    denominator = 1.0 + z * z / total
    centre = p + z * z / (2.0 * total)
    spread = z * math.sqrt((p * (1.0 - p) + z * z / (4.0 * total)) / total)
    return max(0.0, (centre - spread) / denominator)


def metadata_urls(uri: str) -> list[str]:
    text = str(uri or "").strip()
    if not text:
        return []
    if text.startswith("ipfs://"):
        key = text[7:].lstrip("/")
        return [
            f"https://ipfs.io/ipfs/{key}",
            f"https://gateway.pinata.cloud/ipfs/{key}",
            f"https://cloudflare-ipfs.com/ipfs/{key}",
        ]
    if text.startswith("ar://"):
        return [f"https://arweave.net/{text[5:].lstrip('/')}"]
    output = [text]
    if "/ipfs/" in text:
        key = text.split("/ipfs/", 1)[1]
        output.extend([
            f"https://ipfs.io/ipfs/{key}",
            f"https://gateway.pinata.cloud/ipfs/{key}",
        ])
    return list(dict.fromkeys(output))


def walk_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).lower() in {
                "twitter", "x", "social", "socials", "website", "description",
                "telegram", "created_by", "external_url", "extensions",
            }:
                yield from walk_strings(item)
            elif isinstance(item, (Mapping, list, tuple)):
                yield from walk_strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from walk_strings(item)


def social_fields(metadata: Mapping[str, Any] | None, create_ns: int) -> dict[str, Any]:
    values = list(walk_strings(metadata or {}))
    handle = ""
    status_id = ""
    twitter = ""
    for value in values:
        match = STATUS_RE.search(value)
        if match:
            handle = match.group(1).lower().lstrip("@")
            status_id = match.group(2)
            twitter = value
            break
    if not handle:
        for value in values:
            match = HANDLE_RE.search(value)
            if match:
                handle = match.group(1).lower().lstrip("@")
                twitter = value
                break
    tweet_age_seconds = None
    tweet_time_ns = None
    if status_id:
        tweet_ms = (int(status_id) >> 22) + X_EPOCH_MS
        tweet_time_ns = tweet_ms * 1_000_000
        tweet_age_seconds = (create_ns - tweet_time_ns) / 1e9
    website = any(
        value.lower().startswith(("http://", "https://"))
        and "twitter.com" not in value.lower()
        and "x.com" not in value.lower()
        for value in values
    )
    return {
        "metadata_ok": bool(metadata),
        "twitter": twitter,
        "twitter_handle": handle,
        "twitter_status_id": status_id,
        "tweet_time_ns": tweet_time_ns,
        "tweet_age_seconds": tweet_age_seconds,
        "website_present": website,
    }


def scan_cache(paths: Sequence[Path]) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}

    def inspect(value: Any) -> None:
        if isinstance(value, Mapping):
            mint = str(value.get("mint") or "")
            if mint and any(
                key in value
                for key in (
                    "twitter_handle", "twitter_status_id", "tweet_age_seconds",
                    "metadata_ok", "website_present",
                )
            ):
                output[mint] = {
                    "metadata_ok": bool(value.get("metadata_ok")),
                    "twitter": str(value.get("twitter") or ""),
                    "twitter_handle": str(value.get("twitter_handle") or "").lower().lstrip("@"),
                    "twitter_status_id": str(value.get("twitter_status_id") or ""),
                    "tweet_time_ns": value.get("tweet_time_ns"),
                    "tweet_age_seconds": value.get("tweet_age_seconds"),
                    "website_present": bool(value.get("website_present")),
                }
            for item in value.values():
                if isinstance(item, (Mapping, list)):
                    inspect(item)
        elif isinstance(value, list):
            for item in value:
                inspect(item)

    for root in paths:
        files = [root] if root.is_file() else list(root.rglob("*.json")) if root.exists() else []
        for path in files:
            try:
                inspect(json.loads(path.read_text(encoding="utf-8")))
            except Exception:
                continue
    return output


class Fetcher:
    def __init__(self, concurrency: int, timeout_seconds: float) -> None:
        self.sem = asyncio.Semaphore(max(1, concurrency))
        self.timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self.session: aiohttp.ClientSession | None = None

    async def __aenter__(self):
        self.session = aiohttp.ClientSession(
            timeout=self.timeout,
            connector=aiohttp.TCPConnector(
                limit=max(16, self.sem._value * 2),
                ttl_dns_cache=600,
                keepalive_timeout=45,
                enable_cleanup_closed=True,
            ),
        )
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self.session is not None:
            await self.session.close()

    async def one(self, uri: str) -> Mapping[str, Any] | None:
        assert self.session is not None
        async with self.sem:
            for url in metadata_urls(uri):
                try:
                    async with self.session.get(
                        url,
                        headers={"accept": "application/json", "user-agent": "gambit-v12-research"},
                    ) as response:
                        if response.status >= 400:
                            continue
                        payload = await response.json(content_type=None)
                        if isinstance(payload, Mapping):
                            return payload
                except Exception:
                    continue
        return None


async def fill_metadata(
    launches: Mapping[tuple[str, str], dict[str, Any]],
    cache: dict[str, dict[str, Any]],
    *,
    concurrency: int,
    timeout_seconds: float,
) -> dict[str, dict[str, Any]]:
    by_uri: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for launch in launches.values():
        mint = launch["mint"]
        if mint not in cache and launch.get("uri"):
            by_uri[str(launch["uri"])].append(launch)
    uris = list(by_uri)
    async with Fetcher(concurrency, timeout_seconds) as fetcher:
        for start in range(0, len(uris), 400):
            chunk = uris[start : start + 400]
            results = await asyncio.gather(*(fetcher.one(uri) for uri in chunk))
            for uri, metadata in zip(chunk, results):
                for launch in by_uri[uri]:
                    cache[launch["mint"]] = social_fields(metadata, launch["create_ns"])
            print(json.dumps({
                "metadata_processed": min(len(uris), start + len(chunk)),
                "metadata_target": len(uris),
            }), flush=True)
    return cache


def raw_social(raw: Mapping[str, Any], create_ns: int) -> dict[str, Any] | None:
    direct = {}
    for key in (
        "twitter", "x", "website", "description", "social", "socials",
        "metadata", "extensions",
    ):
        if key in raw:
            direct[key] = raw[key]
    if not direct:
        return None
    result = social_fields(direct, create_ns)
    return result if result["metadata_ok"] or result["twitter_handle"] else None


def launch_rows(runs: Sequence[replay.RunData]) -> dict[tuple[str, str], dict[str, Any]]:
    output = {}
    for run_index, run in enumerate(runs):
        for mint, rows in run.events_by_mint.items():
            create = next(
                (row for row in rows if str(row.get("kind") or "").upper() == "CREATE"),
                None,
            )
            if create is None:
                continue
            raw = create.get("raw") if isinstance(create.get("raw"), Mapping) else {}
            creator = str(create.get("creator") or raw.get("creator") or create.get("trader") or "")
            first_reserve = next(
                (
                    row for row in rows
                    if replay.reserve_from_row(row, integer(row.get("__sequence"), 0)) is not None
                ),
                None,
            )
            if first_reserve is None:
                continue
            embedded = raw_social(raw, integer(create.get("received_ns")))
            output[(run.run_id, mint)] = {
                "run_id": run.run_id,
                "run_index": run_index,
                "mint": mint,
                "creator": creator,
                "create_ns": integer(create.get("received_ns")),
                "create_slot": integer(create.get("slot")),
                "create_signature": str(create.get("signature") or ""),
                "uri": str(raw.get("uri") or create.get("uri") or ""),
                "metadata_host": (urlparse(str(raw.get("uri") or "")).netloc or "").lower(),
                "mayhem": bool(raw.get("is_mayhem_mode") or raw.get("mayhem")),
                "decision_event": first_reserve,
                "embedded_social": embedded,
            }
    return output


def state_at_decision(run: replay.RunData, launch: Mapping[str, Any]) -> dict[str, Any]:
    decision = launch["decision_event"]
    decision_key = (
        integer(decision.get("received_ns")),
        integer(decision.get("__sequence"), -1),
    )
    seed = outside = 0.0
    buyers: set[str] = set()
    buy_count = same_slot = create_signature_buys = 0
    sell_count = 0
    fdv = finite(decision.get("fdv_usd"))
    for row in run.events_by_mint.get(launch["mint"], []):
        key = (integer(row.get("received_ns")), integer(row.get("__sequence"), -1))
        if key > decision_key:
            break
        kind = str(row.get("kind") or "").upper()
        trader = str(row.get("trader") or "")
        if finite(row.get("fdv_usd")) > 0:
            fdv = finite(row.get("fdv_usd"))
        if kind in replay.BUY_KINDS and trader != replay.E4_WALLET:
            amount = max(0.0, finite(row.get("sol_amount")))
            buy_count += 1
            if integer(row.get("slot")) == launch["create_slot"]:
                same_slot += 1
            if str(row.get("signature") or "") == launch["create_signature"]:
                create_signature_buys += 1
            if trader and trader == launch["creator"]:
                seed += amount
            elif trader:
                outside += amount
                buyers.add(trader)
        elif kind in replay.SELL_KINDS and trader != replay.E4_WALLET:
            sell_count += 1
    return {
        "creator_seed_sol": seed,
        "outside_sol": outside,
        "unique_buyers": len(buyers),
        "buy_count": buy_count,
        "same_slot_buys": same_slot,
        "create_signature_buys": create_signature_buys,
        "sell_count": sell_count,
        "fdv_usd": fdv,
    }


@dataclass(frozen=True)
class Rule:
    minimum_tweet_age_seconds: float
    maximum_tweet_age_seconds: float
    minimum_creator_wins: int
    maximum_creator_losses: int
    minimum_creator_rate: float
    minimum_handle_wins: int
    maximum_handle_losses: int
    minimum_handle_rate: float
    minimum_creator_seed_sol: float
    minimum_outside_sol: float
    minimum_unique_buyers: int
    minimum_fdv_usd: float
    maximum_fdv_usd: float
    require_website: bool
    require_creator_or_handle_history: bool
    output_shortfall_bps: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_snapshots(
    runs: Sequence[replay.RunData],
    launches: Mapping[tuple[str, str], dict[str, Any]],
    metadata: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    creator_wins: Counter[str] = Counter()
    creator_losses: Counter[str] = Counter()
    handle_wins: Counter[str] = Counter()
    handle_losses: Counter[str] = Counter()
    output: list[dict[str, Any]] = []
    for run_index, run in enumerate(runs):
        run_launches = [
            value for key, value in launches.items() if key[0] == run.run_id
        ]
        run_launches.sort(key=lambda row: integer(row["decision_event"].get("received_ns")))
        for launch in run_launches:
            meta = dict(metadata.get(launch["mint"], {}))
            if not meta and launch.get("embedded_social"):
                meta = dict(launch["embedded_social"])
            age = meta.get("tweet_age_seconds")
            if age is None or not math.isfinite(finite(age, float("nan"))):
                continue
            creator = launch["creator"]
            handle = str(meta.get("twitter_handle") or "").lower().lstrip("@")
            state = state_at_decision(run, launch)
            decision = launch["decision_event"]
            source_buy, _ = replay.source_events(run.events_by_mint.get(launch["mint"], []))
            source_position = run.e4_positions.get(launch["mint"], {})
            source_won = finite(source_position.get("pnl_sol")) > 0
            creator_trades = creator_wins[creator] + creator_losses[creator]
            handle_trades = handle_wins[handle] + handle_losses[handle]
            output.append({
                "run_id": run.run_id,
                "run_index": run_index,
                "mint": launch["mint"],
                "creator": creator,
                "twitter_handle": handle,
                "decision_ns": integer(decision.get("received_ns")),
                "decision_sequence": integer(decision.get("__sequence"), -1),
                "decision_event_id": decision.get("event_id"),
                "decision_signature": str(decision.get("signature") or ""),
                "decision_event_index": integer(decision.get("event_index")),
                "tweet_age_seconds": finite(age),
                "tweet_time_ns": meta.get("tweet_time_ns"),
                "website_present": bool(meta.get("website_present")),
                "metadata_host": launch.get("metadata_host"),
                "mayhem": launch.get("mayhem"),
                "creator_wins": creator_wins[creator],
                "creator_losses": creator_losses[creator],
                "creator_rate": creator_wins[creator] / creator_trades if creator_trades else 0.0,
                "handle_wins": handle_wins[handle] if handle else 0,
                "handle_losses": handle_losses[handle] if handle else 0,
                "handle_rate": handle_wins[handle] / handle_trades if handle_trades else 0.0,
                "source_intent": source_buy is not None,
                "source_won": source_won,
                **state,
            })
        # The entire current run remains out of its own prior history.  This is
        # deliberately conservative and prevents same-window outcome leakage.
        for launch in run_launches:
            position = run.e4_positions.get(launch["mint"])
            if not position:
                continue
            meta = dict(metadata.get(launch["mint"], {}))
            if not meta and launch.get("embedded_social"):
                meta = dict(launch["embedded_social"])
            creator = launch["creator"]
            handle = str(meta.get("twitter_handle") or "").lower().lstrip("@")
            won = finite(position.get("pnl_sol")) > 0
            if creator:
                (creator_wins if won else creator_losses)[creator] += 1
            if handle:
                (handle_wins if won else handle_losses)[handle] += 1
        print(json.dumps({
            "run_id": run.run_id,
            "prelaunch_social_snapshots": sum(integer(row["run_index"]) == run_index for row in output),
            "known_winning_handles": len(handle_wins),
        }), flush=True)
    return output


def accepts(row: Mapping[str, Any], rule: Rule) -> bool:
    age = finite(row.get("tweet_age_seconds"), -1.0)
    creator_history = integer(row.get("creator_wins")) >= rule.minimum_creator_wins
    handle_history = integer(row.get("handle_wins")) >= rule.minimum_handle_wins
    return bool(
        rule.minimum_tweet_age_seconds <= age <= rule.maximum_tweet_age_seconds
        and integer(row.get("creator_wins")) >= rule.minimum_creator_wins
        and integer(row.get("creator_losses")) <= rule.maximum_creator_losses
        and finite(row.get("creator_rate")) >= rule.minimum_creator_rate
        and integer(row.get("handle_wins")) >= rule.minimum_handle_wins
        and integer(row.get("handle_losses")) <= rule.maximum_handle_losses
        and finite(row.get("handle_rate")) >= rule.minimum_handle_rate
        and finite(row.get("creator_seed_sol")) >= rule.minimum_creator_seed_sol
        and finite(row.get("outside_sol")) >= rule.minimum_outside_sol
        and integer(row.get("unique_buyers")) >= rule.minimum_unique_buyers
        and rule.minimum_fdv_usd <= finite(row.get("fdv_usd")) <= rule.maximum_fdv_usd
        and integer(row.get("sell_count")) == 0
        and not bool(row.get("mayhem"))
        and (not rule.require_website or bool(row.get("website_present")))
        and (not rule.require_creator_or_handle_history or creator_history or handle_history)
    )


def select(rows: Sequence[Mapping[str, Any]], rule: Rule) -> list[dict[str, Any]]:
    output = []
    touched = set()
    for row in sorted(rows, key=lambda item: (integer(item.get("decision_ns")), str(item.get("mint") or ""))):
        key = (str(row.get("run_id") or ""), str(row.get("mint") or ""))
        if key in touched or not accepts(row, rule):
            continue
        touched.add(key)
        output.append({
            "run_id": row["run_id"],
            "mint": row["mint"],
            "decision_ns": row["decision_ns"],
            "decision_sequence": row["decision_sequence"],
            "decision_event_id": row["decision_event_id"],
            "decision_signature": row["decision_signature"],
            "decision_event_index": row["decision_event_index"],
            "score": 0.98,
            "family": "v12_prelaunch_social_intent",
            "entry_fraction": 0.0185,
            "source_won": row.get("source_won"),
            "tweet_age_seconds": row.get("tweet_age_seconds"),
            "twitter_handle": row.get("twitter_handle"),
        })
    return output


def rules() -> list[Rule]:
    output = []
    for minimum_age, maximum_age in (
        (0.10, 5.0),
        (0.50, 10.0),
        (1.0, 30.0),
        (5.0, 120.0),
        (10.0, 600.0),
        (0.10, 600.0),
    ):
        for creator_wins, creator_losses, creator_rate in (
            (0, 99, 0.0),
            (1, 1, 0.50),
            (2, 1, 0.66),
            (3, 1, 0.75),
        ):
            for handle_wins, handle_losses, handle_rate in (
                (0, 99, 0.0),
                (1, 1, 0.50),
                (2, 1, 0.66),
            ):
                for seed, outside, buyers in (
                    (0.0, 0.0, 0),
                    (0.25, 0.0, 0),
                    (0.50, 0.25, 1),
                    (1.50, 1.0, 1),
                ):
                    for fdv_min, fdv_max in (
                        (2_750.0, 5_000.0),
                        (3_200.0, 7_500.0),
                        (2_750.0, 10_000.0),
                    ):
                        for website in (False, True):
                            for history in (False, True):
                                for floor in (200, 400, 600, 800, 1_000):
                                    output.append(Rule(
                                        minimum_age, maximum_age,
                                        creator_wins, creator_losses, creator_rate,
                                        handle_wins, handle_losses, handle_rate,
                                        seed, outside, buyers,
                                        fdv_min, fdv_max,
                                        website, history, floor,
                                    ))
    return output


def compact(grid: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    return golden.compact_grid(grid)


def search(
    runs: Sequence[replay.RunData],
    snapshots: Sequence[dict[str, Any]],
    latencies: Sequence[float],
) -> dict[str, Any]:
    run_map = {run.run_id: run for run in runs}
    count = len(runs)
    train_end = count - 4
    validation_end = count - 2
    train = [row for row in snapshots if integer(row["run_index"]) < train_end]
    validation = [row for row in snapshots if train_end <= integer(row["run_index"]) < validation_end]
    holdout = [row for row in snapshots if integer(row["run_index"]) >= validation_end]
    seen: set[tuple[tuple[str, str], ...]] = set()
    shortlist = []
    for rule in rules():
        train_predictions = select(train, rule)
        key = tuple((row["run_id"], row["mint"]) for row in train_predictions)
        if len(key) < 8 or key in seen:
            continue
        seen.add(key)
        train_grid = golden.economic_grid(
            run_map, train_predictions,
            floor_bps=rule.output_shortfall_bps,
            latencies=latencies,
        )
        if not golden.economics_pass(train_grid, 8):
            continue
        validation_predictions = select(validation, rule)
        if len(validation_predictions) < 3:
            continue
        validation_grid = golden.economic_grid(
            run_map, validation_predictions,
            floor_bps=rule.output_shortfall_bps,
            latencies=latencies,
        )
        if not golden.economics_pass(validation_grid, 3):
            continue
        score = (
            min(finite(value["win_rate"]) for value in validation_grid.values()),
            min(finite(value["wilson_low"]) for value in validation_grid.values()),
            min(finite(value["profit_factor"]) for value in validation_grid.values()),
            sum(finite(value["net_pnl_sol"]) for value in validation_grid.values()),
            -len(validation_predictions),
        )
        shortlist.append((score, rule, train_grid, validation_grid))
    shortlist.sort(key=lambda item: item[0], reverse=True)
    best = None
    for score, rule, train_grid, validation_grid in shortlist[:50]:
        holdout_predictions = select(holdout, rule)
        holdout_grid = golden.economic_grid(
            run_map, holdout_predictions,
            floor_bps=rule.output_shortfall_bps,
            latencies=latencies,
        )
        objective = (
            int(golden.economics_pass(holdout_grid, 3)),
            min((finite(value["win_rate"]) for value in holdout_grid.values()), default=0.0),
            min((finite(value["wilson_low"]) for value in holdout_grid.values()), default=0.0),
            min((finite(value["profit_factor"]) for value in holdout_grid.values()), default=0.0),
            sum(finite(value["net_pnl_sol"]) for value in holdout_grid.values()),
            *score,
        )
        if best is None or objective > best[0]:
            best = (objective, rule, train_grid, validation_grid, holdout_grid)
    passed = bool(best and best[0][0])
    payload = {
        "version": "e4-v12-prelaunch-social-thesis-v1",
        "status": "HISTORICAL_GOLDEN_CONFIRMED" if passed else "NOT_CONCLUSIVE",
        "run_ids": [run.run_id for run in runs],
        "snapshots": len(snapshots),
        "unique_prediction_sets_checked": len(seen),
        "shortlisted_rules": len(shortlist),
    }
    if best:
        _, rule, train_grid, validation_grid, holdout_grid = best
        payload.update({
            "rule": rule.as_dict(),
            "train": compact(train_grid),
            "validation": compact(validation_grid),
            "historical_holdout": compact(holdout_grid),
        })
    return payload


async def async_main(args: argparse.Namespace) -> int:
    runs = [replay.load_run(*replay.parse_pair(value)) for value in args.pair]
    if len(runs) < 8:
        raise SystemExit("at least eight chronological runs are required")
    launches = launch_rows(runs)
    metadata = scan_cache(args.metadata_cache)
    for launch in launches.values():
        if launch.get("embedded_social") and launch["mint"] not in metadata:
            metadata[launch["mint"]] = dict(launch["embedded_social"])
    metadata = await fill_metadata(
        launches,
        metadata,
        concurrency=args.metadata_concurrency,
        timeout_seconds=args.metadata_timeout_seconds,
    )
    args.metadata_output.parent.mkdir(parents=True, exist_ok=True)
    args.metadata_output.write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")
    snapshots = build_snapshots(runs, launches, metadata)
    latencies = [finite(value) for value in args.latencies_ms.split(",") if value.strip()]
    payload = search(runs, snapshots, latencies)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(payload, indent=2, sort_keys=True), flush=True)
    return 0 if payload["status"] == "HISTORICAL_GOLDEN_CONFIRMED" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Search causal prelaunch social-intent entries")
    parser.add_argument("--pair", action="append", default=[])
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
