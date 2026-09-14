#!/usr/bin/env python3
"""Frozen standalone Golden Thesis candidate: early-buyer reputation.

No E4 activity is used as an entry signal. The E4 wallet is explicitly excluded
from early-buyer sets so the rule is independently reproducible.
"""
from __future__ import annotations

import gzip, hashlib, heapq, json, math
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

VERSION = "golden-buyer-reputation-v1"
CORPUS_SHA256 = "6f41376cfee3d54d57774b4368ec8b50c9e59becbbd04650cf56a20ef48dce6c"
E4_WALLET = "E4EzXdwf7NNdqM2XGswWaWHfxgucVCo24PTCcrimTKBz"
WSOL_MINT = "So11111111111111111111111111111111111111112"
SYSTEM_MINT = "11111111111111111111111111111111"
DECISION_MS = 10
HOLD_MS = 2000
MIN_APPEARANCES = 10
MIN_PRIOR_WIN_RATE = 0.70
STARTING_BALANCE_SOL = 2.0
POSITION_FRACTION = 0.0185
MAX_CONCURRENT = 2
RESERVE_SOL = 0.03
BENCHMARK_STAKE_SOL = 0.0555
PUMP_FEE_BPS = 125
PRIORITY_AND_TIP_SOL = 0.00015


def finite(value: Any, default: float = 0.0) -> float:
    try:
        x = float(value)
    except (TypeError, ValueError):
        return default
    return x if math.isfinite(x) else default


def integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def sha256_path(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def normal_sol(value: Any) -> float:
    amount = finite(value)
    return amount / 1_000_000_000.0 if amount >= 1_000_000.0 else amount


def normal_tokens(value: Any) -> float:
    amount = finite(value)
    return amount / 1_000_000.0 if amount >= 10_000_000_000.0 else amount


def native_quote(raw: Mapping[str, Any]) -> bool:
    q = str(raw.get("quote_mint") or "")
    return q in ("", WSOL_MINT, SYSTEM_MINT)


def event_ns(event: Any) -> int:
    return integer(event.get("received_ns") if isinstance(event, Mapping) else getattr(event, "received_ns", 0))


def event_kind(event: Any) -> str:
    return str(event.get("kind", "") if isinstance(event, Mapping) else getattr(event, "kind", ""))


def event_mint(event: Any) -> str:
    return str(event.get("mint", "") if isinstance(event, Mapping) else getattr(event, "mint", ""))


def event_trader(event: Any) -> str:
    return str(event.get("trader", "") if isinstance(event, Mapping) else (getattr(event, "trader", None) or ""))


def event_creator(event: Any) -> str:
    if isinstance(event, Mapping):
        raw = event.get("raw") if isinstance(event.get("raw"), Mapping) else {}
        return str(event.get("creator") or raw.get("creator") or raw.get("user") or "")
    raw = getattr(event, "raw", {}) if isinstance(getattr(event, "raw", {}), Mapping) else {}
    return str(getattr(event, "creator", None) or raw.get("creator") or raw.get("user") or "")


def state_from_event(event: Any) -> dict[str, Any] | None:
    raw = event.get("raw") if isinstance(event, Mapping) else getattr(event, "raw", None)
    raw = raw if isinstance(raw, Mapping) else {}
    if not native_quote(raw):
        return None
    complete = bool(event.get("complete", False) if isinstance(event, Mapping) else getattr(event, "complete", False))
    kind = event_kind(event)
    if complete or kind == "MIGRATION":
        return {"ns": event_ns(event), "complete": True, "vsol": 0.0, "vtok": 0.0, "rtok": 0.0}
    vsol = normal_sol(raw.get("virtual_sol_reserves", raw.get("virtual_quote_reserves")))
    vtok = normal_tokens(raw.get("virtual_token_reserves"))
    rtok = normal_tokens(raw.get("real_token_reserves"))
    if vsol <= 0 or vtok <= 0:
        return None
    return {"ns": event_ns(event), "complete": False, "vsol": vsol, "vtok": vtok, "rtok": rtok if rtok > 0 else float("inf")}


def quote_buy(total_budget_sol: float, state: Mapping[str, Any]) -> tuple[float, float]:
    budget = max(0.0, total_budget_sol - PRIORITY_AND_TIP_SOL)
    curve = budget / (1.0 + PUMP_FEE_BPS / 10_000.0)
    vsol = finite(state.get("vsol")); vtok = finite(state.get("vtok")); rtok = finite(state.get("rtok"), float("inf"))
    if curve <= 0 or vsol <= 0 or vtok <= 0 or state.get("complete"):
        return 0.0, 0.0
    tokens = curve * vtok / (vsol + curve)
    tokens = min(tokens, rtok)
    return max(0.0, tokens), curve


def quote_sell(tokens: float, state: Mapping[str, Any]) -> float:
    vsol = finite(state.get("vsol")); vtok = finite(state.get("vtok"))
    if tokens <= 0 or vsol <= 0 or vtok <= 0 or state.get("complete"):
        return 0.0
    gross = tokens * vsol / (vtok + tokens)
    return max(0.0, gross * (1.0 - PUMP_FEE_BPS / 10_000.0) - PRIORITY_AND_TIP_SOL)


def unique_early_buyers(events: Sequence[Any], creator: str, create_ns: int, horizon_ms: int = DECISION_MS) -> list[str]:
    cutoff = create_ns + horizon_ms * 1_000_000
    buyers: list[str] = []
    for e in events:
        if event_ns(e) > cutoff or event_kind(e) not in {"BUY", "PUMPSWAP_BUY"}:
            continue
        wallet = event_trader(e)
        if not wallet or wallet == creator or wallet == E4_WALLET or wallet in buyers:
            continue
        buyers.append(wallet)
    return buyers


class Reputation:
    def __init__(self, stats: Mapping[str, Sequence[int]] | None = None):
        self.appear = Counter(); self.wins = Counter()
        if stats:
            for wallet, pair in stats.items():
                self.appear[str(wallet)] = int(pair[0]); self.wins[str(wallet)] = int(pair[1])

    def aggregate(self, buyers: Iterable[str]) -> tuple[int, int, float | None]:
        unique = list(dict.fromkeys(str(x) for x in buyers if x))
        n = sum(self.appear[x] for x in unique); w = sum(self.wins[x] for x in unique)
        return n, w, (w / n if n else None)

    def qualifies(self, buyers: Iterable[str]) -> tuple[bool, int, int, float | None]:
        n, w, rate = self.aggregate(buyers)
        return n >= MIN_APPEARANCES and rate is not None and rate >= MIN_PRIOR_WIN_RATE, n, w, rate

    def update(self, buyers: Iterable[str], won: bool) -> None:
        for wallet in dict.fromkeys(str(x) for x in buyers if x):
            self.appear[wallet] += 1; self.wins[wallet] += int(won)

    def serialise(self) -> dict[str, list[int]]:
        return {wallet: [self.appear[wallet], self.wins[wallet]] for wallet in self.appear}


def initialise_reputation_from_corpus(path: Path) -> Reputation:
    if sha256_path(path) != CORPUS_SHA256:
        raise RuntimeError("frozen corpus SHA mismatch")
    rep = Reputation(); pending: list[tuple[int, str, list[str], bool]] = []
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            row = json.loads(line); decision = integer(row["create_ns"]) + DECISION_MS * 1_000_000
            while pending and pending[0][0] <= decision:
                _, _, buyers, won = heapq.heappop(pending); rep.update(buyers, won)
            creator = str(row.get("creator") or ""); buyers: list[str] = []
            for wallet in row.get("first_outside_buyers_10ms") or []:
                wallet = str(wallet or "")
                if wallet and wallet != creator and wallet != E4_WALLET and wallet not in buyers:
                    buyers.append(wallet)
            won = finite(row.get("paper_10ms_hold_2000ms_pnl_sol")) > 0
            settle = integer(row["create_ns"]) + (DECISION_MS + HOLD_MS) * 1_000_000
            heapq.heappush(pending, (settle, str(row.get("mint") or ""), buyers, won))
    while pending:
        _, _, buyers, won = heapq.heappop(pending); rep.update(buyers, won)
    return rep


def summary(ledger: Sequence[Mapping[str, Any]], balance: float) -> dict[str, Any]:
    pnls = [finite(row.get("pnl_sol")) for row in ledger if row.get("status") == "CLOSED"]
    wins = sum(x > 0 for x in pnls); gains = sum(x for x in pnls if x > 0); losses = -sum(x for x in pnls if x < 0)
    peak = STARTING_BALANCE_SOL; drawdown = 0.0
    for row in ledger:
        if row.get("status") != "CLOSED":
            continue
        b = finite(row.get("balance_after_sol"), peak); peak = max(peak, b); drawdown = max(drawdown, (peak - b) / peak if peak else 0)
    largest = max((x for x in pnls if x > 0), default=0); gross = sum(x for x in pnls if x > 0)
    return {"closed_trades": len(pnls), "wins": wins, "losses": len(pnls) - wins,
            "win_rate": wins / len(pnls) if pnls else None, "net_pnl_sol": sum(pnls), "ending_balance_sol": balance,
            "profit_factor": gains / losses if losses else (999.0 if gains else None),
            "expectancy_sol": sum(pnls) / len(pnls) if pnls else None, "maximum_drawdown_fraction": drawdown,
            "largest_winner_profit_share": largest / gross if gross else None}
