#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import bisect
import json
import math
import re
import statistics
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlparse

import aiohttp
import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import precision_recall_fscore_support
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

E4_WALLET = "E4EzXdwf7NNdqM2XGswWaWHfxgucVCo24PTCcrimTKBz"
X_EPOCH_MS = 1_288_834_974_657
STATUS_RE = re.compile(r"(?:x|twitter)\.com/([^/?#]+)/status(?:es)?/(\d+)", re.I)
HANDLE_RE = re.compile(r"(?:x|twitter)\.com/([^/?#]+)", re.I)
BUY_KINDS = {"BUY", "PUMPSWAP_BUY"}
SELL_KINDS = {"SELL", "PUMPSWAP_SELL"}


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


def log1p(value: Any) -> float:
    return math.log1p(max(0.0, finite(value)))


def median(values: Iterable[float]) -> float | None:
    rows = [float(value) for value in values if math.isfinite(float(value))]
    return statistics.median(rows) if rows else None


def wilson_lower(successes: int, total: int, z: float = 1.96) -> float:
    if total <= 0:
        return 0.0
    p = successes / total
    denominator = 1.0 + z * z / total
    centre = p + z * z / (2.0 * total)
    spread = z * math.sqrt((p * (1.0 - p) + z * z / (4.0 * total)) / total)
    return max(0.0, (centre - spread) / denominator)


def parse_pair(value: str) -> tuple[Path, Path]:
    left, right = value.split(":", 1)
    return Path(left), Path(right)


def tx_index(row: Mapping[str, Any]) -> int:
    raw = row.get("raw") if isinstance(row.get("raw"), Mapping) else {}
    for key in ("transaction_index", "transactionIndex", "tx_index", "txIndex"):
        if key in row and row.get(key) is not None:
            return integer(row.get(key), -1)
        if key in raw and raw.get(key) is not None:
            return integer(raw.get(key), -1)
    return -1


def event_key(row: Mapping[str, Any]) -> tuple[int, int, int, int]:
    slot = integer(row.get("slot"), -1)
    index = tx_index(row)
    event_index = integer(row.get("event_index"), 0)
    received = integer(row.get("received_ns"), 0)
    return slot, index if index >= 0 else 1_000_000, event_index, received


def metadata_urls(uri: str) -> list[str]:
    text = str(uri or "").strip()
    if not text:
        return []
    if text.startswith("ipfs://"):
        key = text[7:].lstrip("/")
        return [f"https://ipfs.io/ipfs/{key}", f"https://gateway.pinata.cloud/ipfs/{key}"]
    if text.startswith("ar://"):
        return [f"https://arweave.net/{text[5:].lstrip('/')}"]
    output = [text]
    if "/ipfs/" in text:
        key = text.split("/ipfs/", 1)[1]
        output.extend([f"https://ipfs.io/ipfs/{key}", f"https://gateway.pinata.cloud/ipfs/{key}"])
    return list(dict.fromkeys(output))


