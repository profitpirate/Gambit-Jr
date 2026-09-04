#!/usr/bin/env python3
from __future__ import annotations

import argparse
import itertools
import json
import math
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

E4_WALLET = "E4EzXdwf7NNdqM2XGswWaWHfxgucVCo24PTCcrimTKBz"


def finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


@dataclass(frozen=True)
class Rule:
    min_seed_sol: float
    max_seed_sol: float
    min_fdv: float
    max_fdv: float
    min_buy_rank: int
    max_buy_rank: int
    min_noncreator_buyers: int
    max_age_ms: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "min_seed_sol": self.min_seed_sol,
            "max_seed_sol": self.max_seed_sol,
            "min_fdv": self.min_fdv,
            "max_fdv": self.max_fdv,
            "min_buy_rank": self.min_buy_rank,
            "max_buy_rank": self.max_buy_rank,
            "min_noncreator_buyers": self.min_noncreator_buyers,
            "max_age_ms": self.max_age_ms,
        }


@dataclass
class Launch:
    mint: str
    creator: str
    created_ns: int
    e4_buy_ns: int | None
    e4_won: bool
    snapshots: list[dict[str, Any]]


def same_window_e4(batch: Mapping[str, Any]) -> dict[str, bool]:
    positions = list((batch.get("actual_e4_fresh_sample") or {}).get("positions") or [])
    return {
        str(row.get("mint") or ""): finite(row.get("pnl_sol")) > 0
        for row in positions
        if str(row.get("mint") or "")
    }


def load_launches(batch_path: Path, events_path: Path) -> list[Launch]:
    batch = json.loads(batch_path.read_text(encoding="utf-8"))
    e4_outcomes = same_window_e4(batch)
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    with events_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            mint = str(row.get("mint") or "")
            if mint:
                grouped[mint].append(row)

    launches: list[Launch] = []
    for mint, rows in grouped.items():
        rows.sort(key=lambda row: (int(row.get("received_ns") or 0), int(row.get("event_index") or 0)))
        creator = ""
        created_ns = 0
        e4_buy_ns: int | None = None
        seed_sol = 0.0
        buy_rank = 0
        noncreator_buyers: set[str] = set()
        sell_count = 0
        snapshots: list[dict[str, Any]] = []

        for row in rows:
            kind = str(row.get("kind") or "").upper()
            received_ns = int(row.get("received_ns") or 0)
            trader = str(row.get("trader") or "")
            row_creator = str(row.get("creator") or (row.get("raw") or {}).get("creator") or "")
            if row_creator and not creator:
                creator = row_creator
            if kind == "CREATE" and not created_ns:
                created_ns = received_ns
                if row_creator:
                    creator = row_creator
                continue
            if kind not in {"BUY", "SELL", "PUMPSWAP_BUY", "PUMPSWAP_SELL"}:
                continue
            if trader == E4_WALLET and kind in {"BUY", "PUMPSWAP_BUY"}:
                e4_buy_ns = received_ns
                break
            if kind in {"SELL", "PUMPSWAP_SELL"}:
                sell_count += 1
                continue

            buy_rank += 1
            sol_amount = max(0.0, finite(row.get("sol_amount")))
            if creator and trader == creator:
                seed_sol += sol_amount
            elif trader:
                noncreator_buyers.add(trader)
            raw = row.get("raw") if isinstance(row.get("raw"), Mapping) else {}
            fdv = finite(row.get("fdv_usd") or raw.get("usd_market_cap") or raw.get("fdv_usd"))
            age_ms = ((received_ns - created_ns) / 1e6) if created_ns and received_ns >= created_ns else float("inf")
            snapshots.append(
                {
                    "received_ns": received_ns,
                    "buy_rank": buy_rank,
                    "creator_seed_sol": seed_sol,
                    "noncreator_buyers": len(noncreator_buyers),
                    "sell_count": sell_count,
                    "fdv_usd": fdv,
                    "age_ms": age_ms,
                }
            )

        if created_ns:
            launches.append(
                Launch(
                    mint=mint,
                    creator=creator,
                    created_ns=created_ns,
                    e4_buy_ns=e4_buy_ns,
                    e4_won=e4_outcomes.get(mint, False),
                    snapshots=snapshots,
                )
            )
    return launches


def trigger(launch: Launch, rule: Rule) -> dict[str, Any] | None:
    for snap in launch.snapshots:
        if snap["sell_count"] != 0:
            return None
        if not (rule.min_buy_rank <= snap["buy_rank"] <= rule.max_buy_rank):
            continue
        if snap["noncreator_buyers"] < rule.min_noncreator_buyers:
            continue
        if not (rule.min_seed_sol <= snap["creator_seed_sol"] <= rule.max_seed_sol):
            continue
        if not (rule.min_fdv <= snap["fdv_usd"] <= rule.max_fdv):
            continue
        if snap["age_ms"] > rule.max_age_ms:
            continue
        return snap
    return None


