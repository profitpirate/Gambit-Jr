#!/usr/bin/env python3
"""Untouched-forward seven-model paper tournament.

Every arm sees the same newly captured Solana launch window. No transaction is
signed or broadcast. The E4 pre-armed arms use only frozen pre-launch identity
evidence plus immutable launch metadata timestamps; live X transport latency is
not claimed.
"""
from __future__ import annotations

import argparse
import asyncio
import heapq
import json
import math
import re
import time
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

import aiohttp

import e4_v12_true_latency_replay as replay
import golden_buyer_reputation_core as core
import golden_management_v2 as v2
import golden_management_v2_1 as v21

VERSION = "golden-sevenmodel-forward-v1"
MODELS = (
    "FULL_3S",
    "FULL_4S",
    "FULL_3S_V2_1",
    "FULL_3S_LIBRARY",
    "FULL_3S_V2_1_LIBRARY",
    "E4_PREARMED",
    "E4_PREARMED_CONVICTION_LIBRARY",
)
STARTING_SOL = 3.0
RESERVE_SOL = 0.03
MAX_CONCURRENT = 2
ENTRY_LATENCY_MS = 20.0
GOLDEN_MAX_SLIPPAGE = 0.20
PLATFORM_BPS = 95.0
PROTOCOL_BPS = 125.0
BASE_FEE = 0.000005
PRIORITY_FEE = 0.001
BUY_BRIBE = 0.001
SELL_BRIBE = 0.0
VARIABLE_RATE = (PLATFORM_BPS + PROTOCOL_BPS) / 10_000.0
BUY_FIXED = BASE_FEE + PRIORITY_FEE + BUY_BRIBE
SELL_FIXED = BASE_FEE + PRIORITY_FEE + SELL_BRIBE
E4_WALLET = core.E4_WALLET
STATUS_RE = re.compile(r"(?:twitter\.com|x\.com)/([^/?#]+)/status/(\d+)", re.I)

def finite(v: Any, default: float = 0.0) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    return x if math.isfinite(x) else default