def walk_strings(value: Any) -> list[str]:
    output: list[str] = []
    if isinstance(value, str):
        output.append(value)
    elif isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).lower() in {
                "twitter", "x", "social", "socials", "website", "description", "telegram", "created_by"
            }:
                output.extend(walk_strings(item))
            elif isinstance(item, (Mapping, list, tuple)):
                output.extend(walk_strings(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            output.extend(walk_strings(item))
    return output


def social_fields(metadata: Mapping[str, Any] | None, create_ns: int) -> dict[str, Any]:
    values = walk_strings(metadata or {})
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
    tweet_age = None
    if status_id:
        tweet_ms = (int(status_id) >> 22) + X_EPOCH_MS
        tweet_age = create_ns / 1e6 / 1000.0 - tweet_ms / 1000.0
    return {
        "metadata_ok": bool(metadata),
        "twitter": twitter,
        "twitter_handle": handle,
        "twitter_status_id": status_id,
        "tweet_age_seconds": tweet_age,
        "website_present": any("http" in value.lower() and "twitter.com" not in value.lower() and "x.com" not in value.lower() for value in values),
    }


def scan_metadata_cache(paths: Sequence[Path]) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}

    def walk(value: Any) -> None:
        if isinstance(value, Mapping):
            mint = str(value.get("mint") or "")
            if mint and any(key in value for key in ("twitter_handle", "tweet_age_seconds", "metadata_ok", "twitter_status_id")):
                output[mint] = {
                    "metadata_ok": bool(value.get("metadata_ok")),
                    "twitter": str(value.get("twitter") or ""),
                    "twitter_handle": str(value.get("twitter_handle") or "").lower().lstrip("@"),
                    "twitter_status_id": str(value.get("twitter_status_id") or ""),
                    "tweet_age_seconds": value.get("tweet_age_seconds"),
                    "website_present": bool(value.get("website_present")),
                }
            for item in value.values():
                if isinstance(item, (Mapping, list)):
                    walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    for root in paths:
        candidates = [root] if root.is_file() else list(root.rglob("*.json")) if root.exists() else []
        for path in candidates:
            try:
                walk(json.loads(path.read_text(encoding="utf-8")))
            except Exception:
                continue
    return output


class MetadataFetcher:
    def __init__(self, concurrency: int, timeout: float) -> None:
        self.sem = asyncio.Semaphore(max(1, concurrency))
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "MetadataFetcher":
        connector = aiohttp.TCPConnector(limit=128, ttl_dns_cache=600, keepalive_timeout=45)
        self.session = aiohttp.ClientSession(timeout=self.timeout, connector=connector)
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self.session is not None:
            await self.session.close()

    async def one(self, uri: str) -> Mapping[str, Any] | None:
        assert self.session is not None
        async with self.sem:
            for url in metadata_urls(uri):
                try:
                    async with self.session.get(url, headers={"accept": "application/json"}) as response:
                        if response.status >= 400:
                            continue
                        payload = await response.json(content_type=None)
                        if isinstance(payload, Mapping):
                            return payload
                except Exception:
                    continue
        return None


async def fill_metadata(
    launches: Mapping[str, "Launch"],
    cache: dict[str, dict[str, Any]],
    concurrency: int,
    timeout: float,
) -> dict[str, dict[str, Any]]:
    missing_by_uri: dict[str, list[Launch]] = defaultdict(list)
    for launch in launches.values():
        if launch.mint not in cache and launch.uri:
            missing_by_uri[launch.uri].append(launch)
    uris = list(missing_by_uri)
    if not uris:
        return cache
    async with MetadataFetcher(concurrency, timeout) as fetcher:
        for start in range(0, len(uris), 300):
            chunk = uris[start : start + 300]
            values = await asyncio.gather(*(fetcher.one(uri) for uri in chunk))
            for uri, metadata in zip(chunk, values):
                for launch in missing_by_uri[uri]:
                    cache[launch.mint] = social_fields(metadata, launch.create_ns)
            print(json.dumps({"metadata": min(len(uris), start + len(chunk)), "target": len(uris)}), flush=True)
    return cache


def load_creator_history(path: Path) -> dict[str, dict[str, float]]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("top_creators") or data.get("creators") or []
    if isinstance(rows, Mapping):
        rows = [{"creator": key, **(value if isinstance(value, Mapping) else {})} for key, value in rows.items()]
    output = {}
    for row in rows:
        creator = str(row.get("creator") or "")
        if not creator:
            continue
        wins = integer(row.get("wins") or row.get("e4_observed_wins"))
        losses = integer(row.get("losses") or row.get("e4_observed_losses"))
        trades = max(integer(row.get("trades")), wins + losses)
        output[creator] = {
            "wins": float(wins),
            "losses": float(losses),
            "trades": float(trades),
            "rate": finite(row.get("gross_win_rate"), wins / trades if trades else 0.0),
        }
    return output


def load_failed_attempts(paths: Sequence[Path]) -> dict[str, list[dict[str, Any]]]:
    by_mint: dict[str, list[dict[str, Any]]] = defaultdict(list)

    def inspect(value: Any) -> None:
        if not isinstance(value, Mapping):
            return
        failed = value.get("failed_attempts")
        rows = failed.get("rows") if isinstance(failed, Mapping) else None
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, Mapping):
                    continue
                mint = str(row.get("mapped_mint") or row.get("mint") or "")
                if mint and bool(row.get("mapping_ok", True)):
                    by_mint[mint].append(dict(row))
        for item in value.values():
            if isinstance(item, Mapping):
                inspect(item)

    for root in paths:
        candidates = [root] if root.is_file() else list(root.rglob("*.json")) if root.exists() else []
        for path in candidates:
            try:
                inspect(json.loads(path.read_text(encoding="utf-8")))
            except Exception:
                continue
    for rows in by_mint.values():
        rows.sort(key=lambda row: (integer(row.get("attempt_slot"), 2**63 - 1), integer(row.get("attempt_transaction_index"), 2**31 - 1)))
    return by_mint


@dataclass
class Launch:
    mint: str
    run_index: int
    run_id: str
    creator: str
    create_ns: int
    create_slot: int
    create_signature: str
    uri: str
    metadata_host: str
    token_program: str
    mayhem: bool
    cashback: bool
    events: list[dict[str, Any]] = field(default_factory=list)
    success_event: dict[str, Any] | None = None
    failed_attempt: dict[str, Any] | None = None


@dataclass
class State:
    creator_seed_sol: float = 0.0
    outside_sol: float = 0.0
    buy_count: int = 0
    sell_count: int = 0
    unique_buyers: set[str] = field(default_factory=set)
    first_buyers: list[str] = field(default_factory=list)
    first_buyer_ns: list[int] = field(default_factory=list)
    same_slot_buys: int = 0
    same_slot_unique: set[str] = field(default_factory=set)
    buy_signatures: Counter[str] = field(default_factory=Counter)
    buy_slots: Counter[int] = field(default_factory=Counter)
    fdv_usd: float = 0.0
    price_sol: float = 0.0
    initial_price_sol: float = 0.0
    latest_ns: int = 0


def state_copy(state: State) -> State:
    return State(
        creator_seed_sol=state.creator_seed_sol,
        outside_sol=state.outside_sol,
        buy_count=state.buy_count,
        sell_count=state.sell_count,
        unique_buyers=set(state.unique_buyers),
        first_buyers=list(state.first_buyers),
        first_buyer_ns=list(state.first_buyer_ns),
        same_slot_buys=state.same_slot_buys,
        same_slot_unique=set(state.same_slot_unique),
        buy_signatures=Counter(state.buy_signatures),
        buy_slots=Counter(state.buy_slots),
        fdv_usd=state.fdv_usd,
        price_sol=state.price_sol,
        initial_price_sol=state.initial_price_sol,
        latest_ns=state.latest_ns,
    )


def apply_event(launch: Launch, state: State, row: Mapping[str, Any]) -> None:
    kind = str(row.get("kind") or "").upper()
    trader = str(row.get("trader") or "")
    received = integer(row.get("received_ns"))
    state.latest_ns = max(state.latest_ns, received)
    fdv = finite(row.get("fdv_usd"))
    price = finite(row.get("price_sol"))
    if fdv > 0:
        state.fdv_usd = fdv
    if price > 0:
        state.price_sol = price
        if state.initial_price_sol <= 0:
            state.initial_price_sol = price
    if kind in BUY_KINDS:
        if trader == E4_WALLET:
            return
        sol = max(0.0, finite(row.get("sol_amount")))
        state.buy_count += 1
        signature = str(row.get("signature") or "")
        slot = integer(row.get("slot"))
        state.buy_signatures[signature] += 1
        state.buy_slots[slot] += 1
        if slot == launch.create_slot:
            state.same_slot_buys += 1
        if trader == launch.creator:
            state.creator_seed_sol += sol
        elif trader:
            state.outside_sol += sol
            if trader not in state.unique_buyers:
                state.unique_buyers.add(trader)
                if len(state.first_buyers) < 12:
                    state.first_buyers.append(trader)
                    state.first_buyer_ns.append(received)
            if slot == launch.create_slot:
                state.same_slot_unique.add(trader)
    elif kind in SELL_KINDS:
        if trader != E4_WALLET:
            state.sell_count += 1


