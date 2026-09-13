#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import gzip
import hashlib
import importlib.util
import json
import math
import statistics
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

import aiohttp
import numpy as np
from sklearn.decomposition import PCA
from sklearn.neighbors import NearestNeighbors
from sklearn.preprocessing import RobustScaler

FROZEN_CORPUS_SHA256 = "6f41376cfee3d54d57774b4368ec8b50c9e59becbbd04650cf56a20ef48dce6c"
PCA_SEED = 7331
STARTING_BANKROLL_SOL = 2.0
POSITION_FRACTION = 0.0185
MAX_CONCURRENT = 2
RESERVE_SOL = 0.03

CANDIDATES = {
    "A_86_96": {
        "k": 3,
        "policy": "hold_1000ms",
        "threshold": 0.9923740946678363,
        "historical_experiment_id": "e4x-prototype-c635917c1bb80abe24f25e95d760a7c421101abceb2fac49c19860e0a1b0b12a",
    },
    "B_75": {
        "k": 10,
        "policy": "hold_2000ms",
        "threshold": 0.9838282174643644,
        "historical_experiment_id": "e4x-prototype-c635917c1bb80abe24f25e95d760a7c421101abceb2fac49c19860e0a1b0b12a",
    },
}


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


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    temporary.replace(path)


def profit_factor(values: Sequence[float]) -> float:
    gains = sum(value for value in values if value > 0)
    losses = -sum(value for value in values if value < 0)
    if losses > 0:
        return gains / losses
    return 999.0 if gains > 0 else 0.0


def percentile_against(reference: np.ndarray, value: float) -> float:
    ordered = np.sort(reference, kind="stable")
    index = np.searchsorted(ordered, value, side="right") - 1
    return float(np.clip(index / max(1, len(ordered) - 1), 0.0, 1.0))


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class DistanceHead:
    def __init__(self, representation: np.ndarray, train: np.ndarray, target: np.ndarray, k: int):
        positive = np.flatnonzero(train & target)
        negative = np.flatnonzero(train & ~target)
        if len(positive) < k or len(negative) < k:
            raise RuntimeError(f"insufficient frozen prototypes for k={k}")
        self.positive = NearestNeighbors(n_neighbors=k, metric="euclidean", algorithm="auto", n_jobs=-1)
        self.negative = NearestNeighbors(n_neighbors=k, metric="euclidean", algorithm="auto", n_jobs=-1)
        self.positive.fit(representation[positive])
        self.negative.fit(representation[negative])
        self.reference = self.raw(representation)

    def raw(self, matrix: np.ndarray) -> np.ndarray:
        positive_distance = self.positive.kneighbors(matrix, return_distance=True)[0].mean(axis=1)
        negative_distance = self.negative.kneighbors(matrix, return_distance=True)[0].mean(axis=1)
        return np.log1p(negative_distance) - np.log1p(positive_distance)

    def percentile(self, vector: np.ndarray) -> float:
        value = float(self.raw(vector.reshape(1, -1))[0])
        return percentile_against(self.reference, value)


