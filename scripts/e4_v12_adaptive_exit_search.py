#!/usr/bin/env python3
"""Evaluate a causal profit-protection/trailing-exit thesis on a later epoch."""

from __future__ import annotations

import argparse
import json
import pickle
import statistics
from collections import Counter, deque
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from itertools import pairwise
from pathlib import Path
from typing import Any

import e4_v12_profit_survival_search as base
import e4_v12_regime_robust_survival as regime
import numpy as np

SCHEMA_VERSION = "e4-v12-adaptive-exit-v3"
THESIS_FAMILY = "regime-robust-expected-value-adaptive-exit-v3"
MANIFEST = Path("artifacts/e4-v12-adaptive-exit-source-manifest.json")
NEW_CAPTURE_ROOT = Path(".tmp-regime-newer-evidence/runs")
SHORTLIST_PER_STOP_AND_HORIZON = 3
MODEL_FAMILIES = (
    *regime.MODEL_FAMILIES,
    "profit_probability_conservative",
    "profit_probability_geometric",
)
REGIME_FEATURE_NAMES = (
    "prior_launches_60s",
    "prior_launches_300s",
    "prior_public_buy_sol_median",
    "prior_unique_buyers_median",
    "prior_price_multiple_median",
    "prior_buy_acceleration_median",
    "prior_prefix_drawdown_median",
    "relative_public_buy_sol",
    "relative_unique_buyers",
    "relative_price_multiple",
    "relative_buy_acceleration",
    "relative_prefix_drawdown",
)
FEATURE_NAMES = (*base.FEATURE_NAMES, *REGIME_FEATURE_NAMES)


@dataclass(frozen=True, slots=True)
class AdaptivePolicy:
    initial_stop: float
    take: float
    hold_ms: int
    trail_activate: float
    trail_retrace: float
    protect_activate: float = 1.15
    protection_floor: float = 1.06

    @property
    def key(self) -> str:
        return (
            f"stop={self.initial_stop:.2f}|take={self.take:.2f}|hold={self.hold_ms}"
            f"|protect={self.protect_activate:.2f}:{self.protection_floor:.2f}"
            f"|trail={self.trail_activate:.2f}:{self.trail_retrace:.2f}"
        )


POLICIES = tuple(
    AdaptivePolicy(stop, take, hold, activate, retrace)
    for stop in (0.70, 0.80)
    for take in (2.0, 3.0)
    for hold in (30_000, 60_000)
    for activate in (1.25, 1.50)
    for retrace in (0.15, 0.25)
)


def capture_specs(root: Path, *, include_holdout: bool) -> list[base.CaptureSpec]:
    parent = [
        replace(spec, split="train")
        for spec in regime.all_capture_specs(root, include_holdout=True)
    ]
    manifest = json.loads((root / MANIFEST).read_text(encoding="utf-8"))
    newer = []
    for row in manifest["new_captures"]:
        split = str(row["split"])
        if split == "holdout" and not include_holdout:
            continue
        run_id = str(row["run_id"])
        newer.append(
            base.CaptureSpec(
                run_id=run_id,
                split=split,
                start_ns=base.integer(row["capture_start_ns"]),
                end_ns=base.integer(row["capture_end_ns"]),
                events_path=root
                / NEW_CAPTURE_ROOT
                / run_id
                / "artifacts/e4-v12-forward-batch-live-events.jsonl",
                expected_sha256=str(row["events_sha256"]),
            )
        )
    specs = sorted([*parent, *newer], key=lambda spec: (spec.start_ns, int(spec.run_id)))
    if any(right.start_ns <= left.start_ns for left, right in pairwise(specs)):
        raise ValueError("capture chronology is not strictly increasing")
    return specs


