#!/usr/bin/env python3
from __future__ import annotations

import argparse
import itertools
import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

E4_WALLET = "E4EzXdwf7NNdqM2XGswWaWHfxgucVCo24PTCcrimTKBz"


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
        rate = finite(row.get("gross_win_rate"), wins / trades if trades else 0.0)
        output[creator] = {"wins": wins, "losses": losses, "trades": trades, "rate": rate}
    return output


def outcomes(batch_path: Path) -> dict[str, bool]:
    data = json.loads(batch_path.read_text(encoding="utf-8"))
    return {
        str(row.get("mint") or ""): finite(row.get("pnl_sol")) > 0
        for row in (data.get("actual_e4_fresh_sample") or {}).get("positions") or []
        if str(row.get("mint") or "")
    }


def load_events(path: Path) -> list[dict[str, Any]]:
    rows = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            if row.get("mint") and row.get("received_ns"):
                rows.append(row)
    rows.sort(key=lambda row: (
        integer(row.get("received_ns")), integer(row.get("slot")), integer(row.get("event_index"))
    ))
    return rows


@dataclass(frozen=True)
class Rule:
    identity: str
    min_seed_sol: float
    min_buyers: int
    min_same_slot_buys: int
    max_buy_count: int
    max_age_ms: float
    min_fdv: float
    max_fdv: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "identity": self.identity,
            "min_seed_sol": self.min_seed_sol,
            "min_buyers": self.min_buyers,
            "min_same_slot_buys": self.min_same_slot_buys,
            "max_buy_count": self.max_buy_count,
            "max_age_ms": self.max_age_ms,
            "min_fdv": self.min_fdv,
            "max_fdv": self.max_fdv,
        }


@dataclass
class Launch:
    mint: str
    creator: str
    e4_selected: bool
    e4_won: bool
    snapshots: list[dict[str, Any]]


def identity_flags(creator: str, h: Mapping[str, Any], prior_creator: int, prior_buyer_hits: int) -> dict[str, bool]:
    wins = integer(h.get("wins"))
    trades = integer(h.get("trades"))
    rate = finite(h.get("rate"))
    any_prior = wins >= 1
    elite = trades >= 5 and wins >= 4 and rate >= 0.80
    ultra = trades >= 8 and wins >= 7 and rate >= 0.85
    return {
        "WHITELIST_ANY": any_prior,
        "WHITELIST_ELITE": elite,
        "WHITELIST_ULTRA": ultra,
        "PRIOR_E4_CREATOR": prior_creator >= 1,
        "PRIOR_E4_CREATOR_2": prior_creator >= 2,
        "PRIOR_E4_BUYER": prior_buyer_hits >= 1,
        "PRIOR_E4_BUYER_2": prior_buyer_hits >= 2,
        "ELITE_OR_PRIOR_CREATOR": elite or prior_creator >= 1,
        "ULTRA_OR_PRIOR_CREATOR_2": ultra or prior_creator >= 2,
        "HISTORY_AND_BUYER": any_prior and prior_buyer_hits >= 1,
        "ELITE_AND_FLOW_ID": elite and (prior_creator >= 1 or prior_buyer_hits >= 1),
        "CREATOR_OR_BUYER": prior_creator >= 1 or prior_buyer_hits >= 1,
    }