class FrozenPrototypeScorer:
    def __init__(self, base: Any, load_corpus_stream: Any, corpus_path: Path):
        self.base = base
        self.hist = load_corpus_stream(corpus_path, 0)
        self.train = self.hist.splits == "train"
        self.fields = list(base.BASE_NUMERIC) + [f"{stem}_0ms" for stem in base.WINDOW_STEMS]
        train_rows = [row for row in self.hist.rows if str(row.get("split")) == "train"]
        self.medians: dict[str, float] = {}
        for field in self.fields:
            values = []
            for row in train_rows:
                raw = row.get(field)
                if raw is None:
                    continue
                value = finite(raw, float("nan"))
                if math.isfinite(value):
                    values.append(value)
            self.medians[field] = statistics.median(values) if values else 0.0

        scaler = RobustScaler(quantile_range=(10.0, 90.0))
        scaler.fit(self.hist.x_general[self.train])
        historical_scaled = np.clip(scaler.transform(self.hist.x_general), -15, 15).astype(np.float32)
        pca = PCA(n_components=32, whiten=True, random_state=PCA_SEED)
        pca.fit(historical_scaled[self.train])
        self.scaler = scaler
        self.pca = pca
        self.historical_representation = pca.transform(historical_scaled).astype(np.float32)
        self.models: dict[str, dict[str, Any]] = {}

        for name, config in CANDIDATES.items():
            k = int(config["k"])
            intent = DistanceHead(self.historical_representation, self.train, self.hist.selected, k)
            profit_target = self.hist.pnl[str(config["policy"])].min(axis=1) > 0
            profit = DistanceHead(self.historical_representation, self.train, profit_target, k)
            self.models[name] = {"intent": intent, "profit": profit}

    def vector(self, row: Mapping[str, Any]) -> np.ndarray:
        vector = np.zeros((1, 2 * len(self.fields)), dtype=np.float32)
        offset = 0
        for field in self.fields:
            raw = row.get(field)
            missing = raw is None
            vector[0, offset] = finite(raw, self.medians[field]) if not missing else self.medians[field]
            vector[0, offset + 1] = float(missing)
            offset += 2
        scaled = np.clip(self.scaler.transform(vector), -15, 15).astype(np.float32)
        return self.pca.transform(scaled).astype(np.float32)[0]

    def score(self, row: Mapping[str, Any]) -> dict[str, float]:
        vector = self.vector(row)
        scores: dict[str, float] = {}
        for name, model in self.models.items():
            intent = model["intent"].percentile(vector)
            profit = model["profit"].percentile(vector)
            scores[name] = math.sqrt(max(1e-9, intent) * max(1e-9, profit))
        return scores


@dataclass
class PaperPosition:
    mint: str
    score: float
    create_ns: int
    signal_ns: int
    entry_ns: int
    entry_state_ns: int
    entry_fdv_usd: float
    stake_sol: float
    tokens: float
    hold_ms: int


@dataclass
class PaperAccount:
    name: str
    config: Mapping[str, Any]
    build: Any
    latest_states: dict[str, dict[str, Any]]
    entry_delay_ms: float
    balance_sol: float = STARTING_BANKROLL_SOL
    signals: int = 0
    skipped_concurrency: int = 0
    rejected_no_state: int = 0
    rejected_no_fill: int = 0
    active: dict[str, PaperPosition] = field(default_factory=dict)
    ledger: list[dict[str, Any]] = field(default_factory=list)
    tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def schedule_signal(self, mint: str, score: float, create_ns: int) -> None:
        self.signals += 1
        signal_ns = time.time_ns()
        task = asyncio.create_task(self._enter_then_exit(mint, score, create_ns, signal_ns))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _enter_then_exit(self, mint: str, score: float, create_ns: int, signal_ns: int) -> None:
        if self.entry_delay_ms > 0:
            await asyncio.sleep(self.entry_delay_ms / 1000.0)
        async with self.lock:
            if len(self.active) >= MAX_CONCURRENT:
                self.skipped_concurrency += 1
                return
            state = self.latest_states.get(mint)
            if not state:
                self.rejected_no_state += 1
                return
            stake = min(max(0.0, self.balance_sol - RESERVE_SOL), self.balance_sol * POSITION_FRACTION)
            if stake <= 0:
                self.rejected_no_fill += 1
                return
            tokens, _curve_input = self.build.quote_buy(stake, state)
            if tokens <= 0:
                self.rejected_no_fill += 1
                return
            entry_ns = time.time_ns()
            hold_ms = integer(str(self.config["policy"]).removeprefix("hold_").removesuffix("ms"))
            position = PaperPosition(
                mint=mint,
                score=score,
                create_ns=create_ns,
                signal_ns=signal_ns,
                entry_ns=entry_ns,
                entry_state_ns=integer(state.get("ns")),
                entry_fdv_usd=finite(state.get("fdv")),
                stake_sol=stake,
                tokens=tokens,
                hold_ms=hold_ms,
            )
            self.active[mint] = position

        await asyncio.sleep(position.hold_ms / 1000.0)

        async with self.lock:
            current = self.active.pop(mint, None)
            if current is None:
                return
            state = self.latest_states.get(mint)
            proceeds = self.build.quote_sell(current.tokens, state or {}) if state else 0.0
            pnl = proceeds - current.stake_sol
            self.balance_sol += pnl
            exit_ns = time.time_ns()
            self.ledger.append(
                {
                    "mint": mint,
                    "score": current.score,
                    "create_ns": current.create_ns,
                    "signal_ns": current.signal_ns,
                    "entry_ns": current.entry_ns,
                    "exit_ns": exit_ns,
                    "create_to_signal_ms": (current.signal_ns - current.create_ns) / 1_000_000.0,
                    "create_to_entry_ms": (current.entry_ns - current.create_ns) / 1_000_000.0,
                    "signal_to_entry_ms": (current.entry_ns - current.signal_ns) / 1_000_000.0,
                    "actual_hold_ms": (exit_ns - current.entry_ns) / 1_000_000.0,
                    "entry_state_ns": current.entry_state_ns,
                    "exit_state_ns": integer((state or {}).get("ns")),
                    "entry_fdv_usd": current.entry_fdv_usd,
                    "exit_fdv_usd": finite((state or {}).get("fdv")),
                    "stake_sol": current.stake_sol,
                    "tokens": current.tokens,
                    "proceeds_sol": proceeds,
                    "pnl_sol": pnl,
                    "win": pnl > 0,
                    "balance_after_sol": self.balance_sol,
                }
            )

    async def drain(self) -> None:
        if self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=False)

    def summary(self) -> dict[str, Any]:
        pnls = [finite(row.get("pnl_sol")) for row in self.ledger]
        wins = sum(value > 0 for value in pnls)
        peak = STARTING_BANKROLL_SOL
        maximum_drawdown = 0.0
        for row in self.ledger:
            balance = finite(row.get("balance_after_sol"), peak)
            peak = max(peak, balance)
            if peak > 0:
                maximum_drawdown = max(maximum_drawdown, (peak - balance) / peak)
        return {
            "signals": self.signals,
            "closed_trades": len(self.ledger),
            "wins": wins,
            "losses": len(self.ledger) - wins,
            "win_rate": wins / len(self.ledger) if self.ledger else None,
            "net_pnl_sol": self.balance_sol - STARTING_BANKROLL_SOL,
            "roi_fraction": (self.balance_sol / STARTING_BANKROLL_SOL) - 1.0,
            "ending_bankroll_sol": self.balance_sol,
            "profit_factor": profit_factor(pnls),
            "maximum_drawdown_fraction": maximum_drawdown,
            "skipped_concurrency": self.skipped_concurrency,
            "rejected_no_state": self.rejected_no_state,
            "rejected_no_fill": self.rejected_no_fill,
            "active_positions": len(self.active),
        }