def adaptive_outcome(
    trace: base.Trace,
    horizon_ms: int,
    policy: AdaptivePolicy,
    *,
    latency_ms: int = 0,
) -> base.Outcome | None:
    decision_ns = trace.create_ns + horizon_ms * 1_000_000
    entry_ns = decision_ns + latency_ms * 1_000_000
    entry = base.latest_point(trace.points, entry_ns)
    if entry is None or entry.complete:
        return None
    entry_price = entry.price_sol or entry.virtual_sol / max(entry.virtual_tokens, 1e-12)
    exit_point = entry
    exit_reason = "MAXIMUM_HOLD"
    peak_multiple = 1.0
    deadline = entry_ns + policy.hold_ms * 1_000_000
    for point in trace.points:
        if point.timestamp_ns <= entry_ns:
            continue
        if point.timestamp_ns > deadline:
            break
        if point.price_sol <= 0 or point.virtual_sol <= 0 or point.virtual_tokens <= 0:
            continue
        exit_point = point
        multiple = point.price_sol / max(entry_price, 1e-18)
        peak_multiple = max(peak_multiple, multiple)
        if point.complete:
            exit_reason = "LIQUIDITY_EMERGENCY"
            break
        if multiple >= policy.take:
            exit_reason = "TAKE_PROFIT"
            break
        floor = policy.initial_stop
        if peak_multiple >= policy.protect_activate:
            floor = max(floor, policy.protection_floor)
        if peak_multiple >= policy.trail_activate:
            floor = max(floor, peak_multiple * (1 - policy.trail_retrace))
        if multiple <= floor:
            if peak_multiple >= policy.trail_activate:
                exit_reason = "TRAILING_STOP"
            elif peak_multiple >= policy.protect_activate:
                exit_reason = "PROFIT_PROTECTION"
            else:
                exit_reason = "INITIAL_STOP"
            break
    position = base.STARTING_BANKROLL_SOL * base.POSITION_FRACTION
    tokens = base.choice_sets.buy_tokens(position, entry.virtual_sol, entry.virtual_tokens)
    if entry.real_tokens > 0:
        tokens = min(tokens, entry.real_tokens)
    gross = tokens * exit_point.virtual_sol / max(exit_point.virtual_tokens + tokens, 1e-12)
    return base.Outcome(
        gross_multiple=gross / position,
        exit_offset_ms=max(0.0, (exit_point.timestamp_ns - decision_ns) / 1_000_000),
        exit_reason=exit_reason,
    )


def extract_specs(
    specs: Sequence[base.CaptureSpec],
    creator_counts: Counter[str],
    *,
    verify_hashes: bool,
    horizons: Sequence[int] = base.HORIZONS_MS,
    certification_policy_index: int | None = None,
) -> tuple[list[base.ResearchRow], dict[str, Any]]:
    rows: list[base.ResearchRow] = []
    audits = []
    for spec in specs:
        print(f"extract {spec.split} capture {spec.run_id}", flush=True)
        actual_sha = base.sha256_path(spec.events_path) if verify_hashes else spec.expected_sha256
        if actual_sha != spec.expected_sha256:
            raise ValueError(f"capture hash changed: {spec.run_id}")
        traces, parse_errors = base.load_capture(spec, creator_counts)
        included: Counter[str] = Counter()
        for trace in traces:
            for horizon_ms in horizons:
                decision_ns = trace.create_ns + horizon_ms * 1_000_000
                if decision_ns + 60_000 * 1_000_000 > spec.end_ns:
                    included["insufficient_tail"] += 1
                    continue
                features = base.snapshot(trace, horizon_ms)
                if features is None:
                    included["invalid_snapshot"] += 1
                    continue
                outcomes = tuple(adaptive_outcome(trace, horizon_ms, policy) for policy in POLICIES)
                if any(outcome is None for outcome in outcomes):
                    included["invalid_outcome"] += 1
                    continue
                latency_outcomes = (
                    tuple(
                        adaptive_outcome(
                            trace,
                            horizon_ms,
                            POLICIES[certification_policy_index],
                            latency_ms=latency,
                        )
                        for latency in base.LATENCIES_MS
                    )
                    if certification_policy_index is not None
                    else ()
                )
                if any(outcome is None for outcome in latency_outcomes):
                    included["invalid_latency_outcome"] += 1
                    continue
                rows.append(
                    base.ResearchRow(
                        run_id=trace.run_id,
                        split=trace.split,
                        mint=trace.mint,
                        decision_ns=decision_ns,
                        horizon_ms=horizon_ms,
                        features=features,
                        outcomes=outcomes,  # type: ignore[arg-type]
                        latency_outcomes=latency_outcomes,  # type: ignore[arg-type]
                    )
                )
                included["rows"] += 1
        audits.append(
            {
                "run_id": spec.run_id,
                "split": spec.split,
                "launches": len(traces),
                "parse_errors": parse_errors,
                "sha256": actual_sha,
                "hash_match": actual_sha == spec.expected_sha256,
                **dict(included),
            }
        )
    return rows, {
        "version": SCHEMA_VERSION,
        "runs": audits,
        "rows": len(rows),
        "rows_by_split": dict(Counter(row.split for row in rows)),
        "rows_by_horizon": dict(Counter(str(row.horizon_ms) for row in rows)),
        "holdout_opened": any(spec.split == "holdout" for spec in specs),
        "future_values_in_features": False,
        "future_values_in_exit": True,
        "exit_values_used_for_labels_and_replay_only": True,
        "production_paths_changed": 0,
    }


