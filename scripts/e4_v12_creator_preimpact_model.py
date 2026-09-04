#!/usr/bin/env python3
from __future__ import annotations

import argparse
import itertools
import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from scripts.e4_v12_preimpact_model import Launch, load_launches, finite


@dataclass(frozen=True)
class Rule:
    min_wins: int
    min_trades: int
    min_win_rate: float
    min_seed_sol: float
    max_seed_sol: float
    min_fdv: float
    max_fdv: float
    max_buy_rank: int
    max_age_ms: float

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def candidate_snapshot(launch: Launch, rule: Rule) -> dict[str, Any] | None:
    for snap in launch.snapshots:
        if snap["sell_count"]:
            return None
        if snap["buy_rank"] > rule.max_buy_rank:
            return None
        if not (rule.min_seed_sol <= snap["creator_seed_sol"] <= rule.max_seed_sol):
            continue
        if not (rule.min_fdv <= snap["fdv_usd"] <= rule.max_fdv):
            continue
        if snap["age_ms"] > rule.max_age_ms:
            continue
        return snap
    return None


def evaluate_sequence(
    launches: Iterable[Launch],
    rule: Rule,
    *,
    initial_history: dict[str, tuple[int, int]] | None = None,
    update_history: bool = True,
) -> tuple[dict[str, Any], dict[str, tuple[int, int]]]:
    history = dict(initial_history or {})
    launches = sorted(list(launches), key=lambda row: row.created_ns)
    candidates: list[tuple[Launch, dict[str, Any]]] = []
    e4_entries = 0
    e4_winners = 0
    eligible_e4 = 0
    eligible_winners = 0

    for launch in launches:
        wins, losses = history.get(launch.creator, (0, 0))
        trades = wins + losses
        wr = wins / trades if trades else 0.0
        eligible = bool(
            launch.creator
            and wins >= rule.min_wins
            and trades >= rule.min_trades
            and wr >= rule.min_win_rate
        )
        if launch.e4_buy_ns is not None:
            e4_entries += 1
            if launch.e4_won:
                e4_winners += 1
            if eligible:
                eligible_e4 += 1
                if launch.e4_won:
                    eligible_winners += 1

        snap = candidate_snapshot(launch, rule) if eligible else None
        if snap is not None:
            candidates.append((launch, snap))

        # Crucially, this launch's E4 outcome is learned only after the launch
        # decision. It can influence future launches, never itself.
        if update_history and launch.e4_buy_ns is not None and launch.creator:
            if launch.e4_won:
                wins += 1
            else:
                losses += 1
            history[launch.creator] = (wins, losses)

    true = [(row, snap) for row, snap in candidates if row.e4_buy_ns is not None]
    winners = [(row, snap) for row, snap in true if row.e4_won]
    leads = [
        (row.e4_buy_ns - snap["received_ns"]) / 1e6
        for row, snap in true
        if row.e4_buy_ns is not None and row.e4_buy_ns >= snap["received_ns"]
    ]
    metrics = {
        "launches": len(launches),
        "e4_entries": e4_entries,
        "e4_winners": e4_winners,
        "eligible_e4_entries": eligible_e4,
        "eligible_e4_winners": eligible_winners,
        "candidates": len(candidates),
        "true_e4_candidates": len(true),
        "true_e4_winner_candidates": len(winners),
        "false_positives": len(candidates) - len(true),
        "precision": len(true) / len(candidates) if candidates else 0.0,
        "recall_all_e4": len(true) / e4_entries if e4_entries else 0.0,
        "winner_recall_all_e4": len(winners) / e4_winners if e4_winners else 0.0,
        "recall_eligible_e4": len(true) / eligible_e4 if eligible_e4 else 0.0,
        "winner_recall_eligible_e4": len(winners) / eligible_winners if eligible_winners else 0.0,
        "median_lead_ms": sorted(leads)[len(leads)//2] if leads else None,
    }
    return metrics, history


def rules():
    for values in itertools.product(
        (1, 2),
        (1, 2, 3),
        (0.5, 0.67, 1.0),
        (0.5, 1.0, 1.5, 2.0),
        (4.0, 5.0, 6.0),
        (3000.0, 3500.0, 4000.0),
        (7000.0, 8500.0),
        (1, 2, 3),
        (80.0, 150.0, 300.0, 500.0),
    ):
        rule = Rule(*values)
        if rule.min_wins <= rule.min_trades and rule.min_seed_sol <= rule.max_seed_sol:
            yield rule


def objective(m: dict[str, Any]) -> tuple[float, float, float, float]:
    # Require at least two genuinely prior-creator E4 opportunities before
    # rewarding precision. Then prefer precision, then winner capture, then lead.
    valid = 1.0 if m["eligible_e4_entries"] >= 2 and m["true_e4_candidates"] >= 2 else 0.0
    return (
        valid,
        finite(m["precision"]),
        finite(m["winner_recall_eligible_e4"]),
        finite(m["recall_eligible_e4"]),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Causal repeat-E4-creator pre-impact evaluator")
    parser.add_argument("--train", action="append", default=[], metavar="BATCH:EVENTS")
    parser.add_argument("--holdout", action="append", default=[], metavar="BATCH:EVENTS")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    def parse(items: list[str]) -> list[Launch]:
        rows: list[Launch] = []
        for item in items:
            batch, events = item.split(":", 1)
            rows.extend(load_launches(Path(batch), Path(events)))
        return rows

    train = parse(args.train)
    holdout = parse(args.holdout)
    ranked = []
    for rule in rules():
        train_metrics, history = evaluate_sequence(train, rule)
        ranked.append((objective(train_metrics), rule, train_metrics, history))
    ranked.sort(key=lambda row: row[0], reverse=True)
    _, best, train_metrics, history = ranked[0]
    holdout_frozen, _ = evaluate_sequence(holdout, best, initial_history=history, update_history=False)
    holdout_rolling, _ = evaluate_sequence(holdout, best, initial_history=history, update_history=True)

    safe = bool(
        holdout_frozen["precision"] >= 0.50
        and holdout_frozen["true_e4_candidates"] >= 2
        and holdout_frozen["false_positives"] <= holdout_frozen["true_e4_candidates"]
    )
    payload = {
        "version": "e4-v12-repeat-creator-preimpact-v1",
        "rule": best.as_dict(),
        "train": train_metrics,
        "holdout_frozen_history": holdout_frozen,
        "holdout_rolling_history": holdout_rolling,
        "safe_to_authorize": safe,
        "top_rules": [
            {"rule": rule.as_dict(), "train": metrics}
            for _, rule, metrics, _ in ranked[:10]
        ],
    }
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