def integer(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default

def atomic_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    tmp.replace(path)

def event_kind(row: Mapping[str, Any]) -> str:
    return str(row.get("kind") or row.get("anchor_event") or "").upper()

def event_creator(row: Mapping[str, Any]) -> str:
    raw = row.get("raw") if isinstance(row.get("raw"), Mapping) else {}
    return str(row.get("creator") or raw.get("creator") or raw.get("user") or row.get("trader") or "")

def event_uri(row: Mapping[str, Any]) -> str:
    raw = row.get("raw") if isinstance(row.get("raw"), Mapping) else {}
    return str(row.get("uri") or raw.get("uri") or "")

def price(state: replay.ReserveState | None) -> float:
    if state is None or state.virtual_sol <= 0 or state.virtual_tokens <= 0:
        return 0.0
    return state.price_sol if state.price_sol > 0 else state.virtual_sol / state.virtual_tokens

def state_at_or_after_exact(states: list[replay.ReserveState], ns: int) -> replay.ReserveState | None:
    state = replay.state_at_or_after(states, ns)
    return state if state is not None and state.received_ns >= ns else None

def buy_curve_for_budget(budget: float) -> float:
    return max(0.0, (budget - BUY_FIXED) / (1.0 + VARIABLE_RATE))

def entry_cost(curve_sol: float) -> float:
    return curve_sol * (1.0 + VARIABLE_RATE) + BUY_FIXED

def sell_proceeds(tokens: float, state: replay.ReserveState) -> float:
    gross = replay.sell_sol(tokens, state)
    return max(0.0, gross * (1.0 - VARIABLE_RATE) - SELL_FIXED)

def buy_tokens(curve_sol: float, state: replay.ReserveState) -> float:
    return replay.buy_tokens(curve_sol, state)

def metrics(ledger: list[dict[str, Any]]) -> dict[str, Any]:
    pnls = [finite(x.get("pnl_sol")) for x in ledger]
    wins = sum(x > 0 for x in pnls)
    gains = sum(x for x in pnls if x > 0)
    losses = -sum(x for x in pnls if x < 0)
    equity = STARTING_SOL
    peak = STARTING_SOL
    dd = 0.0
    for row in ledger:
        equity += finite(row.get("pnl_sol"))
        peak = max(peak, equity)
        if peak > 0:
            dd = max(dd, (peak - equity) / peak)
    return {
        "closed_trades": len(ledger),
        "wins": wins,
        "losses": len(ledger) - wins,
        "win_rate": wins / len(ledger) if ledger else None,
        "net_pnl_sol": sum(pnls),
        "profit_factor": gains / losses if losses else (999.0 if gains else None),
        "expectancy_sol": sum(pnls) / len(ledger) if ledger else None,
        "maximum_drawdown_fraction": dd,
    }

def empty_model() -> dict[str, Any]:
    return {"cash_sol": STARTING_SOL, "ledger": [], "rejections": {}}

def empty_state(e4_model_hash: str, library_fingerprint: str) -> dict[str, Any]:
    return {
        "version": VERSION,
        "paper_only": True,
        "real_money_execution": False,
        "starting_sol_per_model": STARTING_SOL,
        "target_per_model": 100,
        "entry_latency_ms_common": ENTRY_LATENCY_MS,
        "models": {name: empty_model() for name in MODELS},
        "processed_runs": [],
        "reputation_delta": {},
        "metadata_cache": {},
        "e4_model_sha256": e4_model_hash,
        "library_fingerprint": library_fingerprint,
        "created_ns": time.time_ns(),
    }

def load_state(path: Path, e4_model_hash: str, library_fingerprint: str) -> dict[str, Any]:
    if not path.exists():
        return empty_state(e4_model_hash, library_fingerprint)
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("version") != VERSION:
        raise RuntimeError("seven-model state version mismatch")
    if state.get("e4_model_sha256") != e4_model_hash:
        raise RuntimeError("E4 frozen model changed mid-campaign")
    if state.get("library_fingerprint") != library_fingerprint:
        raise RuntimeError("library snapshot changed mid-campaign")
    if set(state.get("models", {})) != set(MODELS):
        raise RuntimeError("model roster changed mid-campaign")
    return state

def lib_stats(snapshot: Mapping[str, Any], creator: str) -> dict[str, Any]:
    row = ((snapshot.get("developers") or {}).get(creator) or {}) if creator else {}
    resolved = integer(row.get("resolved"))
    wins = integer(row.get("wins"))
    losses = integer(row.get("losses"))
    wr = finite(row.get("win_rate"), -1.0)
    mean = finite(row.get("mean_return"), 0.0)
    positive = bool(resolved >= 3 and wins >= 2 and wr >= 2/3 and mean > 0)
    strong_negative = bool(resolved >= 5 and (wr <= 0.25 or mean <= -0.10))
    return {
        "resolved": resolved, "wins": wins, "losses": losses,
        "win_rate": None if wr < 0 else wr, "mean_return": mean,
        "positive": positive, "strong_negative": strong_negative,
    }

TIER_ORDER = ("BASE", "MEDIUM", "HIGH", "EXCEPTIONAL")

def shifted_tier(tier: str, shift: int) -> str:
    i = TIER_ORDER.index(tier) if tier in TIER_ORDER else 0
    return TIER_ORDER[max(0, min(len(TIER_ORDER)-1, i + shift))]

def library_adjusted_conviction(conv: Mapping[str, Any], lib: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(conv)
    shift = 1 if lib.get("positive") else (-1 if lib.get("strong_negative") else 0)
    tier = shifted_tier(str(conv.get("conviction_tier") or "BASE"), shift)
    out["conviction_tier"] = tier
    out["position_fraction"] = v2.TIERS[tier].position_fraction
    out["library_tier_shift"] = shift
    out["library_stats"] = dict(lib)
    return out

def e4_conviction(prior_attempts: int, seed_sol: float, lib: Mapping[str, Any]) -> dict[str, Any]:
    score = 50.0
    if prior_attempts >= 2: score += 10
    if prior_attempts >= 3: score += 10
    if seed_sol >= 3.0: score += 10
    if seed_sol >= 5.0: score += 5
    if lib.get("positive"): score += 15
    if lib.get("strong_negative"): score -= 15
    if score >= 85: tier = "EXCEPTIONAL"
    elif score >= 75: tier = "HIGH"
    elif score >= 65: tier = "MEDIUM"
    else: tier = "BASE"
    return {
        "conviction_score": score,
        "conviction_tier": tier,
        "position_fraction": v2.TIERS[tier].position_fraction,
        "prior_e4_attempts": prior_attempts,
        "creator_seed_sol": seed_sol,
        "library_stats": dict(lib),
    }

def creator_seed(rows: list[dict[str, Any]], create: Mapping[str, Any], creator: str) -> float:
    sig = str(create.get("signature") or "")
    total = 0.0
    for row in rows:
        if event_kind(row) not in {"BUY", "PUMPSWAP_BUY"}:
            continue
        if str(row.get("trader") or "") != creator:
            continue
        if sig and str(row.get("signature") or "") != sig:
            continue
        raw = row.get("raw") if isinstance(row.get("raw"), Mapping) else {}
        amount = finite(row.get("sol_amount"))
        if amount <= 0:
            amount = core.normal_sol(raw.get("sol_amount"))
        total += max(0.0, amount)
    return total

def early_buyers(rows: list[dict[str, Any]], creator: str, create_ns: int) -> list[str]:
    cutoff = create_ns + core.DECISION_MS * 1_000_000
    out: list[str] = []
    for row in rows:
        ns = integer(row.get("received_ns"))
        if ns < create_ns or ns > cutoff or event_kind(row) not in {"BUY", "PUMPSWAP_BUY"}:
            continue
        wallet = str(row.get("trader") or "")
        if not wallet or wallet in (creator, E4_WALLET) or wallet in out:
            continue
        out.append(wallet)
    return out

def reputation_outcome(states: list[replay.ReserveState], create_ns: int) -> bool | None:
    entry = state_at_or_after_exact(states, create_ns + core.DECISION_MS * 1_000_000)
    exit_state = state_at_or_after_exact(states, create_ns + (core.DECISION_MS + core.HOLD_MS) * 1_000_000)
    if entry is None or exit_state is None:
        return None
    curve = buy_curve_for_budget(core.BENCHMARK_STAKE_SOL)
    if curve <= 0:
        return None
    tok = buy_tokens(curve, entry)
    if tok <= 0:
        return None
    return sell_proceeds(tok, exit_state) > entry_cost(curve)

def normalize_uri(uri: str) -> str:
    if uri.startswith("ipfs://"):
        return "https://ipfs.io/ipfs/" + uri[len("ipfs://"):].lstrip("/")
    if uri.startswith("ar://"):
        return "https://arweave.net/" + uri[len("ar://"):].lstrip("/")
    return uri

async def fetch_one(session: aiohttp.ClientSession, uri: str, sem: asyncio.Semaphore) -> dict[str, Any]:
    url = normalize_uri(uri)
    if not url.startswith(("http://", "https://")):
        return {"error": "unsupported_uri"}
    async with sem:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=12), allow_redirects=True) as r:
                if r.status != 200:
                    return {"error": f"http_{r.status}"}
                data = await r.json(content_type=None)
                return dict(data) if isinstance(data, Mapping) else {"error": "not_mapping"}
        except Exception as exc:
            return {"error": type(exc).__name__}

