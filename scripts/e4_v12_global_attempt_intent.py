#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import math
import re
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

import aiohttp

E4_WALLET = "E4EzXdwf7NNdqM2XGswWaWHfxgucVCo24PTCcrimTKBz"
X_EPOCH_MS = 1_288_834_974_657
STATUS_RE = re.compile(r"(?:x|twitter)\.com/([^/?#]+)/status(?:es)?/(\d+)", re.I)
HANDLE_RE = re.compile(r"(?:x|twitter)\.com/([^/?#]+)", re.I)


def finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def median(values: list[float]) -> float | None:
    values = [float(value) for value in values if math.isfinite(float(value))]
    return statistics.median(values) if values else None


def metadata_url(uri: str) -> list[str]:
    text = str(uri or "").strip()
    if not text:
        return []
    if text.startswith("ipfs://"):
        key = text[7:].lstrip("/")
        return [f"https://ipfs.io/ipfs/{key}", f"https://gateway.pinata.cloud/ipfs/{key}"]
    if text.startswith("ar://"):
        return [f"https://arweave.net/{text[5:].lstrip('/')}"]
    urls = [text]
    marker = "/ipfs/"
    if marker in text:
        key = text.split(marker, 1)[1]
        urls.extend([f"https://ipfs.io/ipfs/{key}", f"https://gateway.pinata.cloud/ipfs/{key}"])
    return list(dict.fromkeys(urls))