def score(launches: Iterable[Launch], rule: Rule) -> dict[str, Any]:
    launches = list(launches)
    e4 = [row for row in launches if row.e4_buy_ns is not None]
    e4_winners = [row for row in e4 if row.e4_won]
    candidates: list[tuple[Launch, dict[str, Any]]] = []
    for launch in launches:
        snap = trigger(launch, rule)
        if snap is not None:
            candidates.append((launch, snap))
    candidate_mints = {row.mint for row, _ in candidates}
    true = [row for row, _ in candidates if row.e4_buy_ns is not None]
    true_winners = [row for row in true if row.e4_won]
    leads = [
        max(0.0, (row.e4_buy_ns - snap["received_ns"]) / 1e6)
        for row, snap in candidates
        if row.e4_buy_ns is not None and row.e4_buy_ns >= snap["received_ns"]
    ]
    return {
        "launches": len(launches),
        "e4_entries": len(e4),
        "e4_winners": len(e4_winners),
        "candidates": len(candidates),
        "true_e4_candidates": len(true),
        "true_e4_winner_candidates": len(true_winners),
        "precision": len(true) / len(candidates) if candidates else 0.0,
        "recall": len(true) / len(e4) if e4 else 0.0,
        "winner_recall": len(true_winners) / len(e4_winners) if e4_winners else 0.0,
        "false_positives": len(candidate_mints) - len(true),
        "median_lead_ms": sorted(leads)[len(leads)//2] if leads else None,
        "min_lead_ms": min(leads) if leads else None,
        "max_lead_ms": max(leads) if leads else None,
    }


def rules() -> Iterable[Rule]:
    for values in itertools.product(
        (0.5, 1.0, 1.5, 2.0),
        (4.0, 5.0, 6.0),
        (3000.0, 3500.0, 4000.0),
        (7000.0, 8500.0),
        (2, 3),
        (4, 5),
        (1, 2),
        (80.0, 150.0, 300.0, 500.0),
    ):
        rule = Rule(*values)
        if rule.min_seed_sol <= rule.max_seed_sol and rule.min_buy_rank <= rule.max_buy_rank:
            yield rule


def objective(metrics: Mapping[str, Any]) -> tuple[float, float, float, float]:
    # Precision is first because a 3k-launch window has only a handful of E4
    # trades. A low-precision pre-impact trigger would simply recreate the
    # failed generic-flow strategy. Require meaningful recall before rewarding
    # precision.
    recall = finite(metrics.get("recall"))
    winner_recall = finite(metrics.get("winner_recall"))
    precision = finite(metrics.get("precision"))
    candidates = max(1.0, finite(metrics.get("candidates"), 1.0))
    valid = 1.0 if recall >= 0.20 and winner_recall >= 0.20 else 0.0
    return (valid, precision, winner_recall, recall - candidates / 100_000.0)


def main() -> int:
    parser = argparse.ArgumentParser(description="Learn a causal pre-E4 V12 entry gate")
    parser.add_argument("--train", action="append", default=[], metavar="BATCH:EVENTS")
    parser.add_argument("--holdout", action="append", default=[], metavar="BATCH:EVENTS")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    def parse(items: list[str]) -> list[Launch]:
        output: list[Launch] = []
        for item in items:
            batch, events = item.split(":", 1)
            output.extend(load_launches(Path(batch), Path(events)))
        return output

    train = parse(args.train)
    holdout = parse(args.holdout)
    ranked = []
    for rule in rules():
        metrics = score(train, rule)
        ranked.append((objective(metrics), rule, metrics))
    ranked.sort(key=lambda row: row[0], reverse=True)
    best_objective, best, train_metrics = ranked[0]
    holdout_metrics = score(holdout, best)

    payload = {
        "version": "e4-v12-preimpact-role-model-v1",
        "methodology": {
            "causal": True,
            "entry_trigger_uses_e4_buy": False,
            "train_launches": len(train),
            "holdout_launches": len(holdout),
            "selection_objective": "precision first, subject to >=20% E4 and winner recall on training data",
        },
        "rule": best.as_dict(),
        "train": train_metrics,
        "holdout": holdout_metrics,
        "top_rules": [
            {"rule": rule.as_dict(), "metrics": metrics}
            for _, rule, metrics in ranked[:10]
        ],
        "safe_to_authorize": bool(
            holdout_metrics["precision"] >= 0.20
            and holdout_metrics["recall"] >= 0.20
            and holdout_metrics["winner_recall"] >= 0.20
            and holdout_metrics["candidates"] <= max(12, holdout_metrics["e4_entries"] * 4)
        ),
    }
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