async def hydrate_metadata(run: replay.RunData, state: dict[str, Any], e4_model: Mapping[str, Any]) -> None:
    prior = e4_model.get("creator_prior_e4_attempts") or {}
    cache = state.setdefault("metadata_cache", {})
    needed: set[str] = set()
    for rows in run.events_by_mint.values():
        create = next((x for x in rows if event_kind(x) == "CREATE"), None)
        if not create:
            continue
        creator = event_creator(create)
        if integer(prior.get(creator)) < 1:
            continue
        uri = event_uri(create)
        if uri and uri not in cache:
            needed.add(uri)
    if not needed:
        return
    sem = asyncio.Semaphore(24)
    async with aiohttp.ClientSession(headers={"User-Agent": "Gambit-Jr-forward-research/1.0"}) as session:
        rows = await asyncio.gather(*(fetch_one(session, uri, sem) for uri in sorted(needed)))
    for uri, payload in zip(sorted(needed), rows):
        cache[uri] = payload

def prearmed_info(rows: list[dict[str, Any]], create: Mapping[str, Any], e4_model: Mapping[str, Any],
                  cache: Mapping[str, Any]) -> dict[str, Any]:
    raw = create.get("raw") if isinstance(create.get("raw"), Mapping) else {}
    creator = event_creator(create)
    create_ns = integer(create.get("received_ns"))
    prior = integer((e4_model.get("creator_prior_e4_attempts") or {}).get(creator))
    seed = creator_seed(rows, create, creator)
    mayhem = bool(raw.get("is_mayhem_mode"))
    result = {
        "qualifies": False, "creator": creator, "prior_e4_attempts": prior,
        "creator_seed_sol": seed, "mayhem_mode": mayhem, "social_handle": "",
        "status_id": "", "tweet_age_seconds": None, "reason": "",
    }
    if mayhem:
        result["reason"] = "MAYHEM"; return result
    if prior < 1:
        result["reason"] = "NO_PRIOR_E4"; return result
    if seed < 2.0:
        result["reason"] = "SEED_BELOW_2_SOL"; return result
    uri = event_uri(create)
    metadata = cache.get(uri, {}) if uri else {}
    twitter = str(metadata.get("twitter") or metadata.get("x") or "")
    match = STATUS_RE.search(twitter)
    if not match:
        result["reason"] = "NO_STATUS_URL"; return result
    handle, status_id = match.group(1).lower(), match.group(2)
    known = {str(x).lower().lstrip("@") for x in (e4_model.get("creator_handles") or {}).get(creator, [])}
    if handle not in known:
        result["reason"] = "UNSEEN_CREATOR_HANDLE"; return result
    try:
        status_ms = (int(status_id) >> 22) + 1288834974657
    except ValueError:
        result["reason"] = "BAD_STATUS_ID"; return result
    age = (create_ns - status_ms * 1_000_000) / 1e9
    result.update(social_handle=handle, status_id=status_id, tweet_age_seconds=age)
    if not (0.0 <= age <= 10.0):
        result["reason"] = "STATUS_OUTSIDE_0_10S"; return result
    result["qualifies"] = True
    result["reason"] = "PREARMED_E4_FORMULA"
    return result