def census(rows: Sequence[base.ResearchRow]) -> list[dict[str, Any]]:
    position = base.STARTING_BANKROLL_SOL * base.POSITION_FRACTION
    output = []
    for horizon in base.HORIZONS_MS:
        for index, policy in enumerate(POLICIES):
            record: dict[str, Any] = {
                "horizon_ms": horizon,
                "policy_index": index,
                "policy": policy.key,
            }
            for split in ("train", "validation"):
                pnls = [
                    base.net_pnl(row.outcomes[index], 0.001, position)
                    for row in rows
                    if row.split == split and row.horizon_ms == horizon
                ]
                profits = [pnl for pnl in pnls if pnl > 0]
                losses = [pnl for pnl in pnls if pnl <= 0]
                record[split] = {
                    "launches": len(pnls),
                    "profitable": len(profits),
                    "profitable_percent": 100 * len(profits) / max(len(pnls), 1),
                    "oracle_pnl_sol": sum(pnls),
                    "oracle_profit_factor": sum(profits) / max(abs(sum(losses)), 1e-12),
                }
            output.append(record)
    return output


def with_causal_market_context(
    rows: Sequence[base.ResearchRow],
) -> list[base.ResearchRow]:
    """Append rolling market features made exclusively from earlier snapshots."""
    if rows and len(rows[0].features) == len(FEATURE_NAMES):
        return list(rows)
    enhanced: list[base.ResearchRow | None] = [None] * len(rows)
    for horizon in base.HORIZONS_MS:
        indices = sorted(
            (index for index, row in enumerate(rows) if row.horizon_ms == horizon),
            key=lambda index: (rows[index].decision_ns, int(rows[index].run_id), rows[index].mint),
        )
        prior: deque[base.ResearchRow] = deque()
        for index in indices:
            row = rows[index]
            lower_300s = row.decision_ns - 300_000_000_000
            while prior and prior[0].decision_ns < lower_300s:
                prior.popleft()
            history = list(prior)[-100:]
            recent_60s = sum(
                item.decision_ns >= row.decision_ns - 60_000_000_000 for item in history
            )
            feature_indices = (2, 6, 14, 22, 16)
            medians = tuple(
                statistics.median(item.features[position] for item in history)
                if history
                else 0.0
                for position in feature_indices
            )
            relatives = tuple(
                row.features[position] / max(abs(median), 1e-9)
                for position, median in zip(feature_indices, medians)
            )
            context = (float(recent_60s), float(len(history)), *medians, *relatives)
            enhanced[index] = replace(row, features=(*row.features, *context))
            prior.append(row)
    if any(row is None for row in enhanced):
        raise ValueError("market context did not cover every row")
    return [row for row in enhanced if row is not None]


def robust_rank(metrics: Mapping[str, Any]) -> tuple[Any, ...]:
    windows = list(metrics["by_capture_window"].values())
    positive_windows = sum(window["pnl_sol"] > 0 for window in windows)
    worst_window = min((window["pnl_sol"] for window in windows), default=-999.0)
    requirements = (
        metrics["trades"] >= 50,
        metrics["capture_windows"] == 5,
        metrics["win_rate"] >= 0.65,
        metrics["net_pnl_sol"] > 0,
        metrics["profit_factor"] >= 1.25,
        metrics["maximum_drawdown_fraction"] <= 0.15,
        metrics["largest_winner_contribution"] <= 0.25,
        positive_windows >= 4,
        worst_window >= -0.03,
    )
    return (
        sum(requirements),
        *requirements,
        positive_windows,
        worst_window,
        metrics["net_pnl_sol"],
        metrics["profit_factor"],
    )


def score_family(
    models: Mapping[str, Any], rows: Sequence[base.ResearchRow], family: str
) -> np.ndarray:
    if family in regime.MODEL_FAMILIES:
        return regime.score_family(models, rows, family)
    matrix = base.feature_matrix(rows)
    probability_all = models["cls_all"].predict_proba(matrix)[:, 1]
    probability_recent = models["cls_recent"].predict_proba(matrix)[:, 1]
    if family == "profit_probability_conservative":
        return np.minimum(probability_all, probability_recent)
    return np.sqrt(probability_all * probability_recent)