def state_from_live_event(event: Any, build: Any) -> dict[str, Any] | None:
    raw = event.raw if isinstance(getattr(event, "raw", None), Mapping) else {}
    virtual_sol = build.normal_sol(raw.get("virtual_sol_reserves"))
    virtual_tokens = build.normal_tokens(raw.get("virtual_token_reserves"))
    real_tokens = build.normal_tokens(raw.get("real_token_reserves"))
    if virtual_sol <= 0 or virtual_tokens <= 0:
        return None
    return {
        "ns": integer(getattr(event, "received_ns", 0)),
        "slot": integer(getattr(event, "slot", -1), -1),
        "vsol": virtual_sol,
        "vtok": virtual_tokens,
        "rtok": real_tokens if real_tokens > 0 else float("inf"),
        "price": finite(getattr(event, "price_sol", None)),
        "fdv": finite(getattr(event, "fdv_usd", None)),
        "complete": bool(getattr(event, "complete", False)),
    }


def live_row_from_payload(create: Any, same_payload: Sequence[Any], build: Any) -> dict[str, Any]:
    raw = create.raw if isinstance(getattr(create, "raw", None), Mapping) else {}
    creator = str(getattr(create, "creator", None) or raw.get("creator") or raw.get("user") or "")
    name = str(raw.get("name") or "")
    symbol = str(raw.get("symbol") or "")
    uri = str(raw.get("uri") or "")
    create_ns = integer(getattr(create, "received_ns", 0))
    create_slot = integer(getattr(create, "slot", -1), -1)
    signature = str(getattr(create, "signature", "") or "")

    visible = [
        event
        for event in same_payload
        if event.mint == create.mint
        and integer(getattr(event, "received_ns", 0)) <= create_ns
        and str(getattr(event, "kind", "")) != "CREATE"
        and str(getattr(event, "trader", "") or "") != build.E4_WALLET
    ]
    buys = [event for event in visible if str(getattr(event, "kind", "")) in build.BUY_KINDS]
    sells = [event for event in visible if str(getattr(event, "kind", "")) in build.SELL_KINDS]
    creator_buys = [event for event in buys if str(getattr(event, "trader", "") or "") == creator]
    outside_buys = [
        event
        for event in buys
        if str(getattr(event, "trader", "") or "") and str(getattr(event, "trader", "") or "") != creator
    ]
    outside_buyers: list[str] = []
    for event in outside_buys:
        trader = str(getattr(event, "trader", "") or "")
        if trader and trader not in outside_buyers:
            outside_buyers.append(trader)
    signatures = Counter(str(getattr(event, "signature", "") or "") for event in buys if getattr(event, "signature", None))
    states = [state_from_live_event(event, build) for event in same_payload if event.mint == create.mint]
    states = [state for state in states if state]
    state = states[-1] if states else state_from_live_event(create, build)

    row: dict[str, Any] = {
        "mint": create.mint,
        "source_run_id": "online-live",
        "split": "live",
        "create_ns": create_ns,
        "create_slot": create_slot,
        "create_event_index": integer(getattr(create, "event_index", 0)),
        "create_signature": signature,
        "creator": creator,
        "name": name,
        "symbol": symbol,
        "metadata_uri": uri,
        "metadata_uri_host": build.uri_host(uri),
        "token_program": str(raw.get("token_program") or ""),
        "mayhem_mode": bool(raw.get("is_mayhem_mode", False)),
        "cashback_enabled": bool(raw.get("is_cashback_enabled", False)),
        "initial_virtual_sol": build.normal_sol(raw.get("virtual_sol_reserves")),
        "initial_virtual_tokens": build.normal_tokens(raw.get("virtual_token_reserves")),
        "initial_real_tokens": build.normal_tokens(raw.get("real_token_reserves")),
        "create_price_sol": finite(getattr(create, "price_sol", None)),
        "create_fdv_usd": finite(getattr(create, "fdv_usd", None)),
        "selected_by_e4": False,
        "landed_successfully": False,
        "failed_fill_selection": False,
        "decision_ns": None,
        "e4_pnl_sol": None,
        "e4_exit_time_s": None,
        **build.text_features("name", name),
        **build.text_features("symbol", symbol),
    }
    for window_ms in build.EARLY_WINDOWS_MS:
        row[f"first_outside_buyers_{window_ms}ms"] = []
    row["first_outside_buyers_0ms"] = outside_buyers[:12]
    row.update(
        {
            "buy_count_0ms": len(buys),
            "sell_count_0ms": len(sells),
            "creator_buy_count_0ms": len(creator_buys),
            "creator_buy_sol_0ms": sum(finite(getattr(event, "sol_amount", 0.0)) for event in creator_buys),
            "outside_buy_sol_0ms": sum(finite(getattr(event, "sol_amount", 0.0)) for event in outside_buys),
            "unique_outside_buyers_0ms": len(outside_buyers),
            "distinct_buy_signatures_0ms": len(signatures),
            "max_buys_per_signature_0ms": max(signatures.values(), default=0),
            "same_slot_buy_count_0ms": sum(integer(getattr(event, "slot", -1), -1) == create_slot for event in buys),
            "same_slot_outside_buyer_count_0ms": len(
                {
                    str(getattr(event, "trader", "") or "")
                    for event in outside_buys
                    if integer(getattr(event, "slot", -1), -1) == create_slot
                }
            ),
            "same_create_signature_buy_count_0ms": sum(str(getattr(event, "signature", "") or "") == signature for event in buys),
            "fdv_0ms": finite((state or {}).get("fdv"), finite(getattr(create, "fdv_usd", None))),
            "price_0ms": finite((state or {}).get("price"), finite(getattr(create, "price_sol", None))),
            "virtual_sol_0ms": finite((state or {}).get("vsol"), build.normal_sol(raw.get("virtual_sol_reserves"))),
            "virtual_tokens_0ms": finite((state or {}).get("vtok"), build.normal_tokens(raw.get("virtual_token_reserves"))),
            "real_tokens_0ms": finite((state or {}).get("rtok"), build.normal_tokens(raw.get("real_token_reserves"))),
        }
    )
    return row