def build_launches(
    pairs: list[tuple[Path, Path]],
    history: Mapping[str, Mapping[str, Any]],
) -> list[list[Launch]]:
    prior_creators: dict[str, int] = {}
    prior_buyers: dict[str, int] = {}
    all_batches: list[list[Launch]] = []

    for batch_path, events_path in pairs:
        result_outcomes = outcomes(batch_path)
        rows = load_events(events_path)
        selected_mints = {
            str(row.get("mint") or "")
            for row in rows
            if str(row.get("trader") or "") == E4_WALLET
            and str(row.get("kind") or "").upper() in {"BUY", "PUMPSWAP_BUY"}
        }
        states: dict[str, dict[str, Any]] = {}
        records: dict[str, Launch] = {}

        for row in rows:
            mint = str(row.get("mint") or "")
            kind = str(row.get("kind") or "").upper()
            trader = str(row.get("trader") or "")
            now_ns = integer(row.get("received_ns"))
            slot = integer(row.get("slot"))
            raw = row.get("raw") if isinstance(row.get("raw"), Mapping) else {}

            if kind == "CREATE":
                creator = str(row.get("creator") or raw.get("creator") or trader or "")
                price = finite(row.get("price_sol"))
                fdv = finite(row.get("fdv_usd") or raw.get("usd_market_cap") or raw.get("fdv_usd"))
                states[mint] = {
                    "creator": creator,
                    "created_ns": now_ns,
                    "create_slot": slot,
                    "last_slot": slot,
                    "initial_price": price,
                    "price": price,
                    "fdv": fdv,
                    "seed": 0.0,
                    "buy_count": 0,
                    "sell_count": 0,
                    "buyers": set(),
                    "first_buyers": [],
                    "buy_slots": Counter(),
                }
                records[mint] = Launch(mint, creator, mint in selected_mints, bool(result_outcomes.get(mint, False)), [])
                continue

            state = states.get(mint)
            record = records.get(mint)
            if state is None or record is None:
                continue

            is_e4_buy = trader == E4_WALLET and kind in {"BUY", "PUMPSWAP_BUY"}
            if is_e4_buy:
                creator = str(state.get("creator") or "")
                for buyer in state.get("first_buyers") or ():
                    prior_buyers[buyer] = prior_buyers.get(buyer, 0) + 1
                if creator:
                    prior_creators[creator] = prior_creators.get(creator, 0) + 1
                continue

            if kind in {"SELL", "PUMPSWAP_SELL"}:
                state["sell_count"] += 1
                continue
            if kind not in {"BUY", "PUMPSWAP_BUY"}:
                continue

            sol = max(0.0, finite(row.get("sol_amount")))
            fdv = finite(row.get("fdv_usd") or raw.get("usd_market_cap") or raw.get("fdv_usd"))
            price = finite(row.get("price_sol"))
            if fdv > 0:
                state["fdv"] = fdv
            if price > 0:
                state["price"] = price
            state["last_slot"] = slot
            state["buy_count"] += 1
            state["buy_slots"][slot] += 1
            creator = str(state.get("creator") or "")
            if trader and trader == creator:
                state["seed"] += sol
            elif trader:
                state["buyers"].add(trader)
                if trader not in state["first_buyers"] and len(state["first_buyers"]) < 8:
                    state["first_buyers"].append(trader)

            first_buyers = list(state.get("first_buyers") or [])
            buyer_hits = sum(1 for buyer in first_buyers if prior_buyers.get(buyer, 0) > 0)
            h = dict(history.get(creator) or {})
            flags = identity_flags(creator, h, prior_creators.get(creator, 0), buyer_hits)
            p0 = finite(state.get("initial_price"))
            p = finite(state.get("price"))
            record.snapshots.append({
                "received_ns": now_ns,
                "age_ms": max(0.0, (now_ns - integer(state.get("created_ns"))) / 1e6),
                "fdv": finite(state.get("fdv")),
                "seed": finite(state.get("seed")),
                "buy_count": integer(state.get("buy_count")),
                "sell_count": integer(state.get("sell_count")),
                "buyers": len(state.get("buyers") or ()),
                "same_slot_buys": integer((state.get("buy_slots") or {}).get(slot, 0)),
                "price_multiple": p / p0 if p0 > 0 and p > 0 else 1.0,
                "prior_creator": integer(prior_creators.get(creator, 0)),
                "prior_buyer_hits": buyer_hits,
                "history_wins": integer(h.get("wins")),
                "history_trades": integer(h.get("trades")),
                "history_rate": finite(h.get("rate")),
                "identity": flags,
            })

        all_batches.append(list(records.values()))
    return all_batches


def trigger(launch: Launch, rule: Rule) -> dict[str, Any] | None:
    for snap in launch.snapshots:
        if snap["sell_count"] > 0:
            return None
        if not bool((snap.get("identity") or {}).get(rule.identity)):
            continue
        if snap["seed"] < rule.min_seed_sol:
            continue
        if snap["buyers"] < rule.min_buyers:
            continue
        if snap["same_slot_buys"] < rule.min_same_slot_buys:
            continue
        if snap["buy_count"] > rule.max_buy_count:
            continue
        if snap["age_ms"] > rule.max_age_ms:
            continue
        if not (rule.min_fdv <= snap["fdv"] <= rule.max_fdv):
            continue
        return snap
    return None