def prepare_entry(states: list[replay.ReserveState], create_ns: int, decision_ns: int,
                  budget: float, *, e4_guard: Mapping[str, Any] | None = None) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    decision_state = replay.state_at_or_before(states, decision_ns)
    if decision_state is None:
        decision_state = state_at_or_after_exact(states, decision_ns)
    fill_ns = decision_ns + int(ENTRY_LATENCY_MS * 1_000_000)
    fill = state_at_or_after_exact(states, fill_ns)
    create_state = replay.state_at_or_before(states, create_ns) or state_at_or_after_exact(states, create_ns)
    if decision_state is None or fill is None or create_state is None:
        return None, {"reason": "MISSING_RESERVE_STATE", "fee_sol": 0.0}
    curve = buy_curve_for_budget(budget)
    if curve <= 0:
        return None, {"reason": "INSUFFICIENT_CASH", "fee_sol": 0.0}
    expected = buy_tokens(curve, decision_state)
    received = buy_tokens(curve, fill)
    if expected <= 0 or received <= 0:
        return None, {"reason": "NO_FILL", "fee_sol": 0.0}
    output_ratio = received / expected
    if e4_guard is not None:
        min_ratio = finite(e4_guard.get("minimum_entry_output_ratio"), 0.65)
        max_mult = finite(e4_guard.get("maximum_create_to_entry_price_multiple"), 1.5)
        p0, p1 = price(create_state), price(fill)
        price_mult = p1 / p0 if p0 > 0 else float("inf")
        if output_ratio < min_ratio or price_mult > max_mult:
            return None, {"reason": "ENTRY_OUTPUT_GUARD", "fee_sol": BASE_FEE + PRIORITY_FEE,
                          "output_ratio": output_ratio, "price_multiple": price_mult}
    elif output_ratio < 1.0 - GOLDEN_MAX_SLIPPAGE:
        return None, {"reason": "GOLDEN_SLIPPAGE_LIMIT", "fee_sol": BASE_FEE + PRIORITY_FEE,
                      "output_ratio": output_ratio}
    return {
        "decision_state": decision_state, "fill_state": fill, "fill_ns": fill.received_ns,
        "tokens": received, "curve_sol": curve, "entry_cost_sol": entry_cost(curve),
        "output_ratio": output_ratio,
    }, None

def simulate_fixed(states: list[replay.ReserveState], create_ns: int, decision_ns: int,
                   budget: float, horizon_ms: int, e4_guard: Mapping[str, Any] | None = None) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    entry, rej = prepare_entry(states, create_ns, decision_ns, budget, e4_guard=e4_guard)
    if not entry:
        return None, rej
    exit_target = entry["fill_ns"] + horizon_ms * 1_000_000
    exit_state = state_at_or_after_exact(states, exit_target)
    if exit_state is None:
        return None, {"reason": "MISSING_EXIT_STATE", "fee_sol": 0.0}
    proceeds = sell_proceeds(entry["tokens"], exit_state)
    pnl = proceeds - entry["entry_cost_sol"]
    return {
        "fill_ns": entry["fill_ns"], "exit_ns": exit_state.received_ns,
        "entry_cost_sol": entry["entry_cost_sol"], "proceeds_sol": proceeds,
        "pnl_sol": pnl, "win": pnl > 0, "output_ratio": entry["output_ratio"],
        "exit_reason": f"FULL_{horizon_ms}MS",
    }, None