def load_launches(pairs: Sequence[tuple[Path, Path]]) -> tuple[dict[str, Launch], list[str]]:
    launches: dict[str, Launch] = {}
    run_ids: list[str] = []
    for run_index, (batch_path, events_path) in enumerate(pairs):
        run_id = batch_path.parent.parent.name if batch_path.parent.name == "artifacts" else batch_path.stem
        run_ids.append(run_id)
        with events_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                row = json.loads(line)
                mint = str(row.get("mint") or "")
                kind = str(row.get("kind") or "").upper()
                raw = row.get("raw") if isinstance(row.get("raw"), Mapping) else {}
                if kind == "CREATE" and mint not in launches:
                    creator = str(row.get("creator") or raw.get("creator") or row.get("trader") or "")
                    uri = str(raw.get("uri") or "")
                    launches[mint] = Launch(
                        mint=mint,
                        run_index=run_index,
                        run_id=run_id,
                        creator=creator,
                        create_ns=integer(row.get("received_ns")),
                        create_slot=integer(row.get("slot")),
                        create_signature=str(row.get("signature") or ""),
                        uri=uri,
                        metadata_host=(urlparse(uri).netloc or "").lower(),
                        token_program=str(raw.get("token_program") or ""),
                        mayhem=bool(raw.get("is_mayhem_mode")),
                        cashback=bool(raw.get("is_cashback_enabled")),
                    )
                launch = launches.get(mint)
                if launch is None:
                    continue
                launch.events.append(dict(row))
                if str(row.get("trader") or "") == E4_WALLET and kind in BUY_KINDS and launch.success_event is None:
                    launch.success_event = dict(row)
        for launch in (row for row in launches.values() if row.run_index == run_index):
            launch.events.sort(key=event_key)
        print(json.dumps({"run": run_id, "launches": sum(row.run_index == run_index for row in launches.values())}), flush=True)
    return launches, run_ids


def estimate_failed_marker(launch: Launch, attempt: Mapping[str, Any]) -> tuple[tuple[int, int, int, int], int]:
    slot = integer(attempt.get("attempt_slot"), launch.create_slot)
    index = integer(attempt.get("attempt_transaction_index"), -1)
    same_slot_times = [integer(row.get("received_ns")) for row in launch.events if integer(row.get("slot")) == slot]
    if same_slot_times:
        estimate = min(same_slot_times)
    else:
        estimate = launch.create_ns + max(0, slot - launch.create_slot) * 400_000_000
    key = (slot, index if index >= 0 else 1_000_000, -1, estimate)
    return key, estimate


def marker_for(launch: Launch) -> tuple[str, tuple[int, int, int, int], int] | None:
    choices: list[tuple[str, tuple[int, int, int, int], int]] = []
    if launch.success_event is not None:
        choices.append(("SUCCESS", event_key(launch.success_event), integer(launch.success_event.get("received_ns"))))
    if launch.failed_attempt is not None:
        key, estimate = estimate_failed_marker(launch, launch.failed_attempt)
        choices.append(("FAILED_ATTEMPT", key, estimate))
    return min(choices, key=lambda row: row[1]) if choices else None


def snapshot_dict(launch: Launch, state: State, timestamp_ns: int, label: str, stage: str) -> dict[str, Any]:
    age_ms = max(0.0, (timestamp_ns - launch.create_ns) / 1e6)
    price_multiple = state.price_sol / state.initial_price_sol if state.price_sol > 0 and state.initial_price_sol > 0 else 1.0
    first_age = (state.first_buyer_ns[0] - launch.create_ns) / 1e6 if state.first_buyer_ns else 9999.0
    second_age = (state.first_buyer_ns[1] - launch.create_ns) / 1e6 if len(state.first_buyer_ns) > 1 else 9999.0
    interbuy = (
        statistics.median((b - a) / 1e6 for a, b in zip(state.first_buyer_ns, state.first_buyer_ns[1:]))
        if len(state.first_buyer_ns) > 1 else 9999.0
    )
    return {
        "mint": launch.mint,
        "run_index": launch.run_index,
        "run_id": launch.run_id,
        "label": label,
        "positive": label != "IGNORED",
        "stage": stage,
        "timestamp_ns": timestamp_ns,
        "creator": launch.creator,
        "create_ns": launch.create_ns,
        "create_slot": launch.create_slot,
        "creator_seed_sol": state.creator_seed_sol,
        "outside_sol": state.outside_sol,
        "buy_count": state.buy_count,
        "sell_count": state.sell_count,
        "unique_buyers": len(state.unique_buyers),
        "same_slot_buys": state.same_slot_buys,
        "same_slot_unique": len(state.same_slot_unique),
        "first_buyers": list(state.first_buyers),
        "first_buyer_age_ms": first_age,
        "second_buyer_age_ms": second_age,
        "median_interbuyer_ms": interbuy,
        "distinct_buy_signatures": len([key for key in state.buy_signatures if key]),
        "max_buys_one_signature": max(state.buy_signatures.values(), default=0),
        "max_buys_one_slot": max(state.buy_slots.values(), default=0),
        "create_signature_buys": integer(state.buy_signatures.get(launch.create_signature, 0)),
        "fdv_usd": state.fdv_usd,
        "price_multiple": price_multiple,
        "age_ms": age_ms,
        "seed_share": state.creator_seed_sol / max(1e-9, state.creator_seed_sol + state.outside_sol),
        "metadata_host": launch.metadata_host,
        "token_program": launch.token_program,
        "mayhem": launch.mayhem,
        "cashback": launch.cashback,
    }