async def endpoint_worker(
    url: str,
    prod: Any,
    queue: asyncio.Queue[Mapping[str, Any]],
    stop: asyncio.Event,
    counters: dict[str, dict[str, int]],
    errors: list[str],
) -> None:
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=8, sock_read=20)
    endpoint = counters.setdefault(url, {"connections": 0, "messages": 0, "disconnects": 0})
    async with aiohttp.ClientSession(timeout=timeout) as session:
        while not stop.is_set():
            try:
                async with session.ws_connect(url, heartbeat=10, max_msg_size=8 * 1024 * 1024) as ws:
                    endpoint["connections"] += 1
                    await ws.send_json(
                        {
                            "jsonrpc": "2.0",
                            "id": endpoint["connections"],
                            "method": "logsSubscribe",
                            "params": [
                                {"mentions": [prod.PUMP_PROGRAM_ID]},
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
                            await queue.put(payload)
                        elif message.type in {aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE}:
                            endpoint["disconnects"] += 1
                            break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                endpoint["disconnects"] += 1
                errors.append(f"{url}: {type(exc).__name__}: {exc}")
                if len(errors) > 100:
                    del errors[:-100]
                try:
                    await asyncio.wait_for(stop.wait(), timeout=0.5)
                except asyncio.TimeoutError:
                    pass


def report_payload(
    started_ns: int,
    launches: int,
    decoded_events: int,
    accounts: Mapping[str, PaperAccount],
    endpoint_counters: Mapping[str, Any],
    errors: Sequence[str],
    final: bool = False,
) -> dict[str, Any]:
    return {
        "version": "e4-v12-dual-prototype-online-live-v1",
        "status": "complete" if final else "running",
        "mode": "true-online-forward-paper",
        "started_ns": started_ns,
        "checked_ns": time.time_ns(),
        "elapsed_seconds": (time.time_ns() - started_ns) / 1_000_000_000.0,
        "launches_seen": launches,
        "decoded_events": decoded_events,
        "starting_bankroll_sol_per_thesis": STARTING_BANKROLL_SOL,
        "no_post_collection_rescoring": True,
        "no_live_retuning": True,
        "frozen_candidates": CANDIDATES,
        "accounts": {
            name: {"summary": account.summary(), "ledger": account.ledger if final else []}
            for name, account in accounts.items()
        },
        "endpoints": endpoint_counters,
        "recent_errors": list(errors[-20:]),
    }


async def run(args: argparse.Namespace) -> int:
    if sha256_path(args.corpus) != FROZEN_CORPUS_SHA256:
        raise RuntimeError("frozen corpus SHA mismatch")

    production_root = args.production_root.resolve()
    research_root = args.research_root.resolve()
    sys.path.insert(0, str(production_root / "src"))
    sys.path.insert(0, str(research_root))

    prod = load_module("e4_v12_online_prod", production_root / "scripts" / "e4_live_market_stress.py")
    from scripts import e4_v12_allout_launch_corpus as build
    from scripts import e4_v12_allout_launch_corpus_stream as cstream
    from scripts import e4_v12_allout_profit_hazard as research_base
    from scripts.e4_v12_allout_profit_hazard_stream import load_corpus_stream

    scorer = FrozenPrototypeScorer(research_base, load_corpus_stream, args.corpus)
    history = cstream.CausalHistory()
    with gzip.open(args.corpus, "rt", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                history.enrich([json.loads(line)])

    latest_states: dict[str, dict[str, Any]] = {}
    accounts = {
        name: PaperAccount(name, config, build, latest_states, args.entry_delay_ms)
        for name, config in CANDIDATES.items()
    }

    queue: asyncio.Queue[Mapping[str, Any]] = asyncio.Queue(maxsize=20_000)
    stop = asyncio.Event()
    endpoint_counters: dict[str, dict[str, int]] = {}
    errors: list[str] = []
    dedupe: set[tuple[str, int, str]] = set()
    seen_mints: set[str] = set()
    workers = [
        asyncio.create_task(endpoint_worker(url, prod, queue, stop, endpoint_counters, errors))
        for url in dict.fromkeys(args.ws_url or prod.DEFAULT_WS_RPCS)
    ]

    started_monotonic = time.monotonic()
    hard_deadline = started_monotonic + args.duration_seconds
    started_ns = time.time_ns()
    last_progress = 0.0
    launches = 0
    decoded_events = 0

    try:
        while time.monotonic() < hard_deadline:
            if args.target_closed_trades > 0 and all(
                len(account.ledger) >= args.target_closed_trades for account in accounts.values()
            ):
                break
            try:
                payload = await asyncio.wait_for(queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                payload = None

            if payload is not None:
                incoming: list[Any] = []
                prod.decode_log_payload(payload, incoming, dedupe)
                decoded_events += len(incoming)
                for event in incoming:
                    state = state_from_live_event(event, build)
                    if state:
                        latest_states[event.mint] = state

                creates = [event for event in incoming if str(getattr(event, "kind", "")) == "CREATE"]
                for create in creates:
                    if create.mint in seen_mints:
                        continue
                    seen_mints.add(create.mint)
                    launches += 1
                    row = live_row_from_payload(create, incoming, build)
                    history.enrich([row])
                    score_started = time.perf_counter_ns()
                    scores = scorer.score(row)
                    score_elapsed_ms = (time.perf_counter_ns() - score_started) / 1_000_000.0
                    for name, score in scores.items():
                        config = CANDIDATES[name]
                        if score >= finite(config["threshold"]):
                            accounts[name].schedule_signal(create.mint, score, integer(create.received_ns))
                            print(
                                "E4_THESIS_SIGNAL "
                                + json.dumps(
                                    {
                                        "thesis": name,
                                        "mint": create.mint,
                                        "score": score,
                                        "threshold": config["threshold"],
                                        "score_compute_ms": score_elapsed_ms,
                                        "launch_number": launches,
                                    },
                                    separators=(",", ":"),
                                ),
                                flush=True,
                            )

            now = time.monotonic()
            if now - last_progress >= args.heartbeat_seconds:
                payload = report_payload(started_ns, launches, decoded_events, accounts, endpoint_counters, errors)
                atomic_json(args.progress, payload)
                compact = {
                    "launches": launches,
                    "decoded_events": decoded_events,
                    "accounts": {name: account.summary() for name, account in accounts.items()},
                }
                print("E4_THESIS_PROGRESS " + json.dumps(compact, separators=(",", ":")), flush=True)
                last_progress = now
    finally:
        stop.set()
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)

    await asyncio.gather(*(account.drain() for account in accounts.values()))
    final = report_payload(started_ns, launches, decoded_events, accounts, endpoint_counters, errors, final=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(final, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    atomic_json(args.progress, final)

    lines = [
        "# E4 V12 dual thesis — true online forward paper test",
        "",
        f"Launches observed live: {launches}",
        f"Entry delay floor: {args.entry_delay_ms:.3f} ms plus actual scoring/stream latency",
        "",
        "| Thesis | Signals | Closed | Wins | Losses | WR | Net P&L | Ending SOL | PF | Max DD |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, account in accounts.items():
        summary = account.summary()
        wr = "N/A" if summary["win_rate"] is None else f"{summary['win_rate'] * 100:.2f}%"
        lines.append(
            f"| {name} | {summary['signals']} | {summary['closed_trades']} | {summary['wins']} | "
            f"{summary['losses']} | {wr} | {summary['net_pnl_sol']:+.6f} | "
            f"{summary['ending_bankroll_sol']:.6f} | {summary['profit_factor']:.3f} | "
            f"{summary['maximum_drawdown_fraction'] * 100:.3f}% |"
        )
    markdown = args.output.with_suffix(".md")
    markdown.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines), flush=True)
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="True online forward paper test for the two frozen E4 V12 prototype theses")
    result.add_argument("--research-root", type=Path, required=True)
    result.add_argument("--production-root", type=Path, required=True)
    result.add_argument("--corpus", type=Path, required=True)
    result.add_argument("--duration-seconds", type=float, default=19_800.0)
    result.add_argument("--target-closed-trades", type=int, default=20)
    result.add_argument("--entry-delay-ms", type=float, default=10.0)
    result.add_argument("--heartbeat-seconds", type=float, default=60.0)
    result.add_argument("--ws-url", action="append", default=[])
    result.add_argument("--output", type=Path, required=True)
    result.add_argument("--progress", type=Path, required=True)
    return result


def main() -> int:
    return asyncio.run(run(parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