def metrics(launches: list[Launch], rule: Rule) -> dict[str, Any]:
    selected = [row for row in launches if row.e4_selected]
    winners = [row for row in selected if row.e4_won]
    candidates = [(row, trigger(row, rule)) for row in launches]
    candidates = [(row, snap) for row, snap in candidates if snap is not None]
    true = [row for row, _ in candidates if row.e4_selected]
    true_winners = [row for row in true if row.e4_won]
    return {
        "launches": len(launches),
        "e4_entries": len(selected),
        "e4_winners": len(winners),
        "candidates": len(candidates),
        "true_e4": len(true),
        "true_e4_winners": len(true_winners),
        "false_positives": len(candidates) - len(true),
        "precision": len(true) / len(candidates) if candidates else 0.0,
        "recall": len(true) / len(selected) if selected else 0.0,
        "winner_recall": len(true_winners) / len(winners) if winners else 0.0,
    }


def rules():
    identities = (
        "WHITELIST_ELITE", "WHITELIST_ULTRA", "PRIOR_E4_CREATOR", "PRIOR_E4_CREATOR_2",
        "PRIOR_E4_BUYER", "PRIOR_E4_BUYER_2", "ELITE_OR_PRIOR_CREATOR",
        "ULTRA_OR_PRIOR_CREATOR_2", "HISTORY_AND_BUYER", "ELITE_AND_FLOW_ID", "CREATOR_OR_BUYER",
    )
    for values in itertools.product(
        identities,
        (0.25, 0.5, 1.0, 2.0, 3.0),
        (0, 1, 2),
        (1, 2, 3),
        (2, 3, 4, 5),
        (80.0, 150.0, 300.0, 500.0),
        (2800.0, 3200.0, 3500.0),
        (7000.0, 8500.0, 10_000.0),
    ):
        yield Rule(*values)


def objective(row: Mapping[str, Any]) -> tuple[float, float, float, float, float]:
    recall = finite(row.get("recall"))
    winner_recall = finite(row.get("winner_recall"))
    precision = finite(row.get("precision"))
    candidates = finite(row.get("candidates"))
    valid = 1.0 if recall >= 0.10 and winner_recall >= 0.10 else 0.0
    return (valid, precision, winner_recall, recall, -candidates)


def main() -> int:
    parser = argparse.ArgumentParser(description="Causal E4 identity + exact launch-flow preimpact learner")
    parser.add_argument("--train", action="append", default=[], metavar="BATCH:EVENTS")
    parser.add_argument("--holdout", action="append", default=[], metavar="BATCH:EVENTS")
    parser.add_argument("--history", type=Path, default=Path("models/e4/e4-creator-expectancy.json"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    pairs = []
    labels = []
    for label, items in (("train", args.train), ("holdout", args.holdout)):
        for item in items:
            batch, events = item.split(":", 1)
            pairs.append((Path(batch), Path(events)))
            labels.append(label)
    history = history_map(args.history)
    batches = build_launches(pairs, history)
    train = [row for label, batch in zip(labels, batches) if label == "train" for row in batch]
    holdout = [row for label, batch in zip(labels, batches) if label == "holdout" for row in batch]

    ranked = []
    for rule in rules():
        train_metrics = metrics(train, rule)
        ranked.append((objective(train_metrics), rule, train_metrics))
    ranked.sort(key=lambda row: row[0], reverse=True)
    _, best, train_metrics = ranked[0]
    holdout_metrics = metrics(holdout, best)
    payload = {
        "version": "e4-v12-identity-preimpact-v1",
        "methodology": {
            "causal": True,
            "e4_buy_used_as_entry_feature": False,
            "static_history": "creator whitelist snapshot already present before frozen captures",
            "rolling_identity": "prior E4 selected creators and early buyers are only added after earlier E4 entries",
            "train_launches": len(train),
            "holdout_launches": len(holdout),
        },
        "rule": best.as_dict(),
        "train": train_metrics,
        "holdout": holdout_metrics,
        "safe_to_authorize": bool(
            holdout_metrics["precision"] >= 0.40
            and holdout_metrics["recall"] >= 0.10
            and holdout_metrics["winner_recall"] >= 0.10
            and holdout_metrics["candidates"] <= max(10, holdout_metrics["e4_entries"])
        ),
        "top_rules": [
            {"rule": rule.as_dict(), "train": row, "holdout": metrics(holdout, rule)}
            for _, rule, row in ranked[:20]
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