def development_search(
    rows: Sequence[base.ResearchRow], opportunities: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    shortlisted = []
    for horizon in base.HORIZONS_MS:
        for stop in (0.70, 0.80):
            options = [
                row
                for row in opportunities
                if row["horizon_ms"] == horizon
                and POLICIES[base.integer(row["policy_index"])].initial_stop == stop
            ]
            options.sort(
                key=lambda row: (
                    row["train"]["oracle_profit_factor"],
                    row["train"]["profitable"],
                ),
                reverse=True,
            )
            shortlisted.extend(options[:SHORTLIST_PER_STOP_AND_HORIZON])
    results = []
    for number, config in enumerate(shortlisted, 1):
        print(f"fit adaptive-exit candidate {number}/{len(shortlisted)}", flush=True)
        horizon = base.integer(config["horizon_ms"])
        policy_index = base.integer(config["policy_index"])
        train = [row for row in rows if row.split == "train" and row.horizon_ms == horizon]
        validation = [
            row for row in rows if row.split == "validation" and row.horizon_ms == horizon
        ]
        models = regime.fit_regime_models(train, policy_index)
        for family in MODEL_FAMILIES:
            scores = score_family(models, validation, family)
            ordered = sorted(scores, reverse=True)
            thresholds = {
                float(ordered[min(target - 1, len(ordered) - 1)])
                for target in (50, 80, 120, 180, 240, 360, 500)
            }
            best = None
            for threshold in sorted(thresholds, reverse=True):
                metrics = base.simulate_predictions(
                    validation,
                    scores,
                    threshold,
                    policy_index=policy_index,
                    priority_fee_sol=0.001,
                )
                if best is None or robust_rank(metrics) > robust_rank(best):
                    best = metrics
            results.append(
                {
                    "horizon_ms": horizon,
                    "policy_index": policy_index,
                    "policy": POLICIES[policy_index].key,
                    "priority_fee_sol": 0.001,
                    "model_family": family,
                    "recent_train_windows": models["recent_window_ids"],
                    "validation": best,
                }
            )
    results.sort(key=lambda row: robust_rank(row["validation"]), reverse=True)
    winner = results[0] if results else None
    return {
        "version": SCHEMA_VERSION,
        "thesis_family": THESIS_FAMILY,
        "development_only": True,
        "holdout_rows_read": 0,
        "adaptive_policies_in_census": len(opportunities),
        "modelled_candidates": len(results),
        "selection_rule": "nine five-window robustness gates, then worst-window and net economics",
        "winner": winner,
        "top_candidates": results[:20],
        "frozen_candidate_ready": bool(winner and robust_rank(winner["validation"])[0] == 9),
        "production_paths_changed": 0,
    }


def freeze(
    root: Path, audit: Mapping[str, Any], search: Mapping[str, Any], script: Path
) -> dict[str, Any]:
    winner = search.get("winner")
    if not winner or not search.get("frozen_candidate_ready"):
        raise ValueError("development candidate has not cleared all gates")
    policy = POLICIES[base.integer(winner["policy_index"])]
    manifest = json.loads((root / MANIFEST).read_text(encoding="utf-8"))
    identity = {
        "thesis_family_identifier": THESIS_FAMILY,
        "source_code_fingerprint": base.sha256_path(script),
        "dataset_source_manifest_fingerprint": base.sha256_path(root / MANIFEST),
        "evidence_epoch": [
            {"run_id": row["run_id"], "sha256": row["sha256"]} for row in audit["runs"]
        ],
        "feature_set_fingerprint": base.stable_hash(FEATURE_NAMES),
        "model_family": winner["model_family"],
        "full_parameters": {
            "random_seed": regime.RANDOM_SEED,
            "learning_rate": 0.04,
            "max_iter": 160,
            "max_leaf_nodes": 15,
            "min_samples_leaf": 80,
            "l2_regularization": 10.0,
            "recent_train_windows": regime.RECENT_TRAIN_WINDOWS,
            "threshold": winner["validation"]["threshold"],
        },
        "causal_horizon_ms": winner["horizon_ms"],
        "candidate_risk_set_policy": "one independent launch at frozen causal horizon",
        "chronological_split": {
            "train": [row["run_id"] for row in audit["runs"] if row["split"] == "train"],
            "validation": [
                row["run_id"] for row in audit["runs"] if row["split"] == "validation"
            ],
            "holdout": [
                str(row["run_id"])
                for row in manifest["new_captures"]
                if row["split"] == "holdout"
            ],
        },
        "bankroll": base.STARTING_BANKROLL_SOL,
        "position_sizing": {
            "fraction": base.POSITION_FRACTION,
            "maximum_concurrent_positions": base.MAX_CONCURRENT_POSITIONS,
        },
        "fee_model": {
            "priority_fee_sol": 0.001,
            "tip_sol": base.TIP_SOL,
            "base_transaction_fee_sol": base.BASE_TRANSACTION_FEE_SOL,
            "protocol_and_creator_fee_bps": base.PROTOCOL_AND_CREATOR_FEE_BPS,
        },
        "output_guard": "reserve-valid exact constant-product quote",
        "latency_assumptions_ms": list(base.LATENCIES_MS),
        "execution_policy": "paper replay; one entry per launch; no re-entry",
        "exit_policy": asdict(policy),
    }
    return {
        "version": SCHEMA_VERSION,
        "status": "FROZEN_FOR_ONE_TIME_ADAPTIVE_EXIT_HOLDOUT",
        "experiment_id": f"e4x-{base.stable_hash(identity)}",
        "identity": identity,
        "candidate": winner,
        "development_ledger_hash": winner["validation"]["ledger_hash"],
        "holdout_rows_read_before_freeze": 0,
        "live_evidence_authorised": False,
        "production_promotion_authorised": False,
        "production_paths_changed": 0,
    }


def historical_gate(
    economics: Mapping[str, Mapping[str, Any]], baseline: Mapping[str, Any]
) -> dict[str, Any]:
    zero = economics["0"]
    windows = list(zero["by_capture_window"].values())
    requirements = {
        "at_least_50_closed_trades": zero["trades"] >= 50,
        "all_five_holdout_windows_represented": zero["capture_windows"] == 5,
        "at_least_four_profitable_windows": sum(row["pnl_sol"] > 0 for row in windows) >= 4,
        "win_rate_at_least_65_percent": zero["win_rate"] >= 0.65,
        "wilson_lower_bound_at_least_55_percent": zero["wilson_95_lower_bound"] >= 0.55,
        "net_pnl_positive": zero["net_pnl_sol"] > 0,
        "profit_factor_at_least_1_25": zero["profit_factor"] >= 1.25,
        "maximum_drawdown_at_most_15_percent": zero["maximum_drawdown_fraction"] <= 0.15,
        "largest_winner_at_most_25_percent": zero["largest_winner_contribution"] <= 0.25,
        "every_latency_pnl_positive": all(row["net_pnl_sol"] > 0 for row in economics.values()),
        "every_latency_profit_factor_at_least_1_25": all(
            row["profit_factor"] >= 1.25 for row in economics.values()
        ),
        "beats_frozen_scalar_baseline_pnl": zero["net_pnl_sol"] > baseline["holdout"]["net_pnl_sol"],
    }
    passed = all(requirements.values())
    if passed:
        failure = None
    elif zero["trades"] < 50:
        failure = "INSUFFICIENT_LABELS"
    elif zero["net_pnl_sol"] <= 0:
        failure = "NEGATIVE_EXPECTANCY"
    elif zero["profit_factor"] < 1.25:
        failure = "INADEQUATE_PROFIT_FACTOR"
    elif zero["win_rate"] < 0.65:
        failure = "FALSE_POSITIVE_OVERLOAD"
    else:
        failure = "HOLDOUT_COLLAPSE"
    return {
        "status": "HISTORICAL_HOLDOUT_CONFIRMED" if passed else "NOT_CONCLUSIVE",
        "historical_qualification_passed": passed,
        "requirements": requirements,
        "failed_requirements": [name for name, value in requirements.items() if not value],
        "failure_classification": failure,
        "untouched_live_confirmation_required": True,
        "live_confirmation_authorised": passed,
        "production_promotion_authorised": False,
        "production_paths_changed": 0,
    }


def certify(root: Path, cache: Path, frozen_path: Path) -> dict[str, Any]:
    frozen = json.loads(frozen_path.read_text(encoding="utf-8"))
    with cache.open("rb") as handle:
        cached = pickle.load(handle)
    rows = with_causal_market_context(cached["rows"])
    candidate = frozen["candidate"]
    horizon = base.integer(candidate["horizon_ms"])
    policy_index = base.integer(candidate["policy_index"])
    train = [row for row in rows if row.split == "train" and row.horizon_ms == horizon]
    validation = [row for row in rows if row.split == "validation" and row.horizon_ms == horizon]
    models = regime.fit_regime_models(train, policy_index)
    validation_scores = score_family(models, validation, str(candidate["model_family"]))
    threshold = base.finite(candidate["validation"]["threshold"])
    validation_repeat = base.simulate_predictions(
        validation,
        validation_scores,
        threshold,
        policy_index=policy_index,
        priority_fee_sol=0.001,
    )
    if validation_repeat["ledger_hash"] != frozen["development_ledger_hash"]:
        raise ValueError("frozen development ledger did not reproduce")
    holdout_specs = [spec for spec in capture_specs(root, include_holdout=True) if spec.split == "holdout"]
    holdout, audit = extract_specs(
        holdout_specs,
        Counter(cached["creator_counts"]),
        verify_hashes=True,
        horizons=(horizon,),
        certification_policy_index=policy_index,
    )
    holdout = with_causal_market_context(holdout)
    scores = score_family(models, holdout, str(candidate["model_family"]))
    economics = {
        str(latency): base.simulate_predictions(
            holdout,
            scores,
            threshold,
            policy_index=policy_index,
            priority_fee_sol=0.001,
            latency_index=index,
        )
        for index, latency in enumerate(base.LATENCIES_MS)
    }
    baseline = base.scalar_baseline(
        validation,
        holdout,
        target_trades=validation_repeat["trades"],
        policy_index=policy_index,
        priority_fee_sol=0.001,
    )
    repeat = {
        str(latency): base.simulate_predictions(
            holdout,
            scores,
            threshold,
            policy_index=policy_index,
            priority_fee_sol=0.001,
            latency_index=index,
        )["ledger_hash"]
        for index, latency in enumerate(base.LATENCIES_MS)
    }
    if any(economics[key]["ledger_hash"] != repeat[key] for key in economics):
        raise ValueError("holdout replay is not deterministic")
    return {
        "version": SCHEMA_VERSION,
        "experiment_id": frozen["experiment_id"],
        "candidate": candidate,
        "development_reproduction": validation_repeat,
        "holdout_data_audit": audit,
        "holdout_rows": len(holdout),
        "latencies": economics,
        "strongest_scalar_baseline": baseline,
        "deterministic_replay": True,
        "verdict": historical_gate(economics, baseline),
        "production_paths_changed": 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--cache", type=Path, default=Path(".tmp-adaptive-exit-development.pkl"))
    parser.add_argument("--reuse-cache", action="store_true")
    parser.add_argument("--skip-source-hashes", action="store_true")
    parser.add_argument("--certify-holdout", action="store_true")
    args = parser.parse_args()
    root = args.repo_root.resolve()
    cache = (root / args.cache).resolve()
    output = root / "artifacts"
    frozen_path = output / "e4-v12-adaptive-exit-frozen-candidate.json"
    if args.certify_holdout:
        if not frozen_path.exists():
            raise ValueError("freeze a development candidate before opening holdout")
        result = certify(root, cache, frozen_path)
        base.write_json(output / "e4-v12-adaptive-exit-historical-economics.json", result)
        print(json.dumps(result["verdict"], indent=2, sort_keys=True))
        return
    if args.reuse_cache:
        with cache.open("rb") as handle:
            cached = pickle.load(handle)
        raw_rows = cached["rows"]
        audit = cached["audit"]
    else:
        creators: Counter[str] = Counter()
        raw_rows, audit = extract_specs(
            capture_specs(root, include_holdout=False),
            creators,
            verify_hashes=not args.skip_source_hashes,
        )
        with cache.open("wb") as handle:
            pickle.dump(
                {"rows": raw_rows, "audit": audit, "creator_counts": creators}, handle
            )
    rows = with_causal_market_context(raw_rows)
    audit = dict(audit) | {
        "feature_names": list(FEATURE_NAMES),
        "causal_market_context": True,
        "market_context_uses_prior_snapshots_only": True,
    }
    opportunities = census(rows)
    search = development_search(rows, opportunities)
    base.write_json(output / "e4-v12-adaptive-exit-data-audit.json", audit)
    base.write_json(output / "e4-v12-adaptive-exit-opportunity-census.json", opportunities)
    base.write_json(output / "e4-v12-adaptive-exit-development-search.json", search)
    if search["frozen_candidate_ready"]:
        base.write_json(frozen_path, freeze(root, audit, search, Path(__file__).resolve()))
    print(json.dumps({"winner": search["winner"], "ready": search["frozen_candidate_ready"]}, indent=2))


if __name__ == "__main__":
    main()
