#!/usr/bin/env python3
"""Evaluate a causal resolved-outcome regime feature family at 300 ms latency."""

from __future__ import annotations

import heapq
import json
import pickle
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import e4_v12_causal_actor_memory as actors
import e4_v12_latency300_conformal as common
import e4_v12_latency300_labels as labels
import e4_v12_online_conformal_precision as conformal
import e4_v12_profit_survival_search as base
import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier

OUTPUT = Path("artifacts/e4-v12-latency300-causal-outcome-regime.json")
SCHEMA_VERSION = "e4-v12-latency300-causal-outcome-regime-v1"
THESIS_FAMILY = "latency300-causal-outcome-regime-v12"
RESOLUTION_DELAY_MS = 60_300
SHORT_WINDOW_MS = 300_000
LONG_WINDOW_MS = 1_800_000
SEVERE_LOSS_SOL = -0.010
REGIME_FEATURE_NAMES = (
    "resolved_launches_300s",
    "resolved_profit_rate_beta_300s",
    "resolved_mean_pnl_300s",
    "resolved_severe_loss_rate_300s",
    "resolved_launches_1800s",
    "resolved_profit_rate_beta_1800s",
    "resolved_mean_pnl_1800s",
    "resolved_severe_loss_rate_1800s",
    "resolved_profit_rate_delta_300s_1800s",
    "resolved_mean_pnl_delta_300s_1800s",
)
MODEL_PARAMETERS = {
    "learning_rate": 0.05,
    "max_iter": 100,
    "max_leaf_nodes": 15,
    "min_samples_leaf": 100,
    "l2_regularization": 2.0,
    "class_weight": "balanced",
}
RANDOM_SEED_BASE = 132_120
SOURCE_FILES = (
    "scripts/e4_v12_latency300_causal_outcome_regime.py",
    "scripts/e4_v12_latency300_temporal_drift.py",
    "scripts/e4_v12_latency300_conformal.py",
    "scripts/e4_v12_latency300_labels.py",
    "scripts/e4_v12_online_conformal_precision.py",
    "scripts/e4_v12_causal_actor_memory.py",
    "scripts/e4_v12_adaptive_exit_search.py",
    "scripts/e4_v12_profit_survival_search.py",
)


@dataclass(slots=True)
class RollingOutcomeStats:
    duration_ns: int
    observations: deque[tuple[int, float]] = field(default_factory=deque)
    wins: int = 0
    severe_losses: int = 0
    pnl_sum: float = 0.0

    def add(self, available_ns: int, pnl_sol: float) -> None:
        self.observations.append((available_ns, pnl_sol))
        self.wins += int(pnl_sol > 0)
        self.severe_losses += int(pnl_sol <= SEVERE_LOSS_SOL)
        self.pnl_sum += pnl_sol

    def prune(self, decision_ns: int) -> None:
        cutoff = decision_ns - self.duration_ns
        while self.observations and self.observations[0][0] < cutoff:
            _, pnl_sol = self.observations.popleft()
            self.wins -= int(pnl_sol > 0)
            self.severe_losses -= int(pnl_sol <= SEVERE_LOSS_SOL)
            self.pnl_sum -= pnl_sol

    def snapshot(self) -> tuple[float, float, float, float]:
        count = len(self.observations)
        return (
            float(count),
            (self.wins + 1.0) / (count + 2.0),
            self.pnl_sum / max(count, 1),
            self.severe_losses / max(count, 1),
        )


def sha256_lf(path: Path) -> str:
    return __import__("hashlib").sha256(
        path.read_bytes().replace(b"\r\n", b"\n")
    ).hexdigest()


def source_code_fingerprint(root: Path | None = None) -> str:
    root = Path.cwd() if root is None else root
    return base.stable_hash(
        {relative: sha256_lf(root / relative) for relative in SOURCE_FILES}
    )