def simulate_e4(states: list[replay.ReserveState], create_ns: int, decision_ns: int,
                budget: float, selector: Mapping[str, Any], policy: Mapping[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    entry, rej = prepare_entry(states, create_ns, decision_ns, budget, e4_guard=selector)
    if not entry:
        return None, rej
    fill = entry["fill_state"]
    fill_price = price(fill)
    if fill_price <= 0:
        return None, {"reason": "BAD_FILL_PRICE", "fee_sol": 0.0}
    remaining = entry["tokens"]
    original = remaining
    proceeds = 0.0
    first_done = False
    peak = 1.0
    deadline = entry["fill_ns"] + integer(policy.get("hold_ms"), 2000) * 1_000_000
    stop = finite(policy.get("stop"), 0.7)
    first_take = finite(policy.get("first_take"), 1.15)
    first_fraction = finite(policy.get("first_fraction"), 0.3)
    trail = finite(policy.get("trail_retrace"), 0.25)
    last = fill
    reason = "MAXIMUM_HOLD"
    for st in states:
        if st.received_ns <= entry["fill_ns"]:
            continue
        if st.received_ns > deadline:
            break
        p = price(st)
        if p <= 0:
            continue
        last = st
        mult = p / fill_price
        peak = max(peak, mult)
        if not first_done and first_fraction > 0 and mult >= first_take:
            amount = min(remaining, original * first_fraction)
            proceeds += sell_proceeds(amount, st)
            remaining -= amount
            first_done = True
        floor = stop
        if first_done:
            floor = max(floor, 1.02, peak * (1.0 - trail))
        elif peak >= first_take:
            floor = max(floor, peak * (1.0 - trail))
        if mult <= floor:
            reason = "TRAILING_OR_STOP"
            break
    final_state = last if last.received_ns >= entry["fill_ns"] else state_at_or_after_exact(states, deadline)
    if final_state is None:
        return None, {"reason": "MISSING_EXIT_STATE", "fee_sol": 0.0}
    if remaining > 0:
        proceeds += sell_proceeds(remaining, final_state)
    pnl = proceeds - entry["entry_cost_sol"]
    return {
        "fill_ns": entry["fill_ns"], "exit_ns": final_state.received_ns,
        "entry_cost_sol": entry["entry_cost_sol"], "proceeds_sol": proceeds,
        "pnl_sol": pnl, "win": pnl > 0, "output_ratio": entry["output_ratio"],
        "exit_reason": reason,
    }, None

def simulate_v21(rows: list[dict[str, Any]], states: list[replay.ReserveState], create_ns: int,
                 decision_ns: int, budget: float, conv: Mapping[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    entry, rej = prepare_entry(states, create_ns, decision_ns, budget)
    if not entry:
        return None, rej
    tier = str(conv.get("conviction_tier") or "BASE")
    cfg = v2.TIERS[tier]
    tracker = v21.FlowTracker()
    guardian = v21.FlowAwareGuardian(cfg, tracker)
    original = float(entry["tokens"])
    remaining = original
    proceeds = 0.0
    fill_ns = integer(entry["fill_ns"])
    max_ns = fill_ns + int(cfg.max_hold_ms * 1_000_000)
    raw_events = [r for r in rows if fill_ns < integer(r.get("received_ns")) <= max_ns]
    raw_events.sort(key=lambda r: (integer(r.get("received_ns")), integer(r.get("__sequence"), -1)))
    by_time: dict[int, list[dict[str, Any]]] = {}
    for row in raw_events:
        by_time.setdefault(integer(row.get("received_ns")), []).append(row)
    timeline = set(by_time)
    timeline.add(fill_ns + int(cfg.initial_target_ms * 1_000_000))
    t = 2000.0
    while t <= cfg.max_hold_ms:
        timeline.add(fill_ns + int(t * 1_000_000))
        t += 250.0
    closed = False
    exit_ns = fill_ns
    exit_reason = ""
    actions: list[dict[str, Any]] = []
    for ns in sorted(x for x in timeline if x <= max_ns):
        st = replay.state_at_or_before(states, ns) or state_at_or_after_exact(states, ns)
        if st is None:
            continue
        full_net = sell_proceeds(original, st)
        ret = full_net / entry["entry_cost_sol"] - 1.0
        for row in by_time.get(ns, []):
            kind = event_kind(row)
            if kind not in {"BUY", "SELL", "PUMPSWAP_BUY", "PUMPSWAP_SELL"}:
                continue
            raw = row.get("raw") if isinstance(row.get("raw"), Mapping) else {}
            sol_amount = finite(row.get("sol_amount"))
            if sol_amount <= 0:
                sol_amount = core.normal_sol(raw.get("sol_amount"))
            tok_amount = finite(row.get("token_amount"))
            if tok_amount <= 0:
                tok_amount = core.normal_tokens(raw.get("token_amount"))
            trader = str(row.get("trader") or "")
            tracker.ingest(v21.FlowEvent(
                t_ms=(ns - fill_ns) / 1e6, kind=kind, trader=trader,
                sol_amount=max(0.0, sol_amount), token_amount=max(0.0, tok_amount),
                return_fraction=ret, is_creator=(trader == event_creator(next((x for x in rows if event_kind(x)=="CREATE"), {}))),
                vsol=st.virtual_sol, vtok=st.virtual_tokens, views=None,
            ))
        mark = v2.Mark((ns - fill_ns) / 1e6, ret, ns, st.virtual_sol, st.virtual_tokens)
        action = guardian.on_mark(mark)
        if action is None:
            continue
        amount = remaining if action.kind == "EXIT" else min(remaining, original * float(action.fraction_of_entry))
        if amount <= 0:
            continue
        sale = sell_proceeds(amount, st)
        proceeds += sale
        remaining -= amount
        actions.append({"kind": action.kind, "reason": action.reason, "t_ms": action.t_ms,
                        "fraction": amount / original if original else 0.0, "proceeds_sol": sale})
        if action.kind == "EXIT" or remaining <= original * 1e-9:
            closed = True
            exit_ns = st.received_ns
            exit_reason = action.reason
            break
    if not closed:
        st = replay.state_at_or_before(states, max_ns) or state_at_or_after_exact(states, max_ns)
        if st is None:
            return None, {"reason": "MISSING_EXIT_STATE", "fee_sol": 0.0}
        if remaining > 0:
            proceeds += sell_proceeds(remaining, st)
        exit_ns = st.received_ns
        exit_reason = "MAX_RUNNER_HOLD_SAFETY_CEILING"
    pnl = proceeds - entry["entry_cost_sol"]
    return {
        "fill_ns": fill_ns, "exit_ns": exit_ns, "entry_cost_sol": entry["entry_cost_sol"],
        "proceeds_sol": proceeds, "pnl_sol": pnl, "win": pnl > 0,
        "output_ratio": entry["output_ratio"], "exit_reason": exit_reason, "actions": actions,
    }, None

class Account:
    def __init__(self, row: dict[str, Any], target: int):
        self.row = row
        self.target = target
        self.cash = finite(row.get("cash_sol"), STARTING_SOL)
        self.ledger = list(row.get("ledger") or [])
        self.rejections = Counter(row.get("rejections") or {})
        self.active: list[tuple[int, float]] = []

    def settle(self, now_ns: int) -> None:
        keep = []
        for exit_ns, proceeds in self.active:
            if exit_ns <= now_ns:
                self.cash += proceeds
            else:
                keep.append((exit_ns, proceeds))
        self.active = keep

    def flush(self) -> None:
        for _, proceeds in self.active:
            self.cash += proceeds
        self.active.clear()

    def can_enter(self) -> bool:
        return len(self.ledger) < self.target and len(self.active) < MAX_CONCURRENT

    def budget(self, fraction: float) -> float:
        return min(max(0.0, self.cash - RESERVE_SOL), max(0.0, self.cash * fraction))

    def failure(self, reason: str, fee: float) -> None:
        self.rejections[reason] += 1
        self.cash = max(0.0, self.cash - max(0.0, fee))

    def add(self, row: dict[str, Any]) -> None:
        self.cash -= finite(row["entry_cost_sol"])
        self.active.append((integer(row["exit_ns"]), finite(row["proceeds_sol"])))
        projected = self.cash + sum(p for _, p in self.active)
        row["balance_after_sol"] = projected
        self.ledger.append(row)

    def save(self) -> None:
        self.row["cash_sol"] = self.cash
        self.row["ledger"] = self.ledger
        self.row["rejections"] = dict(self.rejections)

async def process(args: argparse.Namespace) -> int:
    import hashlib
    e4_bytes = args.e4_model.read_bytes()
    e4_hash = hashlib.sha256(e4_bytes).hexdigest()
    e4_model = json.loads(e4_bytes)
    library = json.loads(args.library_runtime.read_text(encoding="utf-8"))
    library_fingerprint = hashlib.sha256(args.library_runtime.read_bytes()).hexdigest()
    state = load_state(args.state, e4_hash, library_fingerprint)
    if args.run_id in state["processed_runs"]:
        raise RuntimeError("run already processed")
    run = replay.load_run(args.run_id, args.batch, args.events)
    await hydrate_metadata(run, state, e4_model)

    rep = core.initialise_reputation_from_corpus(args.corpus)
    delta = state.setdefault("reputation_delta", {})
    for wallet, pair in delta.items():
        rep.appear[wallet] += integer(pair[0]); rep.wins[wallet] += integer(pair[1])

    evidence = v21.PriorEvidenceBook.from_frozen_corpus(args.corpus)
    accounts = {name: Account(state["models"][name], args.target) for name in MODELS}
    pending_rep: list[tuple[int, str, list[str], bool]] = []
    audit = Counter()

    launches: list[tuple[int, str, dict[str, Any], list[dict[str, Any]], list[replay.ReserveState]]] = []
    for mint, rows in run.events_by_mint.items():
        create = next((x for x in rows if event_kind(x) == "CREATE"), None)
        if create is None:
            continue
        ns = integer(create.get("received_ns"))
        if ns <= integer(e4_model.get("maximum_evidence_ns")):
            raise RuntimeError("captured launch overlaps E4 frozen evidence epoch")
        launches.append((ns, mint, create, rows, run.reserves_by_mint.get(mint, [])))
    launches.sort()

    for create_ns, mint, create, rows, states in launches:
        decision_ns = create_ns + core.DECISION_MS * 1_000_000
        while pending_rep and pending_rep[0][0] <= decision_ns:
            _, _, buyers0, won0 = heapq.heappop(pending_rep)
            rep.update(buyers0, won0)
            for w in buyers0:
                pair = delta.setdefault(w, [0, 0]); pair[0] += 1; pair[1] += int(won0)

        creator = event_creator(create)
        buyers = early_buyers(rows, creator, create_ns)
        qualifies, n, w, rate = rep.qualifies(buyers)
        create_state = replay.state_at_or_before(states, create_ns) or state_at_or_after_exact(states, create_ns)
        decision_state = replay.state_at_or_before(states, decision_ns) or state_at_or_after_exact(states, decision_ns)
        early_delta = (decision_state.virtual_sol - create_state.virtual_sol) if decision_state and create_state else 0.0
        decision = {
            "mint": mint, "creator": creator, "create_ns": create_ns, "decision_ns": decision_ns,
            "buyers": buyers, "buyer_count": len(buyers), "prior_appearances": n,
            "prior_wins": w, "prior_win_rate": rate, "early_vsol_delta": early_delta,
        }
        current_conv = v2.conviction(decision)
        enhanced = v21.enhanced_conviction(decision, evidence)
        lib = lib_stats(library, creator)
        lib_current = library_adjusted_conviction(current_conv, lib)
        lib_enhanced = library_adjusted_conviction(enhanced, lib)
        prearm = prearmed_info(rows, create, e4_model, state.get("metadata_cache") or {})
        prearm_conv = e4_conviction(integer(prearm.get("prior_e4_attempts")), finite(prearm.get("creator_seed_sol")), lib)

        selections = {
            "FULL_3S": bool(qualifies),
            "FULL_4S": bool(qualifies),
            "FULL_3S_V2_1": bool(qualifies),
            "FULL_3S_LIBRARY": bool(qualifies and not lib["strong_negative"]),
            "FULL_3S_V2_1_LIBRARY": bool(qualifies and not lib["strong_negative"]),
            "E4_PREARMED": bool(prearm["qualifies"]),
            "E4_PREARMED_CONVICTION_LIBRARY": bool(prearm["qualifies"]),
        }
        audit["launches"] += 1
        audit["golden_qualified"] += int(qualifies)
        audit["e4_prearmed_qualified"] += int(prearm["qualifies"])
        audit["library_positive"] += int(lib["positive"])
        audit["library_strong_negative"] += int(lib["strong_negative"])

        for name, selected in selections.items():
            acc = accounts[name]
            acc.settle(decision_ns if not name.startswith("E4_") else create_ns)
            if not selected or len(acc.ledger) >= args.target:
                continue
            if not acc.can_enter():
                acc.rejections["CONCURRENCY"] += 1
                continue
            if name == "FULL_3S":
                conv, kind = current_conv, "fixed3"
            elif name == "FULL_4S":
                conv, kind = current_conv, "fixed4"
            elif name == "FULL_3S_V2_1":
                conv, kind = enhanced, "v21"
            elif name == "FULL_3S_LIBRARY":
                conv, kind = lib_current, "fixed3"
            elif name == "FULL_3S_V2_1_LIBRARY":
                conv, kind = lib_enhanced, "v21"
            elif name == "E4_PREARMED":
                conv = {"conviction_tier": "FIXED_10PCT", "position_fraction": 0.10, "conviction_score": None}
                kind = "e4"
            else:
                conv, kind = prearm_conv, "e4"

            fraction = finite(conv.get("position_fraction"), 0.05)
            budget = acc.budget(fraction)
            if budget <= BUY_FIXED + 1e-6:
                acc.rejections["BANKROLL_TOO_LOW"] += 1
                continue
            model_decision_ns = create_ns if name.startswith("E4_") else decision_ns
            if kind == "fixed3":
                trade, rej = simulate_fixed(states, create_ns, model_decision_ns, budget, 3000)
            elif kind == "fixed4":
                trade, rej = simulate_fixed(states, create_ns, model_decision_ns, budget, 4000)
            elif kind == "v21":
                trade, rej = simulate_v21(rows, states, create_ns, model_decision_ns, budget, conv)
            else:
                trade, rej = simulate_e4(states, create_ns, model_decision_ns, budget,
                                          e4_model["selector"], e4_model["exit_policy"])
            if trade is None:
                acc.failure(str((rej or {}).get("reason") or "UNKNOWN_REJECTION"), finite((rej or {}).get("fee_sol")))
                continue
            trade.update({
                "model": name, "mint": mint, "creator": creator, "decision_ns": model_decision_ns,
                "position_fraction": fraction, "conviction": conv,
                "library_stats_at_decision": lib if "LIBRARY" in name else None,
                "prearmed_e4": prearm if name.startswith("E4_") else None,
                "paper_only": True, "real_money_execution": False,
                "common_added_entry_latency_ms": ENTRY_LATENCY_MS,
            })
            acc.add(trade)

        outcome = reputation_outcome(states, create_ns)
        if outcome is not None and buyers:
            heapq.heappush(pending_rep, (create_ns + (core.DECISION_MS + core.HOLD_MS) * 1_000_000, mint, buyers, outcome))

    while pending_rep:
        _, _, buyers0, won0 = heapq.heappop(pending_rep)
        rep.update(buyers0, won0)
        for wallet in buyers0:
            pair = delta.setdefault(wallet, [0, 0]); pair[0] += 1; pair[1] += int(won0)

    for acc in accounts.values():
        acc.flush()
        acc.save()

    state["processed_runs"].append(args.run_id)
    state["updated_ns"] = time.time_ns()
    state["last_audit_counts"] = dict(audit)
    state["metadata_cache"] = dict(list((state.get("metadata_cache") or {}).items())[-5000:])
    counts = {name: len(state["models"][name]["ledger"]) for name in MODELS}
    complete = all(v >= args.target for v in counts.values())
    status = {
        "version": VERSION, "status": "TARGET_REACHED" if complete else "COLLECTING",
        "paper_only": True, "real_money_execution": False,
        "market_data": "STRICTLY_FUTURE_LIVE_SOLANA_CAPTURE_REPLAYED_CAUSALLY",
        "social_evidence": "immutable launch metadata/status timestamp; live X transport latency not claimed",
        "models": list(MODELS), "target_per_model": args.target,
        "closed_trades": counts, "minimum_closed": min(counts.values()), "complete": complete,
        "entry_latency_ms_common": ENTRY_LATENCY_MS,
        "audit_counts": dict(audit),
        "results": {name: metrics(state["models"][name]["ledger"]) for name in MODELS},
        "checked_ns": time.time_ns(), "processed_runs": list(state["processed_runs"]),
    }
    atomic_json(args.state, state)
    atomic_json(args.status, status)
    print(json.dumps(status, sort_keys=True))
    return 0

def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--run-id", required=True)
    p.add_argument("--batch", type=Path, required=True)
    p.add_argument("--events", type=Path, required=True)
    p.add_argument("--corpus", type=Path, required=True)
    p.add_argument("--library-runtime", type=Path, required=True)
    p.add_argument("--e4-model", type=Path, required=True)
    p.add_argument("--state", type=Path, required=True)
    p.add_argument("--status", type=Path, required=True)
    p.add_argument("--target", type=int, default=100)
    a = p.parse_args()
    if a.target != 100:
        p.error("target is frozen at 100")
    return asyncio.run(process(a))

if __name__ == "__main__":
    raise SystemExit(main())