def build_snapshots(launches: Mapping[str, Launch], failed: Mapping[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for launch in launches.values():
        if failed.get(launch.mint):
            launch.failed_attempt = failed[launch.mint][0]
        marker = marker_for(launch)
        marker_key = marker[1] if marker else None
        marker_label = marker[0] if marker else "IGNORED"
        marker_ns = marker[2] if marker else 0
        state = State(latest_ns=launch.create_ns)
        positive_emitted = False
        ignored_candidates: list[dict[str, Any]] = []

        for row in launch.events:
            key = event_key(row)
            if marker_key is not None and not positive_emitted and key >= marker_key:
                rows.append(snapshot_dict(launch, state_copy(state), marker_ns or state.latest_ns, marker_label, "PRE_INTENT"))
                positive_emitted = True
                break
            apply_event(launch, state, row)
            age_ms = max(0.0, (state.latest_ns - launch.create_ns) / 1e6)
            if marker_key is None and state.sell_count == 0 and age_ms <= 1500.0 and state.fdv_usd > 0:
                if state.creator_seed_sol >= 0.20 or state.buy_count > 0:
                    ignored_candidates.append(snapshot_dict(launch, state_copy(state), state.latest_ns, "IGNORED", f"BUY_{min(8, state.buy_count)}"))

        if marker_key is not None and not positive_emitted:
            rows.append(snapshot_dict(launch, state_copy(state), marker_ns or state.latest_ns, marker_label, "PRE_INTENT"))
        elif marker_key is None and ignored_candidates:
            # Keep the most dangerous pre-entry stage per ignored launch: the
            # stage with the strongest seed, buyer and same-slot shape.
            ignored_candidates.sort(
                key=lambda row: (
                    2.5 * log1p(row["creator_seed_sol"])
                    + 1.0 * row["unique_buyers"]
                    + 0.75 * row["same_slot_buys"]
                    + 0.25 * log1p(row["outside_sol"])
                    - 0.001 * row["age_ms"]
                ),
                reverse=True,
            )
            rows.append(ignored_candidates[0])
    return rows


def add_metadata(rows: list[dict[str, Any]], metadata: Mapping[str, Mapping[str, Any]]) -> None:
    for row in rows:
        meta = metadata.get(str(row["mint"]), {})
        row.update({
            "metadata_ok": bool(meta.get("metadata_ok")),
            "twitter_handle": str(meta.get("twitter_handle") or "").lower().lstrip("@"),
            "twitter_status_id": str(meta.get("twitter_status_id") or ""),
            "tweet_age_seconds": meta.get("tweet_age_seconds"),
            "website_present": bool(meta.get("website_present")),
        })


def add_history(rows: list[dict[str, Any]], static_history: Mapping[str, Mapping[str, float]]) -> None:
    creator_attempts: Counter[str] = Counter()
    creator_successes: Counter[str] = Counter()
    creator_failures: Counter[str] = Counter()
    handle_attempts: Counter[str] = Counter()
    buyer_attempts: Counter[str] = Counter()
    buyer_successes: Counter[str] = Counter()
    creator_buyer_attempts: Counter[tuple[str, str]] = Counter()

    positives = sorted((row for row in rows if row["positive"]), key=lambda row: (integer(row["timestamp_ns"]), row["mint"]))
    pointer = 0
    ordered = sorted(rows, key=lambda row: (integer(row["timestamp_ns"]), 0 if not row["positive"] else 1, row["mint"]))

    def apply_positive(item: Mapping[str, Any]) -> None:
        creator = str(item.get("creator") or "")
        handle = str(item.get("twitter_handle") or "")
        if creator:
            creator_attempts[creator] += 1
            creator_successes[creator] += int(item.get("label") == "SUCCESS")
            creator_failures[creator] += int(item.get("label") == "FAILED_ATTEMPT")
        if handle:
            handle_attempts[handle] += 1
        for buyer in item.get("first_buyers") or []:
            buyer_attempts[str(buyer)] += 1
            buyer_successes[str(buyer)] += int(item.get("label") == "SUCCESS")
            if creator:
                creator_buyer_attempts[(creator, str(buyer))] += 1

    for row in ordered:
        timestamp = integer(row["timestamp_ns"])
        while pointer < len(positives) and integer(positives[pointer]["timestamp_ns"]) < timestamp:
            apply_positive(positives[pointer])
            pointer += 1
        creator = str(row.get("creator") or "")
        handle = str(row.get("twitter_handle") or "")
        static = static_history.get(creator, {})
        buyers = [str(value) for value in row.get("first_buyers") or []]
        buyer_counts = [buyer_attempts[value] for value in buyers]
        buyer_success = [buyer_successes[value] for value in buyers]
        pair_counts = [creator_buyer_attempts[(creator, value)] for value in buyers]
        row.update({
            "hist_wins": finite(static.get("wins")),
            "hist_losses": finite(static.get("losses")),
            "hist_trades": finite(static.get("trades")),
            "hist_rate": finite(static.get("rate")),
            "prior_creator_attempts": creator_attempts[creator],
            "prior_creator_successes": creator_successes[creator],
            "prior_creator_failures": creator_failures[creator],
            "prior_handle_attempts": handle_attempts[handle] if handle else 0,
            "known_buyer_count": sum(value > 0 for value in buyer_counts),
            "max_prior_buyer_attempts": max(buyer_counts, default=0),
            "sum_prior_buyer_attempts": sum(buyer_counts),
            "max_prior_buyer_successes": max(buyer_success, default=0),
            "sum_prior_buyer_successes": sum(buyer_success),
            "max_creator_buyer_pair_attempts": max(pair_counts, default=0),
        })
        if row["positive"]:
            # Equal-timestamp decisions do not leak into one another. The row
            # becomes historical authority only after its own decision point.
            while pointer < len(positives) and positives[pointer] is not row and integer(positives[pointer]["timestamp_ns"]) == timestamp:
                break
            apply_positive(row)
            if pointer < len(positives) and positives[pointer] is row:
                pointer += 1


def add_competition(rows: list[dict[str, Any]], window_ms: float = 500.0) -> None:
    by_run: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_run[integer(row["run_index"])].append(row)
    window_ns = int(window_ms * 1e6)
    for run_rows in by_run.values():
        run_rows.sort(key=lambda row: integer(row["timestamp_ns"]))
        queue: deque[dict[str, Any]] = deque()
        for row in run_rows:
            now = integer(row["timestamp_ns"])
            while queue and integer(queue[0]["timestamp_ns"]) < now - window_ns:
                queue.popleft()
            row["visible_competitors_500ms"] = len(queue)
            row["max_competitor_seed_500ms"] = max((finite(item["creator_seed_sol"]) for item in queue), default=0.0)
            row["max_competitor_buyers_500ms"] = max((integer(item["unique_buyers"]) for item in queue), default=0)
            queue.append(row)


AUTHORITY_FEATURES = [
    "log_seed", "log_fdv", "age_100ms", "hist_wins_log", "hist_rate", "prior_creator_log",
    "prior_creator_success_log", "prior_handle_log", "status_present", "fresh_status_1s",
    "fresh_status_5s", "fresh_status_30s", "fresh_status_120s", "tweet_age_log",
    "cashback", "website_present", "create_signature_buys", "seed_share", "visible_competitors_log",
]

CLUSTER_FEATURES = [
    "log_seed", "log_outside", "log_fdv", "age_100ms", "buy_count", "unique_buyers",
    "same_slot_buys", "same_slot_unique", "known_buyer_count", "max_prior_buyer_log",
    "sum_prior_buyer_log", "max_prior_buyer_success_log", "sum_prior_buyer_success_log",
    "max_pair_log", "prior_creator_log", "prior_creator_success_log", "seed_share",
    "first_buyer_age_100ms", "second_buyer_age_100ms", "interbuyer_100ms",
    "distinct_buy_signatures", "max_buys_one_signature", "max_buys_one_slot",
    "create_signature_buys", "price_multiple_clip", "visible_competitors_log",
]


def vector(row: Mapping[str, Any]) -> dict[str, float]:
    tweet_age = row.get("tweet_age_seconds")
    status_present = tweet_age is not None and math.isfinite(finite(tweet_age, float("nan")))
    age = finite(tweet_age, 10**9) if status_present else 10**9
    return {
        "log_seed": log1p(row.get("creator_seed_sol")),
        "log_outside": log1p(row.get("outside_sol")),
        "log_fdv": log1p(row.get("fdv_usd")),
        "age_100ms": min(20.0, finite(row.get("age_ms")) / 100.0),
        "hist_wins_log": log1p(row.get("hist_wins")),
        "hist_rate": finite(row.get("hist_rate")),
        "prior_creator_log": log1p(row.get("prior_creator_attempts")),
        "prior_creator_success_log": log1p(row.get("prior_creator_successes")),
        "prior_handle_log": log1p(row.get("prior_handle_attempts")),
        "status_present": float(status_present),
        "fresh_status_1s": float(status_present and -5 <= age <= 1),
        "fresh_status_5s": float(status_present and -5 <= age <= 5),
        "fresh_status_30s": float(status_present and -5 <= age <= 30),
        "fresh_status_120s": float(status_present and -5 <= age <= 120),
        "tweet_age_log": math.log1p(max(0.0, min(age, 86_400.0))) if status_present else math.log1p(86_400.0),
        "cashback": float(bool(row.get("cashback"))),
        "website_present": float(bool(row.get("website_present"))),
        "create_signature_buys": finite(row.get("create_signature_buys")),
        "seed_share": finite(row.get("seed_share")),
        "visible_competitors_log": log1p(row.get("visible_competitors_500ms")),
        "buy_count": finite(row.get("buy_count")),
        "unique_buyers": finite(row.get("unique_buyers")),
        "same_slot_buys": finite(row.get("same_slot_buys")),
        "same_slot_unique": finite(row.get("same_slot_unique")),
        "known_buyer_count": finite(row.get("known_buyer_count")),
        "max_prior_buyer_log": log1p(row.get("max_prior_buyer_attempts")),
        "sum_prior_buyer_log": log1p(row.get("sum_prior_buyer_attempts")),
        "max_prior_buyer_success_log": log1p(row.get("max_prior_buyer_successes")),
        "sum_prior_buyer_success_log": log1p(row.get("sum_prior_buyer_successes")),
        "max_pair_log": log1p(row.get("max_creator_buyer_pair_attempts")),
        "first_buyer_age_100ms": min(100.0, finite(row.get("first_buyer_age_ms"), 9999.0) / 100.0),
        "second_buyer_age_100ms": min(100.0, finite(row.get("second_buyer_age_ms"), 9999.0) / 100.0),
        "interbuyer_100ms": min(100.0, finite(row.get("median_interbuyer_ms"), 9999.0) / 100.0),
        "distinct_buy_signatures": finite(row.get("distinct_buy_signatures")),
        "max_buys_one_signature": finite(row.get("max_buys_one_signature")),
        "max_buys_one_slot": finite(row.get("max_buys_one_slot")),
        "price_multiple_clip": min(10.0, max(0.0, finite(row.get("price_multiple"), 1.0))),
    }


def eligible(row: Mapping[str, Any]) -> bool:
    return bool(
        not row.get("mayhem")
        and integer(row.get("sell_count")) == 0
        and 2_750.0 <= finite(row.get("fdv_usd")) <= 10_000.0
        and finite(row.get("creator_seed_sol")) >= 0.20
        and finite(row.get("age_ms")) <= 1_500.0
    )


def matrix(rows: Sequence[Mapping[str, Any]], names: Sequence[str]) -> np.ndarray:
    return np.asarray([[vector(row)[name] for name in names] for row in rows], dtype=float)


def balanced_rows(rows: list[dict[str, Any]], maximum_negative_ratio: int = 12) -> list[dict[str, Any]]:
    positives = [row for row in rows if row["positive"] and eligible(row)]
    negatives = [row for row in rows if not row["positive"] and eligible(row)]
    negatives.sort(
        key=lambda row: (
            log1p(row["creator_seed_sol"])
            + 0.6 * integer(row["unique_buyers"])
            + 0.5 * integer(row["same_slot_buys"])
            + 0.8 * log1p(row.get("prior_creator_attempts"))
            + 0.8 * log1p(row.get("sum_prior_buyer_attempts"))
        ),
        reverse=True,
    )
    return positives + negatives[: max(len(positives) * maximum_negative_ratio, 500)]


def fit(rows: list[dict[str, Any]], names: Sequence[str], c: float) -> Pipeline:
    chosen = balanced_rows(rows)
    if not chosen or len({bool(row["positive"]) for row in chosen}) < 2:
        raise RuntimeError("insufficient classes for model")
    model = Pipeline([
        ("scale", StandardScaler()),
        ("logit", LogisticRegression(C=c, class_weight="balanced", max_iter=4000, solver="liblinear")),
    ])
    model.fit(matrix(chosen, names), np.asarray([int(row["positive"]) for row in chosen]))
    return model


def score_rows(
    rows: list[dict[str, Any]],
    authority: Pipeline,
    cluster: Pipeline,
) -> list[dict[str, Any]]:
    output = [dict(row) for row in rows]
    eligible_indices = [index for index, row in enumerate(output) if eligible(row)]
    if not eligible_indices:
        return output
    authority_values = authority.predict_proba(matrix([output[index] for index in eligible_indices], AUTHORITY_FEATURES))[:, 1]
    cluster_values = cluster.predict_proba(matrix([output[index] for index in eligible_indices], CLUSTER_FEATURES))[:, 1]
    for index, a_value, c_value in zip(eligible_indices, authority_values, cluster_values):
        output[index]["authority_probability"] = float(a_value)
        output[index]["cluster_probability"] = float(c_value)
    return output


@dataclass(frozen=True)
class Gate:
    authority_threshold: float
    cluster_threshold: float
    minimum_margin: float
    competition_window_ms: float
    top_k: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "authority_threshold": self.authority_threshold,
            "cluster_threshold": self.cluster_threshold,
            "minimum_margin": self.minimum_margin,
            "competition_window_ms": self.competition_window_ms,
            "top_k": self.top_k,
        }


def predict_launches(scored: list[dict[str, Any]], gate: Gate) -> dict[str, dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for row in scored:
        authority = finite(row.get("authority_probability"), -1.0)
        cluster = finite(row.get("cluster_probability"), -1.0)
        if authority < gate.authority_threshold and cluster < gate.cluster_threshold:
            continue
        mode = "LAUNCH_AUTHORITY" if authority >= cluster else "WALLET_CLUSTER"
        probability = max(authority, cluster)
        item = dict(row)
        item["mode"] = mode
        item["probability"] = probability
        candidates.append(item)

    candidates.sort(key=lambda row: (integer(row["run_index"]), integer(row["timestamp_ns"]), -finite(row["probability"])))
    accepted: dict[str, dict[str, Any]] = {}
    active: dict[int, deque[dict[str, Any]]] = defaultdict(deque)
    window_ns = int(gate.competition_window_ms * 1e6)
    for row in candidates:
        run_index = integer(row["run_index"])
        now = integer(row["timestamp_ns"])
        queue = active[run_index]
        while queue and integer(queue[0]["timestamp_ns"]) < now - window_ns:
            queue.popleft()
        rivals = sorted([finite(item["probability"]) for item in queue] + [finite(row["probability"])], reverse=True)
        rank = 1 + sum(value > finite(row["probability"]) for value in rivals)
        second = rivals[1] if len(rivals) > 1 else 0.0
        margin = finite(row["probability"]) - second if rank == 1 else -1.0
        row["competition_rank"] = rank
        row["competition_margin"] = margin
        queue.append(row)
        if rank > gate.top_k:
            continue
        if rank == 1 and margin < gate.minimum_margin:
            continue
        mint = str(row["mint"])
        existing = accepted.get(mint)
        if existing is None or integer(row["timestamp_ns"]) < integer(existing["timestamp_ns"]):
            accepted[mint] = row
    return accepted


def metrics(rows: list[dict[str, Any]], predictions: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    by_mint = {str(row["mint"]): row for row in rows}
    positives = {mint for mint, row in by_mint.items() if row["positive"] and eligible(row)}
    predicted = set(predictions)
    true = predicted & positives
    false = predicted - positives
    success = {mint for mint in positives if by_mint[mint]["label"] == "SUCCESS"}
    failed = positives - success
    pre_intent = {
        mint for mint in true
        if integer(predictions[mint]["timestamp_ns"]) <= integer(by_mint[mint]["timestamp_ns"])
    }
    modes = Counter(str(predictions[mint].get("mode") or "") for mint in predicted)
    return {
        "launches": len(by_mint),
        "positives": len(positives),
        "successes": len(success),
        "failed_attempts": len(failed),
        "predictions": len(predicted),
        "true": len(true),
        "false_positives": len(false),
        "precision": len(true) / len(predicted) if predicted else 0.0,
        "precision_wilson_low": wilson_lower(len(true), len(predicted)),
        "recall": len(true) / len(positives) if positives else 0.0,
        "success_recall": len(predicted & success) / len(success) if success else 0.0,
        "failed_attempt_recall": len(predicted & failed) / len(failed) if failed else 0.0,
        "pre_intent_true": len(pre_intent),
        "all_true_pre_intent": len(pre_intent) == len(true),
        "modes": dict(modes),
        "true_mints": sorted(true),
        "false_positive_mints": sorted(false),
        "missed_positive_mints": sorted(positives - predicted),
    }


def tune(
    train_rows: list[dict[str, Any]],
    validation_rows: list[dict[str, Any]],
) -> tuple[Pipeline, Pipeline, Gate, dict[str, Any]]:
    best: tuple[tuple[float, ...], Pipeline, Pipeline, Gate, dict[str, Any]] | None = None
    for c in (0.08, 0.15, 0.3, 0.6, 1.0, 2.0):
        authority = fit(train_rows, AUTHORITY_FEATURES, c)
        cluster = fit(train_rows, CLUSTER_FEATURES, c)
        scored = score_rows(validation_rows, authority, cluster)
        for authority_threshold in (0.70, 0.78, 0.84, 0.88, 0.92, 0.95, 0.97, 0.985):
            for cluster_threshold in (0.70, 0.78, 0.84, 0.88, 0.92, 0.95, 0.97, 0.985):
                for minimum_margin in (0.0, 0.025, 0.05, 0.075, 0.10, 0.15):
                    for competition_window_ms in (100.0, 250.0, 500.0, 1000.0):
                        for top_k in (1, 2):
                            gate = Gate(authority_threshold, cluster_threshold, minimum_margin, competition_window_ms, top_k)
                            prediction = predict_launches(scored, gate)
                            result = metrics(validation_rows, prediction)
                            if result["true"] < 5 or result["recall"] < 0.10:
                                continue
                            valid = result["precision"] >= 0.55 and result["precision_wilson_low"] >= 0.30
                            objective = (
                                1.0 if valid else 0.0,
                                result["precision_wilson_low"],
                                result["precision"],
                                result["recall"],
                                result["true"],
                                -result["false_positives"],
                            )
                            if best is None or objective > best[0]:
                                best = (objective, authority, cluster, gate, result)
    if best is None:
        raise RuntimeError("no rule produced five validation true positives")
    return best[1], best[2], best[3], best[4]


def export_pipeline(model: Pipeline, names: Sequence[str]) -> dict[str, Any]:
    scaler: StandardScaler = model.named_steps["scale"]
    logit: LogisticRegression = model.named_steps["logit"]
    return {
        "features": list(names),
        "mean": [float(value) for value in scaler.mean_],
        "scale": [float(value) for value in scaler.scale_],
        "coefficient": [float(value) for value in logit.coef_[0]],
        "intercept": float(logit.intercept_[0]),
    }


def distill(model: Pipeline, names: Sequence[str], limit: int = 8) -> list[dict[str, Any]]:
    logit: LogisticRegression = model.named_steps["logit"]
    rows = sorted(zip(names, logit.coef_[0]), key=lambda pair: abs(float(pair[1])), reverse=True)
    return [{"feature": name, "coefficient": float(value)} for name, value in rows[:limit]]


def main() -> int:
    parser = argparse.ArgumentParser(description="Causal multimode rerun of E4 entry intent versus true ignores")
    parser.add_argument("--pair", action="append", default=[], metavar="BATCH:EVENTS")
    parser.add_argument("--attempts", action="append", default=[], type=Path)
    parser.add_argument("--metadata-cache", action="append", default=[], type=Path)
    parser.add_argument("--creator-history", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model-output", type=Path, required=True)
    parser.add_argument("--metadata-concurrency", type=int, default=96)
    parser.add_argument("--metadata-timeout", type=float, default=5.0)
    args = parser.parse_args()
    if len(args.pair) < 6:
        parser.error("at least six chronological live samples are required")

    pairs = [parse_pair(value) for value in args.pair]
    launches, run_ids = load_launches(pairs)
    failed = load_failed_attempts(args.attempts)
    snapshots = build_snapshots(launches, failed)

    cache = scan_metadata_cache(args.metadata_cache)
    relevant_launches = {
        mint: launch for mint, launch in launches.items()
        if any(row["mint"] == mint and (row["positive"] or eligible(row)) for row in snapshots)
    }
    cache = asyncio.run(fill_metadata(relevant_launches, cache, args.metadata_concurrency, args.metadata_timeout))
    add_metadata(snapshots, cache)
    add_history(snapshots, load_creator_history(args.creator_history))
    add_competition(snapshots)

    run_count = len(run_ids)
    live_index = run_count - 1
    validation_start = max(3, live_index - 3)
    train = [row for row in snapshots if integer(row["run_index"]) < validation_start]
    validation = [row for row in snapshots if validation_start <= integer(row["run_index"]) < live_index]
    live = [row for row in snapshots if integer(row["run_index"]) == live_index]

    authority_seed, cluster_seed, gate, validation_metrics = tune(train, validation)
    # Refit the frozen model on all pre-live observations after gate selection.
    pre_live = [row for row in snapshots if integer(row["run_index"]) < live_index]
    authority = fit(pre_live, AUTHORITY_FEATURES, float(authority_seed.named_steps["logit"].C))
    cluster = fit(pre_live, CLUSTER_FEATURES, float(cluster_seed.named_steps["logit"].C))
    live_scored = score_rows(live, authority, cluster)
    live_predictions = predict_launches(live_scored, gate)
    live_metrics = metrics(live, live_predictions)

    # Walk-forward audit using the frozen gate: each validation/live fold is
    # scored by models trained only on earlier runs.
    walk_folds = []
    aggregate_true = aggregate_predictions = aggregate_positives = 0
    for fold in range(max(3, validation_start), run_count):
        fold_train = [row for row in snapshots if integer(row["run_index"]) < fold]
        fold_rows = [row for row in snapshots if integer(row["run_index"]) == fold]
        if not fold_rows:
            continue
        fold_authority = fit(fold_train, AUTHORITY_FEATURES, float(authority_seed.named_steps["logit"].C))
        fold_cluster = fit(fold_train, CLUSTER_FEATURES, float(cluster_seed.named_steps["logit"].C))
        fold_result = metrics(fold_rows, predict_launches(score_rows(fold_rows, fold_authority, fold_cluster), gate))
        fold_result["run_id"] = run_ids[fold]
        walk_folds.append(fold_result)
        aggregate_true += integer(fold_result["true"])
        aggregate_predictions += integer(fold_result["predictions"])
        aggregate_positives += integer(fold_result["positives"])

    walk = {
        "folds": walk_folds,
        "true": aggregate_true,
        "predictions": aggregate_predictions,
        "positives": aggregate_positives,
        "precision": aggregate_true / aggregate_predictions if aggregate_predictions else 0.0,
        "precision_wilson_low": wilson_lower(aggregate_true, aggregate_predictions),
        "recall": aggregate_true / aggregate_positives if aggregate_positives else 0.0,
    }

    pass_validation = bool(
        validation_metrics["precision"] >= 0.55
        and validation_metrics["recall"] >= 0.10
        and validation_metrics["true"] >= 5
    )
    pass_walk = bool(
        walk["precision"] >= 0.55
        and walk["precision_wilson_low"] >= 0.30
        and walk["recall"] >= 0.10
        and walk["true"] >= 10
    )
    pass_live = bool(
        live_metrics["precision"] >= 0.50
        and live_metrics["recall"] >= 0.10
        and live_metrics["true"] >= 2
        and live_metrics["all_true_pre_intent"]
    )
    status = "LIVE_HOLDOUT_CONFIRMED" if pass_validation and pass_walk and pass_live else "NOT_CONCLUSIVE"

    model_payload = {
        "version": "e4-v12-causal-multimode-entry-v1",
        "status": status,
        "thesis": (
            "E4 enters the highest-authority unsold low-FDV launch visible in a short competition window when either "
            "(A) creator/social launch authority is present or (B) the first-slot buyer set overlaps E4's prior intent graph."
        ),
        "guardrails": {
            "minimum_creator_seed_sol": 0.20,
            "minimum_fdv_usd": 2750.0,
            "maximum_fdv_usd": 10000.0,
            "maximum_age_ms": 1500.0,
            "pre_entry_sell_count": 0,
            "mayhem_allowed": False,
        },
        "gate": gate.as_dict(),
        "authority_model": export_pipeline(authority, AUTHORITY_FEATURES),
        "wallet_cluster_model": export_pipeline(cluster, CLUSTER_FEATURES),
        "distilled_drivers": {
            "launch_authority": distill(authority, AUTHORITY_FEATURES),
            "wallet_cluster": distill(cluster, CLUSTER_FEATURES),
        },
        "validation": validation_metrics,
        "walk_forward": walk,
        "live_holdout": live_metrics,
        "live_run_id": run_ids[live_index],
        "training_run_ids": run_ids[:live_index],
    }

    report = {
        "version": "e4-v12-conclusive-entry-rerun-v1",
        "status": status,
        "coverage": {
            "runs": run_ids,
            "launches": len(launches),
            "snapshots": len(snapshots),
            "successful_entries": sum(row["label"] == "SUCCESS" for row in snapshots),
            "mapped_failed_attempts": sum(row["label"] == "FAILED_ATTEMPT" for row in snapshots),
            "true_ignores": sum(row["label"] == "IGNORED" for row in snapshots),
            "metadata_cached_or_resolved": sum(bool(row.get("metadata_ok")) for row in snapshots),
        },
        "causality": {
            "positive_snapshot": "state immediately before the first successful or mapped failed E4 BUY attempt",
            "negative_snapshot": "strongest unsold state within 1500ms for a launch with no observed E4 entry intent",
            "history": "creator, handle and first-buyer authority uses only earlier E4 intentions",
            "live_holdout": "newest chronological 3000-launch sample excluded from fitting and threshold selection",
        },
        "thesis": model_payload["thesis"],
        "gate": gate.as_dict(),
        "distilled_drivers": model_payload["distilled_drivers"],
        "validation": validation_metrics,
        "walk_forward": walk,
        "live_holdout": live_metrics,
        "safe_to_implement": status == "LIVE_HOLDOUT_CONFIRMED",
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.model_output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    args.model_output.write_text(json.dumps(model_payload, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({
        "status": status,
        "coverage": report["coverage"],
        "gate": report["gate"],
        "validation": {key: validation_metrics[key] for key in ("positives", "predictions", "true", "precision", "recall")},
        "walk_forward": {key: walk[key] for key in ("positives", "predictions", "true", "precision", "precision_wilson_low", "recall")},
        "live_holdout": {key: live_metrics[key] for key in ("positives", "predictions", "true", "precision", "recall", "success_recall", "failed_attempt_recall", "modes")},
    }, indent=2, sort_keys=True), flush=True)
    return 0 if status == "LIVE_HOLDOUT_CONFIRMED" else 3


if __name__ == "__main__":
    raise SystemExit(main())