def causal_regime_matrix(rows: list[Any], pnl: np.ndarray) -> np.ndarray:
    """Build regime features using only outcomes resolved before each decision."""
    if len(rows) != len(pnl):
        raise ValueError("row and outcome counts differ")
    result = np.zeros((len(rows), len(REGIME_FEATURE_NAMES)), dtype=np.float32)
    short = RollingOutcomeStats(SHORT_WINDOW_MS * 1_000_000)
    long = RollingOutcomeStats(LONG_WINDOW_MS * 1_000_000)
    pending: list[tuple[int, int, float]] = []
    order = sorted(
        range(len(rows)),
        key=lambda index: (
            rows[index].decision_ns,
            int(rows[index].run_id),
            rows[index].mint,
        ),
    )
    for sequence, index in enumerate(order):
        decision_ns = int(rows[index].decision_ns)
        while pending and pending[0][0] <= decision_ns:
            available_ns, _, resolved_pnl = heapq.heappop(pending)
            short.add(available_ns, resolved_pnl)
            long.add(available_ns, resolved_pnl)
        short.prune(decision_ns)
        long.prune(decision_ns)
        short_snapshot = short.snapshot()
        long_snapshot = long.snapshot()
        result[index] = (
            *short_snapshot,
            *long_snapshot,
            short_snapshot[1] - long_snapshot[1],
            short_snapshot[2] - long_snapshot[2],
        )
        available_ns = decision_ns + RESOLUTION_DELAY_MS * 1_000_000
        heapq.heappush(pending, (available_ns, sequence, float(pnl[index])))
    return result


def requirements_for(
    summary: dict[str, Any], folds: list[dict[str, Any]], metadata: dict[str, Any]
) -> dict[str, bool]:
    return {
        "minimum_closed_trades": summary["trades"] >= 300,
        "minimum_win_rate": summary["win_rate"] >= 0.65,
        "minimum_wilson_bound": summary["wilson_95_lower_bound"] >= 0.60,
        "positive_net_pnl": summary["net_pnl_sol"] > 0,
        "minimum_profit_factor": summary["profit_factor"] >= 1.25,
        "every_fold_minimum_trades": all(row["trades"] >= 50 for row in folds),
        "every_fold_minimum_win_rate": all(row["win_rate"] >= 0.55 for row in folds),
        "every_fold_positive": all(row["net_pnl_sol"] > 0 for row in folds),
        "every_fold_minimum_profit_factor": all(
            row["profit_factor"] >= 1.10 for row in folds
        ),
        "every_fold_six_profitable_windows": all(
            row["profitable_capture_windows"] >= 6 for row in folds
        ),
        "maximum_drawdown": summary["maximum_fold_drawdown_fraction"] <= 0.15,
        "largest_winner_limit": summary["largest_winner_contribution"] <= 0.15,
        "full_quote_coverage": metadata["no_quote_rows"] == 0,
    }


