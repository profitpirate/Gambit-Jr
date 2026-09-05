#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlparse

import aiohttp

from scripts import e4_v12_golden_thesis_search as golden
from scripts import e4_v12_true_latency_replay as economics

X_EPOCH_MS = 1_288_834_974_657
STATUS_RE = re.compile(r"(?:x|twitter)\.com/([^/?#]+)/status(?:es)?/(\d+)", re.I)
HANDLE_RE = re.compile(r"(?:x|twitter)\.com/([^/?#]+)", re.I)


def finite(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


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
        output.extend(
            [
                f"https://ipfs.io/ipfs/{key}",
                f"https://gateway.pinata.cloud/ipfs/{key}",
            ]
        )
    return list(dict.fromkeys(output))


def walk_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).lower() in {
                "twitter",
                "x",
                "social",
                "socials",
                "website",
                "description",
                "telegram",
                "created_by",
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
    status_url = ""
    for value in values:
        match = STATUS_RE.search(value)
        if match:
            handle = match.group(1).lower().lstrip("@")
            status_id = match.group(2)
            status_url = value
            break
    if not handle:
        for value in values:
            match = HANDLE_RE.search(value)
            if match:
                handle = match.group(1).lower().lstrip("@")
                break
    tweet_age_seconds = None
    if status_id:
        tweet_ms = (int(status_id) >> 22) + X_EPOCH_MS
        tweet_age_seconds = create_ns / 1_000_000.0 / 1_000.0 - tweet_ms / 1_000.0
    return {
        "metadata_ok": bool(metadata),
        "twitter_handle": handle,
        "twitter_status_id": status_id,
        "twitter_status_url": status_url,
        "tweet_age_seconds": tweet_age_seconds,
        "website_present": any(
            "http" in value.lower()
            and "twitter.com" not in value.lower()
            and "x.com" not in value.lower()
            for value in values
        ),
    }


def launch_uri(launch: golden.Launch) -> str:
    create = next(
        (row for row in launch.events if str(row.get("kind") or "").upper() == "CREATE"),
        {},
    )
    raw = create.get("raw") if isinstance(create.get("raw"), Mapping) else {}
    return str(raw.get("uri") or create.get("uri") or "")


class Fetcher:
    def __init__(self, concurrency: int, timeout_seconds: float) -> None:
        self.sem = asyncio.Semaphore(max(1, concurrency))
        self.timeout = aiohttp.ClientTimeout(total=max(0.25, timeout_seconds))
        self.session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "Fetcher":
        self.session = aiohttp.ClientSession(
            timeout=self.timeout,
            connector=aiohttp.TCPConnector(
                limit=256,
                ttl_dns_cache=600,
                keepalive_timeout=45,
                enable_cleanup_closed=True,
            ),
            headers={"accept": "application/json"},
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
                    async with self.session.get(url) as response:
                        if response.status >= 400:
                            continue
                        payload = await response.json(content_type=None)
                        if isinstance(payload, Mapping):
                            return payload
                except Exception:
                    continue
        return None


async def fetch_social(
    runs: Sequence[golden.RunData],
    cache_path: Path,
    concurrency: int,
    timeout_seconds: float,
) -> dict[str, dict[str, Any]]:
    if cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            cached = {}
    else:
        cached = {}
    by_uri: defaultdict[str, list[golden.Launch]] = defaultdict(list)
    for run in runs:
        for launch in run.launches.values():
            if launch.mint in cached:
                continue
            uri = launch_uri(launch)
            if uri:
                by_uri[uri].append(launch)
            else:
                cached[launch.mint] = social_fields(None, launch.create_ns)
    uris = list(by_uri)
    async with Fetcher(concurrency, timeout_seconds) as fetcher:
        for start in range(0, len(uris), 300):
            chunk = uris[start : start + 300]
            payloads = await asyncio.gather(*(fetcher.one(uri) for uri in chunk))
            for uri, payload in zip(chunk, payloads):
                for launch in by_uri[uri]:
                    cached[launch.mint] = social_fields(payload, launch.create_ns)
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(cached, indent=2, sort_keys=True), encoding="utf-8")
            print(json.dumps({"metadata": min(len(uris), start + len(chunk)), "target": len(uris)}), flush=True)
    return cached


@dataclass(frozen=True)
class Rule:
    maximum_tweet_age_seconds: float
    minimum_prior_handle_attempts: int
    minimum_prior_handle_wins: int
    minimum_prior_creator_attempts: int
    minimum_prior_creator_wins: int
    maximum_prior_creator_losses: int
    minimum_fdv_usd: float
    maximum_fdv_usd: float
    stage: str
    max_output_shortfall_bps: int

    def accepts(self, row: Mapping[str, Any]) -> bool:
        age = row.get("tweet_age_seconds")
        return bool(
            age is not None
            and 0.0 <= finite(age, -1.0) <= self.maximum_tweet_age_seconds
            and integer(row.get("prior_handle_attempts")) >= self.minimum_prior_handle_attempts
            and integer(row.get("prior_handle_wins")) >= self.minimum_prior_handle_wins
            and integer(row.get("prior_creator_attempts")) >= self.minimum_prior_creator_attempts
            and integer(row.get("prior_creator_wins")) >= self.minimum_prior_creator_wins
            and integer(row.get("prior_creator_losses")) <= self.maximum_prior_creator_losses
            and self.minimum_fdv_usd <= finite(row.get("fdv_usd")) <= self.maximum_fdv_usd
            and str(row.get("stage")) == self.stage
        )

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Rule":
        return cls(**{key: value[key] for key in cls.__dataclass_fields__})


def stage_rows(
    run: golden.RunData,
    social: Mapping[str, Mapping[str, Any]],
    creator_attempts: Counter[str],
    creator_wins: Counter[str],
    creator_losses: Counter[str],
    handle_attempts: Counter[str],
    handle_wins: Counter[str],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for launch in run.launches.values():
        meta = social.get(launch.mint, {})
        handle = str(meta.get("twitter_handle") or "")
        tweet_age = meta.get("tweet_age_seconds")
        if not handle or tweet_age is None or finite(tweet_age, -1.0) < 0:
            continue
        state = golden.LaunchState(latest_ns=launch.create_ns)
        emitted: set[str] = set()
        for event in launch.events:
            if launch.e4_buy is not None and economics.event_order(event) >= economics.event_order(launch.e4_buy):
                break
            if str(event.get("trader") or "") == economics.E4_WALLET:
                break
            golden.apply_event(launch, state, event)
            kind = str(event.get("kind") or "").upper()
            stage = "create" if kind == "CREATE" else "first_flow"
            if stage in emitted:
                continue
            if stage == "first_flow" and kind not in economics.BUY_KINDS:
                continue
            if state.fdv_usd <= 0 or state.sell_count > 0:
                continue
            emitted.add(stage)
            e4_won = bool(launch.e4_position and finite(launch.e4_position.get("pnl_sol")) > 0)
            rows.append(
                {
                    "mint": launch.mint,
                    "run_id": run.run_id,
                    "run_index": run.run_index,
                    "decision_ns": integer(event.get("received_ns")),
                    "requested_fraction": 0.0185,
                    "score": 0.99,
                    "mode": "v12_social_prearm",
                    "stage": stage,
                    "creator": launch.creator,
                    "twitter_handle": handle,
                    "twitter_status_id": str(meta.get("twitter_status_id") or ""),
                    "tweet_age_seconds": finite(tweet_age),
                    "website_present": bool(meta.get("website_present")),
                    "fdv_usd": state.fdv_usd,
                    "creator_seed_sol": state.creator_seed_sol,
                    "outside_sol": state.outside_sol,
                    "prior_handle_attempts": handle_attempts[handle],
                    "prior_handle_wins": handle_wins[handle],
                    "prior_creator_attempts": creator_attempts[launch.creator],
                    "prior_creator_wins": creator_wins[launch.creator],
                    "prior_creator_losses": creator_losses[launch.creator],
                    "e4_won": e4_won,
                    "e4_pnl_sol": finite(launch.e4_position.get("pnl_sol")) if launch.e4_position else None,
                }
            )
    return rows


def build_rows(runs: Sequence[golden.RunData], social: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    creator_attempts: Counter[str] = Counter()
    creator_wins: Counter[str] = Counter()
    creator_losses: Counter[str] = Counter()
    handle_attempts: Counter[str] = Counter()
    handle_wins: Counter[str] = Counter()
    output: list[dict[str, Any]] = []
    for run in runs:
        current = stage_rows(
            run,
            social,
            creator_attempts,
            creator_wins,
            creator_losses,
            handle_attempts,
            handle_wins,
        )
        output.extend(current)
        for launch in run.launches.values():
            if launch.e4_buy is None:
                continue
            meta = social.get(launch.mint, {})
            handle = str(meta.get("twitter_handle") or "")
            won = bool(launch.e4_position and finite(launch.e4_position.get("pnl_sol")) > 0)
            creator_attempts[launch.creator] += 1
            (creator_wins if won else creator_losses)[launch.creator] += 1
            if handle:
                handle_attempts[handle] += 1
                if won:
                    handle_wins[handle] += 1
        print(json.dumps({"run_id": run.run_id, "social_rows": len(current)}), flush=True)
    return output


def rules() -> Iterable[Rule]:
    for age in (1.0, 3.0, 5.0, 15.0, 30.0, 120.0, 600.0):
        for handle_attempts, handle_wins, creator_attempts, creator_wins, max_losses in (
            (0, 0, 0, 0, 99),
            (1, 0, 0, 0, 99),
            (1, 1, 0, 0, 99),
            (2, 1, 0, 0, 99),
            (0, 0, 1, 0, 99),
            (0, 0, 1, 1, 0),
            (0, 0, 2, 1, 1),
            (1, 1, 1, 1, 0),
            (2, 1, 1, 1, 0),
        ):
            for minimum_fdv, maximum_fdv in (
                (2_500.0, 6_000.0),
                (2_500.0, 8_500.0),
                (2_500.0, 10_000.0),
                (3_000.0, 7_500.0),
                (3_500.0, 8_500.0),
            ):
                for stage in ("create", "first_flow"):
                    for guard in (200, 400, 600, 800):
                        yield Rule(
                            age,
                            handle_attempts,
                            handle_wins,
                            creator_attempts,
                            creator_wins,
                            max_losses,
                            minimum_fdv,
                            maximum_fdv,
                            stage,
                            guard,
                        )


def select(rows: Sequence[Mapping[str, Any]], rule: Rule) -> list[dict[str, Any]]:
    chosen: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in sorted(rows, key=lambda item: (integer(item.get("decision_ns")), str(item.get("mint")))):
        mint = str(row.get("mint") or "")
        if mint and mint not in seen and rule.accepts(row):
            seen.add(mint)
            chosen.append(dict(row))
    return chosen


def evaluate(
    runs: Sequence[golden.RunData],
    rows: Sequence[Mapping[str, Any]],
    rule: Rule,
    latencies: Sequence[float],
    starting_balance_sol: float,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    predictions = select(rows, rule)
    metrics = golden.aggregate_economics(
        runs,
        predictions,
        latencies,
        starting_balance_sol=starting_balance_sol,
        max_output_shortfall_bps=rule.max_output_shortfall_bps,
    )
    return predictions, metrics


def search_mode(args: argparse.Namespace) -> int:
    pairs = [golden.parse_pair(value) for value in args.pair]
    runs = golden.load_runs(pairs)
    social = asyncio.run(fetch_social(runs, args.metadata_cache, args.metadata_concurrency, args.metadata_timeout))
    rows = build_rows(runs, social)
    holdout_start = len(runs) - 2
    validation_start = max(4, holdout_start - 2)
    train_runs = runs[:validation_start]
    validation_runs = runs[validation_start:holdout_start]
    holdout_runs = runs[holdout_start:]
    train_rows = [row for row in rows if integer(row.get("run_index")) < validation_start]
    validation_rows = [row for row in rows if validation_start <= integer(row.get("run_index")) < holdout_start]
    holdout_rows = [row for row in rows if integer(row.get("run_index")) >= holdout_start]
    latencies = economics.parse_latencies(args.latencies)

    best: tuple[Any, ...] | None = None
    tested = 0
    for rule in rules():
        tested += 1
        train_predictions = select(train_rows, rule)
        validation_predictions = select(validation_rows, rule)
        if len(train_predictions) < 8 or len(validation_predictions) < 3:
            continue
        if sum(bool(row.get("e4_won")) for row in validation_predictions) / len(validation_predictions) < 0.50:
            continue
        _, train_metrics = evaluate(train_runs, train_rows, rule, latencies, args.starting_balance_sol)
        if not all(golden.passes_economics(block, 8, args.minimum_win_rate, args.minimum_profit_factor) for block in train_metrics.values()):
            continue
        _, validation_metrics = evaluate(validation_runs, validation_rows, rule, latencies, args.starting_balance_sol)
        if not all(golden.passes_economics(block, 3, args.minimum_win_rate, args.minimum_profit_factor) for block in validation_metrics.values()):
            continue
        objective = (
            min(finite(block.get("win_rate")) for block in validation_metrics.values()),
            min(finite(block.get("wilson_low")) for block in validation_metrics.values()),
            min(finite(block.get("profit_factor")) for block in validation_metrics.values()),
            sum(finite(block.get("net_pnl_sol")) for block in validation_metrics.values()),
            len(validation_predictions),
        )
        candidate = (objective, rule, train_metrics, validation_metrics)
        if best is None or objective > best[0]:
            best = candidate

    if best is None:
        report = {"version": "e4-v12-social-prearm-search-v1", "status": "NOT_CONCLUSIVE", "tested_rules": tested, "metadata_rows": len(rows)}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        print(json.dumps(report, indent=2, sort_keys=True))
        return 2

    _, rule, train_metrics, validation_metrics = best
    holdout_predictions, holdout_metrics = evaluate(holdout_runs, holdout_rows, rule, latencies, args.starting_balance_sol)
    passed = bool(
        len(holdout_predictions) >= 3
        and all(golden.passes_economics(block, 3, args.minimum_win_rate, args.minimum_profit_factor) for block in holdout_metrics.values())
    )
    status = "HISTORICAL_HOLDOUT_CONFIRMED" if passed else "NOT_CONCLUSIVE"
    report = {
        "version": "e4-v12-social-prearm-search-v1",
        "status": status,
        "thesis": "Pre-arm a creator/social identity only when an exact X status existed before launch and its frozen recurrence/FDV gate qualifies; enter at CREATE or first flow with strict output protection.",
        "rule": rule.as_dict(),
        "latencies_ms": latencies,
        "starting_balance_sol": args.starting_balance_sol,
        "train": train_metrics,
        "validation": validation_metrics,
        "holdout": holdout_metrics,
        "holdout_predictions": len(holdout_predictions),
        "train_runs": [run.run_id for run in train_runs],
        "validation_runs": [run.run_id for run in validation_runs],
        "holdout_runs": [run.run_id for run in holdout_runs],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    args.model_output.write_text(json.dumps({"version": "e4-v12-social-prearm-model-v1", "status": status, "rule": rule.as_dict()}, indent=2, sort_keys=True), encoding="utf-8")
    args.predictions_output.write_text(json.dumps({"predictions": holdout_predictions}, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({"status": status, "rule": rule.as_dict()}, indent=2, sort_keys=True))
    return 0 if passed else 3


def apply_mode(args: argparse.Namespace) -> int:
    model = json.loads(args.model_input.read_text(encoding="utf-8"))
    rule = Rule.from_dict(model["rule"])
    pairs = [golden.parse_pair(value) for value in args.pair]
    runs = golden.load_runs(pairs)
    social = asyncio.run(fetch_social(runs, args.metadata_cache, args.metadata_concurrency, args.metadata_timeout))
    rows = build_rows(runs, social)
    live_index = len(runs) - 1
    predictions = select([row for row in rows if integer(row.get("run_index")) == live_index], rule)
    args.predictions_output.parent.mkdir(parents=True, exist_ok=True)
    args.predictions_output.write_text(json.dumps({"version": "e4-v12-social-prearm-live-predictions-v1", "live_run_id": runs[-1].run_id, "rule": rule.as_dict(), "predictions": predictions}, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({"live_run_id": runs[-1].run_id, "predictions": len(predictions)}, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Search a causal prelaunch social/creator V12 thesis")
    parser.add_argument("--mode", choices=("search", "apply"), default="search")
    parser.add_argument("--pair", action="append", default=[])
    parser.add_argument("--latencies", default="0,1,2,5,10")
    parser.add_argument("--starting-balance-sol", type=float, default=3.0)
    parser.add_argument("--minimum-win-rate", type=float, default=0.65)
    parser.add_argument("--minimum-profit-factor", type=float, default=1.25)
    parser.add_argument("--metadata-cache", type=Path, default=Path("artifacts/e4-v12-social-cache.json"))
    parser.add_argument("--metadata-concurrency", type=int, default=120)
    parser.add_argument("--metadata-timeout", type=float, default=2.5)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model-output", type=Path, required=True)
    parser.add_argument("--model-input", type=Path)
    parser.add_argument("--predictions-output", type=Path, required=True)
    args = parser.parse_args()
    if args.mode == "apply":
        if args.model_input is None:
            parser.error("--model-input is required in apply mode")
        return apply_mode(args)
    return search_mode(args)


if __name__ == "__main__":
    raise SystemExit(main())