def walk_strings(value: Any) -> list[str]:
    output: list[str] = []
    if isinstance(value, str):
        output.append(value)
    elif isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).lower() in {"twitter", "x", "social", "socials", "website", "description", "telegram"}:
                output.extend(walk_strings(item))
            elif isinstance(item, (Mapping, list, tuple)):
                output.extend(walk_strings(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            output.extend(walk_strings(item))
    return output


def social_fields(metadata: Mapping[str, Any] | None, create_ns: int) -> dict[str, Any]:
    values = walk_strings(metadata or {})
    status_id = ""
    handle = ""
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
    tweet_age = None
    if status_id:
        tweet_ms = (int(status_id) >> 22) + X_EPOCH_MS
        tweet_age = (create_ns / 1e6 - tweet_ms) / 1000.0
    return {
        "twitter": twitter,
        "twitter_handle": handle,
        "twitter_status_id": status_id,
        "tweet_age_seconds": tweet_age,
    }


def history_map(path: Path) -> dict[str, dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    output = {}
    for row in data.get("top_creators") or []:
        creator = str(row.get("creator") or "")
        if not creator:
            continue
        wins = integer(row.get("wins"))
        losses = integer(row.get("losses"))
        trades = max(integer(row.get("trades")), wins + losses)
        output[creator] = {
            "wins": wins,
            "losses": losses,
            "trades": trades,
            "rate": finite(row.get("gross_win_rate"), wins / trades if trades else 0.0),
        }
    return output


def failed_attempts(path: Path) -> dict[str, dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    output = {}
    for row in (data.get("failed_attempts") or {}).get("rows") or []:
        if not row.get("captured_mint"):
            continue
        mint = str(row.get("mapped_mint") or "")
        if mint:
            output[mint] = dict(row)
    return output


@dataclass
class Launch:
    mint: str
    run: str
    run_index: int
    creator: str
    create_ns: int
    create_slot: int
    uri: str
    host: str
    token_program: str
    mayhem: bool
    cashback: bool
    seed_sol: float = 0.0
    fdv_usd: float = 0.0
    price_multiple: float = 1.0
    buy_count: int = 0
    unique_buyers: int = 0
    outside_sol: float = 0.0
    same_slot_buys: int = 0
    first_buyers: tuple[str, ...] = ()
    first_snapshot_ns: int = 0
    successful: bool = False
    success_ns: int = 0


@dataclass(frozen=True)
class Rule:
    strict_seconds: float
    loose_seconds: float
    seed_sol: float
    identity: str
    require_cashback: bool
    max_fdv: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "strict_seconds": self.strict_seconds,
            "loose_seconds": self.loose_seconds,
            "seed_sol": self.seed_sol,
            "identity": self.identity,
            "require_cashback": self.require_cashback,
            "max_fdv": self.max_fdv,
        }


def parse_pairs(values: list[str]) -> list[tuple[Path, Path]]:
    output = []
    for value in values:
        batch, events = value.split(":", 1)
        output.append((Path(batch), Path(events)))
    return output


def load_launches(pairs: list[tuple[Path, Path]]) -> dict[str, Launch]:
    launches: dict[str, Launch] = {}
    for run_index, (_, events_path) in enumerate(pairs):
        run = events_path.parts[-3] if len(events_path.parts) >= 3 else str(run_index)
        states: dict[str, dict[str, Any]] = {}
        with events_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                row = json.loads(line)
                mint = str(row.get("mint") or "")
                kind = str(row.get("kind") or "").upper()
                trader = str(row.get("trader") or "")
                now_ns = integer(row.get("received_ns"))
                slot = integer(row.get("slot"))
                raw = row.get("raw") if isinstance(row.get("raw"), Mapping) else {}
                if kind == "CREATE":
                    creator = str(row.get("creator") or raw.get("creator") or trader or "")
                    price = finite(row.get("price_sol"))
                    launch = Launch(
                        mint=mint,
                        run=run,
                        run_index=run_index,
                        creator=creator,
                        create_ns=now_ns,
                        create_slot=slot,
                        uri=str(raw.get("uri") or ""),
                        host=(urlparse(str(raw.get("uri") or "")).netloc or "").lower(),
                        token_program=str(raw.get("token_program") or ""),
                        mayhem=bool(raw.get("is_mayhem_mode")),
                        cashback=bool(raw.get("is_cashback_enabled")),
                        fdv_usd=finite(row.get("fdv_usd")),
                    )
                    launches[mint] = launch
                    states[mint] = {
                        "launch": launch,
                        "initial_price": price,
                        "price": price,
                        "buyers": set(),
                        "first_buyers": [],
                        "buy_slots": Counter(),
                    }
                    continue
                state = states.get(mint)
                if state is None:
                    continue
                launch = state["launch"]
                if trader == E4_WALLET and kind in {"BUY", "PUMPSWAP_BUY"}:
                    launch.successful = True
                    launch.success_ns = now_ns
                    continue
                if kind not in {"BUY", "PUMPSWAP_BUY"}:
                    continue
                sol = max(0.0, finite(row.get("sol_amount")))
                launch.buy_count += 1
                state["buy_slots"][slot] += 1
                launch.same_slot_buys = integer(state["buy_slots"].get(launch.create_slot, 0))
                price = finite(row.get("price_sol"))
                fdv = finite(row.get("fdv_usd"))
                if price > 0:
                    state["price"] = price
                if fdv > 0:
                    launch.fdv_usd = fdv
                if trader == launch.creator:
                    launch.seed_sol += sol
                elif trader:
                    state["buyers"].add(trader)
                    launch.outside_sol += sol
                    if trader not in state["first_buyers"] and len(state["first_buyers"]) < 8:
                        state["first_buyers"].append(trader)
                launch.unique_buyers = len(state["buyers"])
                launch.first_buyers = tuple(state["first_buyers"])
                initial = finite(state.get("initial_price"))
                current = finite(state.get("price"))
                launch.price_multiple = current / initial if initial > 0 and current > 0 else 1.0
                if not launch.first_snapshot_ns:
                    launch.first_snapshot_ns = now_ns
        print(json.dumps({"run": run, "launches": sum(row.run_index == run_index for row in launches.values())}), flush=True)
    return launches


class MetadataFetcher:
    def __init__(self, concurrency: int, timeout: float) -> None:
        self.sem = asyncio.Semaphore(concurrency)
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "MetadataFetcher":
        connector = aiohttp.TCPConnector(limit=128, ttl_dns_cache=600, keepalive_timeout=45)
        self.session = aiohttp.ClientSession(timeout=self.timeout, connector=connector)
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self.session is not None:
            await self.session.close()

    async def one(self, uri: str) -> tuple[Mapping[str, Any] | None, str]:
        assert self.session is not None
        async with self.sem:
            last = ""
            for url in metadata_url(uri):
                try:
                    async with self.session.get(url, headers={"accept": "application/json"}) as response:
                        if response.status >= 400:
                            last = f"HTTP {response.status}"
                            continue
                        payload = await response.json(content_type=None)
                        if isinstance(payload, Mapping):
                            return payload, ""
                except Exception as exc:
                    last = f"{type(exc).__name__}: {exc}"
            return None, last or "no metadata URL"


async def fetch_all(launches: list[Launch], concurrency: int, timeout: float) -> dict[str, dict[str, Any]]:
    by_uri: dict[str, list[Launch]] = defaultdict(list)
    for launch in launches:
        by_uri[launch.uri].append(launch)
    uris = list(by_uri)
    output: dict[str, dict[str, Any]] = {}
    async with MetadataFetcher(concurrency, timeout) as fetcher:
        for start in range(0, len(uris), 400):
            chunk = uris[start : start + 400]
            results = await asyncio.gather(*(fetcher.one(uri) for uri in chunk))
            for uri, (metadata, error) in zip(chunk, results):
                for launch in by_uri[uri]:
                    output[launch.mint] = {
                        "metadata_ok": bool(metadata),
                        "metadata_error": error,
                        **social_fields(metadata, launch.create_ns),
                    }
            print(json.dumps({"metadata": min(len(uris), start + len(chunk)), "target": len(uris)}), flush=True)
    return output


def identity_flags(row: Mapping[str, Any]) -> dict[str, bool]:
    return {
        "NONE": False,
        "ANY": bool(row.get("hist_wins") or row.get("prior_creator_attempts") or row.get("prior_handle_attempts")),
        "STRONG": bool(row.get("elite_creator") or row.get("prior_creator_attempts", 0) >= 2 or row.get("prior_handle_attempts", 0) >= 2),
        "ELITE": bool(row.get("elite_creator")),
        "KNOWN_HANDLE": bool(row.get("known_handle")),
        "ELITE_OR_HANDLE": bool(row.get("elite_creator") or row.get("known_handle")),
    }


def triggers(row: Mapping[str, Any], rule: Rule) -> bool:
    age = row.get("tweet_age_seconds")
    if age is None:
        return False
    age = finite(age, float("inf"))
    if age < -5 or row.get("creator_seed_sol", 0.0) < rule.seed_sol:
        return False
    if row.get("mayhem") or row.get("fdv_usd", 0.0) <= 0 or row.get("fdv_usd", 0.0) > rule.max_fdv:
        return False
    if rule.require_cashback and not row.get("cashback"):
        return False
    strict = age <= rule.strict_seconds
    loose = age <= rule.loose_seconds and identity_flags(row).get(rule.identity, False)
    return strict or loose


def evaluate(rows: list[dict[str, Any]], rule: Rule) -> dict[str, Any]:
    selected = [row for row in rows if triggers(row, rule)]
    positives = [row for row in rows if row["positive"]]
    true = [row for row in selected if row["positive"]]
    return {
        "launches": len(rows),
        "positives": len(positives),
        "candidates": len(selected),
        "true": len(true),
        "false_positives": len(selected) - len(true),
        "precision": len(true) / len(selected) if selected else 0.0,
        "recall": len(true) / len(positives) if positives else 0.0,
        "successes": sum(row["label"] == "SUCCESS" for row in selected),
        "failed_attempts": sum(row["label"] == "FAILED_ATTEMPT" for row in selected),
    }


def search_rules(train: list[dict[str, Any]]) -> tuple[Rule, dict[str, Any]]:
    ranked = []
    for values in itertools.product(
        (0.5, 1.0, 2.0, 5.0, 10.0),
        (10.0, 30.0, 60.0, 120.0, 300.0, 1800.0),
        (0.25, 0.5, 1.0, 2.0, 3.0),
        ("NONE", "ANY", "STRONG", "ELITE", "KNOWN_HANDLE", "ELITE_OR_HANDLE"),
        (False, True),
        (7000.0, 8500.0, 10_000.0),
    ):
        rule = Rule(*values)
        if rule.loose_seconds < rule.strict_seconds:
            continue
        metrics = evaluate(train, rule)
        valid = metrics["recall"] >= 0.20 and metrics["true"] >= 20
        objective = (
            1 if valid else 0,
            metrics["precision"],
            metrics["recall"],
            -metrics["false_positives"],
        )
        ranked.append((objective, rule, metrics))
    ranked.sort(key=lambda row: row[0], reverse=True)
    _, rule, metrics = ranked[0]
    return rule, metrics


def cohort(rows: list[dict[str, Any]], label: str) -> dict[str, Any]:
    subset = [row for row in rows if row["label"] == label]
    ages = [finite(row["tweet_age_seconds"]) for row in subset if row.get("tweet_age_seconds") is not None and -5 <= finite(row["tweet_age_seconds"]) <= 1800]
    return {
        "launches": len(subset),
        "metadata_resolved": sum(row["metadata_ok"] for row in subset),
        "recent_status_30m": len(ages),
        "recent_status_rate": len(ages) / len(subset) if subset else 0.0,
        "median_tweet_age_seconds": median(ages),
        "median_creator_seed_sol": median([row["creator_seed_sol"] for row in subset]),
        "elite_creator_rate": sum(row["elite_creator"] for row in subset) / len(subset) if subset else 0.0,
        "known_handle_rate": sum(row["known_handle"] for row in subset) / len(subset) if subset else 0.0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Global causal E4 successful/failed-attempt intent autopsy")
    parser.add_argument("--pair", action="append", default=[], metavar="BATCH:EVENTS")
    parser.add_argument("--attempts", type=Path, required=True)
    parser.add_argument("--history", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--metadata-concurrency", type=int, default=96)
    parser.add_argument("--metadata-timeout", type=float, default=5.0)
    args = parser.parse_args()

    pairs = parse_pairs(args.pair)
    launches = load_launches(pairs)
    failures = failed_attempts(args.attempts)
    history = history_map(args.history)
    success_mints = {mint for mint, launch in launches.items() if launch.successful}
    failed_mints = set(failures) & set(launches)
    positive_mints = success_mints | failed_mints

    plausible = {
        mint
        for mint, launch in launches.items()
        if launch.seed_sol >= 0.25
        and 2_800 <= launch.fdv_usd <= 10_000
        and not launch.mayhem
    }
    contemporaneous: set[str] = set()
    by_run: dict[int, list[Launch]] = defaultdict(list)
    for launch in launches.values():
        by_run[launch.run_index].append(launch)
    for rows in by_run.values():
        rows.sort(key=lambda row: row.create_ns)
    for mint in positive_mints:
        target = launches[mint]
        for launch in by_run[target.run_index]:
            if abs(launch.create_ns - target.create_ns) <= 2_000_000_000:
                contemporaneous.add(launch.mint)
    research_mints = plausible | positive_mints | contemporaneous
    research_launches = [launches[mint] for mint in research_mints]
    metadata = asyncio.run(fetch_all(research_launches, args.metadata_concurrency, args.metadata_timeout))

    rows: list[dict[str, Any]] = []
    for launch in research_launches:
        label = "SUCCESS" if launch.mint in success_mints else "FAILED_ATTEMPT" if launch.mint in failed_mints else "IGNORED"
        hist = history.get(launch.creator, {})
        meta = metadata.get(launch.mint, {})
        rows.append({
            "mint": launch.mint,
            "run": launch.run,
            "run_index": launch.run_index,
            "label": label,
            "positive": label != "IGNORED",
            "creator": launch.creator,
            "create_ns": launch.create_ns,
            "create_slot": launch.create_slot,
            "creator_seed_sol": launch.seed_sol,
            "fdv_usd": launch.fdv_usd,
            "price_multiple": launch.price_multiple,
            "buy_count": launch.buy_count,
            "unique_buyers": launch.unique_buyers,
            "outside_sol": launch.outside_sol,
            "same_slot_buys": launch.same_slot_buys,
            "mayhem": launch.mayhem,
            "cashback": launch.cashback,
            "metadata_host": launch.host,
            "uri": launch.uri,
            "first_buyers": list(launch.first_buyers),
            "hist_wins": integer(hist.get("wins")),
            "hist_losses": integer(hist.get("losses")),
            "hist_trades": integer(hist.get("trades")),
            "hist_rate": finite(hist.get("rate")),
            "elite_creator": bool(integer(hist.get("trades")) >= 5 and integer(hist.get("wins")) >= 4 and finite(hist.get("rate")) >= 0.80),
            **meta,
        })

    rows.sort(key=lambda row: (row["run_index"], row["create_slot"], row["create_ns"]))
    train_cut = max(row["run_index"] for row in rows) - 3
    train_rows = [row for row in rows if row["run_index"] < train_cut]
    handle_counts: Counter[str] = Counter(row["twitter_handle"] for row in train_rows if row["positive"] and row.get("twitter_handle"))
    handle_total: Counter[str] = Counter(row["twitter_handle"] for row in train_rows if row.get("twitter_handle"))
    known_handles = {
        handle for handle, count in handle_counts.items()
        if count >= 2 and count / max(1, handle_total[handle]) >= 0.60
    }
    prior_creator: Counter[str] = Counter()
    prior_handle: Counter[str] = Counter()
    for row in rows:
        row["prior_creator_attempts"] = prior_creator[row["creator"]]
        row["prior_handle_attempts"] = prior_handle[row.get("twitter_handle") or ""]
        row["known_handle"] = bool(row.get("twitter_handle") in known_handles)
        if row["positive"]:
            prior_creator[row["creator"]] += 1
            if row.get("twitter_handle"):
                prior_handle[row["twitter_handle"]] += 1

    train = [row for row in rows if row["run_index"] < train_cut]
    holdout = [row for row in rows if row["run_index"] >= train_cut]
    rule, train_metrics = search_rules(train)
    holdout_metrics = evaluate(holdout, rule)

    simultaneous = []
    ignored = [row for row in rows if row["label"] == "IGNORED"]
    ignored_by_run: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in ignored:
        ignored_by_run[row["run_index"]].append(row)
    for target in (row for row in rows if row["positive"]):
        controls = [
            row for row in ignored_by_run[target["run_index"]]
            if abs(integer(row["create_ns"]) - integer(target["create_ns"])) <= 500_000_000
        ]
        if controls:
            simultaneous.append({"target": target, "controls": controls})

    def means(groups: list[dict[str, Any]], side: str, feature: str) -> float | None:
        values = []
        for group in groups:
            source = [group["target"]] if side == "target" else group["controls"]
            values.extend(finite(row.get(feature)) for row in source)
        return sum(values) / len(values) if values else None

    features = ["creator_seed_sol", "fdv_usd", "buy_count", "unique_buyers", "outside_sol", "same_slot_buys", "hist_wins", "prior_creator_attempts", "prior_handle_attempts"]
    payload = {
        "version": "e4-v12-global-attempt-intent-v1",
        "coverage": {
            "captured_launches": len(launches),
            "successful_entries": len(success_mints),
            "mapped_failed_attempts": len(failed_mints),
            "selected_or_attempted": len(positive_mints),
            "plausible_envelope": len(plausible),
            "research_launches": len(rows),
            "metadata_resolved": sum(row["metadata_ok"] for row in rows),
        },
        "cohorts": {label: cohort(rows, label) for label in ("SUCCESS", "FAILED_ATTEMPT", "IGNORED")},
        "known_handles_train_only": sorted(known_handles),
        "rule": rule.as_dict(),
        "train": train_metrics,
        "holdout": holdout_metrics,
        "safe_for_full_size": False,
        "safe_for_preimpact_probe": bool(
            holdout_metrics["precision"] >= 0.60
            and holdout_metrics["recall"] >= 0.20
            and holdout_metrics["true"] >= 20
        ),
        "simultaneous_500ms": {
            "positive_choices_with_controls": len(simultaneous),
            "control_rows": sum(len(group["controls"]) for group in simultaneous),
            "feature_means": {
                feature: {"selected_or_attempted": means(simultaneous, "target", feature), "true_ignored": means(simultaneous, "control", feature)}
                for feature in features
            },
            "recent_status_rate": {
                "selected_or_attempted": sum(
                    1 for group in simultaneous
                    if group["target"].get("tweet_age_seconds") is not None and -5 <= finite(group["target"]["tweet_age_seconds"]) <= 60
                ) / len(simultaneous) if simultaneous else 0.0,
                "true_ignored": sum(
                    1 for group in simultaneous for row in group["controls"]
                    if row.get("tweet_age_seconds") is not None and -5 <= finite(row["tweet_age_seconds"]) <= 60
                ) / max(1, sum(len(group["controls"]) for group in simultaneous)),
            },
        },
        "rows": rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({key: payload[key] for key in ("coverage", "cohorts", "rule", "train", "holdout", "safe_for_preimpact_probe", "simultaneous_500ms")}, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