def main() -> dict[str, Any]:
    metadata = json.loads(labels.METADATA_PATH.read_text(encoding="utf-8"))
    with conformal.CACHE.open("rb") as handle:
        payload = pickle.load(handle)
    if metadata["dataset_manifest_sha256"] != payload["manifest_sha256"]:
        raise ValueError("300 ms labels target another evidence manifest")
    if metadata["pnl_sha256"] != base.sha256_path(labels.PNL_PATH):
        raise ValueError("300 ms label matrix fingerprint changed")
    if metadata["all_source_hashes_verified"] is not True:
        raise ValueError("300 ms source hashes were not verified")
    if metadata["active_untouched_live_data_used"] is not False:
        raise ValueError("active untouched live evidence was consumed")
    rows = payload["rows"]
    pnl = np.load(labels.PNL_PATH, allow_pickle=False)
    if len(pnl) != len(rows) or not bool(np.all(np.isfinite(pnl))):
        raise ValueError("300 ms labels are incomplete")
    regime_features = causal_regime_matrix(rows, pnl)
    base_features = np.asarray([row.features for row in rows], dtype=np.float32)
    features = np.concatenate((base_features, regime_features), axis=1)
    windows = sorted({row.run_id for row in rows}, key=int)
    window_index = {run_id: index for index, run_id in enumerate(windows)}
    row_windows = np.asarray(
        [window_index[row.run_id] for row in rows], dtype=np.int16
    )
    decisions = np.asarray([row.decision_ns for row in rows], dtype=np.int64)
    folds: list[dict[str, Any]] = []
    for fold_number, (test_start, calibration_size, test_size) in enumerate(
        conformal.FOLDS
    ):
        fit_end = test_start - calibration_size
        fit = np.flatnonzero(row_windows < fit_end)
        calibration = np.flatnonzero(
            (row_windows >= fit_end) & (row_windows < test_start)
        )
        test = np.flatnonzero(
            (row_windows >= test_start) & (row_windows < test_start + test_size)
        )
        calibration = calibration[np.argsort(decisions[calibration], kind="stable")]
        test = test[np.argsort(decisions[test], kind="stable")]
        model = HistGradientBoostingClassifier(
            **MODEL_PARAMETERS,
            early_stopping=False,
            random_state=(
                RANDOM_SEED_BASE + fold_number * 100 + conformal.POLICY_INDEX
            ),
        )
        model.fit(features[fit], pnl[fit] > 0)
        calibration_scores = model.predict_proba(features[calibration])[:, 1]
        test_scores = model.predict_proba(features[test])[:, 1]
        mask, thresholds = conformal.rolling_selection(
            calibration_scores, test_scores
        )
        result = conformal.replay(rows, test[mask], pnl, test_scores[mask])
        result.update(
            {
                "fold": fold_number,
                "fit_windows": windows[:fit_end],
                "calibration_windows": windows[fit_end:test_start],
                "test_windows": windows[test_start : test_start + test_size],
                "threshold_minimum": min(thresholds),
                "threshold_maximum": max(thresholds),
                "threshold_hash": base.stable_hash(thresholds),
            }
        )
        folds.append(result)

    summary = common.aggregate(folds)
    requirements = requirements_for(summary, folds, metadata)
    passed = all(requirements.values())
    chronology = [
        {
            "fit": fold["fit_windows"],
            "calibration": fold["calibration_windows"],
            "test": fold["test_windows"],
        }
        for fold in folds
    ]
    identity = {
        "thesis_family_identifier": THESIS_FAMILY,
        "source_code_fingerprint": source_code_fingerprint(),
        "dataset_source_manifest_fingerprint": payload["manifest_sha256"],
        "evidence_epoch": windows,
        "feature_set_fingerprint": base.stable_hash(
            (*actors.FEATURE_NAMES, *REGIME_FEATURE_NAMES)
        ),
        "model_family": "histogram_boosting_with_causal_outcome_regime_memory",
        "full_parameters": {
            "model": MODEL_PARAMETERS,
            "random_seed_base": RANDOM_SEED_BASE,
            "outcome_resolution_delay_ms": RESOLUTION_DELAY_MS,
            "short_regime_window_ms": SHORT_WINDOW_MS,
            "long_regime_window_ms": LONG_WINDOW_MS,
            "severe_loss_sol": SEVERE_LOSS_SOL,
            "score_fraction": conformal.SCORE_FRACTION,
            "rolling_history_rows": conformal.HISTORY_ROWS,
            "threshold_refresh_rows": conformal.REFRESH_ROWS,
            "adaptive_exit_policy_index": conformal.POLICY_INDEX,
            "label_matrix_sha256": metadata["pnl_sha256"],
        },
        "causal_horizon_ms": 250,
        "candidate_risk_set_policy": (
            "fit exact-300ms net-profit labels with causal 5m/30m resolved-"
            "outcome regime memory; accept above the unchanged rolling "
            "99.73rd percentile of prior scores"
        ),
        "chronological_split": chronology,
        "bankroll_sol": base.STARTING_BANKROLL_SOL,
        "position_sizing": {
            "fraction_of_current_bankroll": base.POSITION_FRACTION,
            "maximum_concurrent_positions": base.MAX_CONCURRENT_POSITIONS,
        },
        "fee_model": {
            "protocol_and_creator_fee_bps": base.PROTOCOL_AND_CREATOR_FEE_BPS,
            "priority_fee_sol": 0.001,
            "tip_sol": base.TIP_SOL,
            "base_transaction_fee_sol": base.BASE_TRANSACTION_FEE_SOL,
        },
        "output_guard": {
            "rolling_history_rows": conformal.HISTORY_ROWS,
            "refresh_rows": conformal.REFRESH_ROWS,
            "score_fraction": conformal.SCORE_FRACTION,
            "threshold_uses_prior_scores_only": True,
        },
        "latency_assumptions_ms": [labels.LATENCY_MS],
        "execution_policy": (
            "paper replay at exact 300 ms entry delay; compound 1.85% of "
            "current bankroll; maximum two concurrent positions"
        ),
        "exit_policy": conformal.adaptive.POLICIES[conformal.POLICY_INDEX].key,
    }
    failure_reason = (
        "Causal outcome-regime memory did not pass every preregistered "
        f"development gate: {summary['wins']}/{summary['trades']} wins "
        f"({summary['win_rate']:.2%}), Wilson "
        f"{summary['wilson_95_lower_bound']:.2%}, final-fold WR "
        f"{folds[-1]['win_rate']:.2%}."
    )
    output = {
        "version": SCHEMA_VERSION,
        "experiment_id": f"e4x-{base.stable_hash(identity)}",
        "thesis_family": THESIS_FAMILY,
        "identity": identity,
        "scientific_basis": (
            "Measured chronological decay motivated a preregistered causal "
            "market-health memory. Every feature excludes the current launch "
            "and uses only counterfactual outcomes fully observable 60.3s ago."
        ),
        "label_audit": metadata,
        "regime_feature_audit": {
            "feature_names": list(REGIME_FEATURE_NAMES),
            "rows": len(rows),
            "feature_matrix_sha256": base.stable_hash(regime_features.tolist()),
            "nonempty_short_rows": int(np.sum(regime_features[:, 0] > 0)),
            "nonempty_long_rows": int(np.sum(regime_features[:, 4] > 0)),
            "maximum_short_count": int(np.max(regime_features[:, 0])),
            "maximum_long_count": int(np.max(regime_features[:, 4])),
            "current_or_future_outcomes_used": False,
        },
        "aggregate": summary,
        "folds": [common.compact_fold(fold) for fold in folds],
        "requirements": requirements,
        "development_gate_passed": passed,
        "retired": not passed,
        "failure_classification": None if passed else "VALIDATION_COLLAPSE",
        "failure_reason": None if passed else failure_reason,
        "material_change_required_before_rerun": (
            "Freeze this exact identity and collect a new strictly later "
            "untouched holdout; no development retuning is permitted."
            if passed
            else "A genuinely new causal feature family or model family; "
            "outcome-window, threshold, and model-parameter variations are "
            "prohibited."
        ),
        "ready_for_new_strictly_later_holdout": passed,
        "anti_lookahead": {
            "fit_precedes_calibration": True,
            "calibration_precedes_test": True,
            "regime_outcomes_resolve_before_current_decision": True,
            "regime_resolution_delay_ms": RESOLUTION_DELAY_MS,
            "threshold_uses_prior_scores_only": True,
            "active_untouched_live_data_used": False,
        },
        "untouched_holdout_passed": False,
        "live_confirmation_authorised": False,
        "production_promotion_authorised": False,
        "production_deployment_authorised": False,
        "production_paths_changed": 0,
    }
    base.write_json(OUTPUT, output)
    print(json.dumps(output, indent=2, sort_keys=True))
    return output


if __name__ == "__main__":
    main()
