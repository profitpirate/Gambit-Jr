from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import statistics
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

FREQUENCIES = (0.0001, 0.00025, 0.0005, 0.001, 0.0025, 0.005, 0.01, 0.02, 0.05)
TARGETS = (2, 5, 10, 20)
SYSTEM_PROGRAM = "BwWK17cbHxwWBKZkUYvzxLcNQ1YVyaFezduWbtm2de6s"

WINDOWS = {
    "train": ("2026-06-05", "2026-06-21"),
    "calibration": ("2026-06-21", "2026-06-28"),
    "outer_1": ("2026-06-28", "2026-07-05"),
    "outer_2": ("2026-07-05", "2026-07-12"),
    "outer_3_partial": ("2026-07-12", "2026-07-15"),
}

FEATURE_GROUPS: dict[str, tuple[str, ...]] = {
    "piecewise_time": (
        "time_30s",
        "time_60s",
        "time_180s",
        "time_300s",
        "time_600s",
        "time_1800s",
    ),
    "market_cap_stage": (
        "log_market_cap",
        "curve_progress",
        "market_cap_growth",
        "price_return_pct",
    ),
    "buyer_order_flow": (
        "log_trade_count",
        "log_buy_count",
        "log_sell_count",
        "log_independent_buyers",
        "buyer_growth",
        "clean_buy_pressure",
        "clean_net_sol",
        "clean_trade_velocity",
    ),
    "momentum": ("momentum_score", "vertical_acceleration"),
    "creator": ("creator_success_rate", "log_creator_history"),
    "concentration": ("initial_top10_pct_corrected", "dev_buy_pct_corrected"),
    "entry_actionability": ("snapshot_staleness_seconds", "clean_flow_coverage"),
    "regime": ("regime_day", "launch_intensity"),
}

AVAILABLE_FEATURES = tuple(feature for family in FEATURE_GROUPS.values() for feature in family)


@dataclass(frozen=True, slots=True)
class ResearchData:
    mint: Any
    creator: Any
    decision_day: Any
    timestamp_seconds: Any
    peak_multiple: Any
    terminal_failure: Any
    max_adverse_excursion: Any
    graduated: Any
    features: Mapping[str, Any]
    control_score: Any

    def __len__(self) -> int:
        return len(self.mint)


@dataclass(frozen=True, slots=True)
class ConstantProbabilityEstimator:
    probability: float

    def predict_proba(self, matrix: Any) -> Any:
        import numpy as np

        probability = np.full(len(matrix), self.probability)
        return np.column_stack([1 - probability, probability])


@dataclass(frozen=True, slots=True)
class FittedProbability:
    estimator: Any
    calibrator: Any
    features: tuple[str, ...]
    kind: str
    target: int

    def probabilities(self, matrix: Any) -> Any:
        import numpy as np

        raw = self.estimator.predict_proba(matrix)[:, 1]
        clipped = np.clip(raw, 1e-8, 1 - 1e-8)
        logits = np.log(clipped / (1 - clipped)).reshape(-1, 1)
        if self.calibrator is None:
            return clipped
        return self.calibrator.predict_proba(logits)[:, 1]


def _dependencies() -> tuple[Any, Any]:
    try:
        import duckdb
        import numpy as np
    except ImportError as error:  # pragma: no cover - guarded by research extra
        raise RuntimeError("install the research extra: pip install -e .[research]") from error
    return duckdb, np


def load_available_data(database: str | Path, corpus: str | Path) -> ResearchData:
    """Load native landmark rows and reconstruct point-in-time valid order flow."""

    duckdb, np = _dependencies()
    corpus_path = Path(corpus)
    trades = (corpus_path / "trades" / "*.parquet").as_posix()
    tokens = (corpus_path / "tokens.parquet").as_posix()
    connection = duckdb.connect(str(database), read_only=True)
    query = f"""
      WITH landmarks(timestamp_seconds) AS (
        VALUES (30),(60),(180),(300),(600),(1800)
      ), valid_trade AS (
        SELECT *
        FROM read_parquet('{trades}')
        WHERE seconds_since_launch BETWEEN 0 AND 1800
          AND user_wallet <> '{SYSTEM_PROGRAM}'
          AND sol_amount IS NOT NULL AND token_amount IS NOT NULL AND price_sol IS NOT NULL
          AND token_amount*price_sol > 0
          AND sol_amount/(token_amount*price_sol) BETWEEN .01 AND 100
      ), flow_bucket AS (
        SELECT mint,
          CASE
            WHEN seconds_since_launch<=30 THEN 30
            WHEN seconds_since_launch<=60 THEN 60
            WHEN seconds_since_launch<=180 THEN 180
            WHEN seconds_since_launch<=300 THEN 300
            WHEN seconds_since_launch<=600 THEN 600
            ELSE 1800
          END bucket_seconds,
          count(*) valid_trades,
          count_if(is_buy) valid_buys,
          count_if(NOT is_buy) valid_sells,
          sum(CASE WHEN is_buy THEN sol_amount ELSE 0 END) buy_sol,
          sum(CASE WHEN NOT is_buy THEN sol_amount ELSE 0 END) sell_sol,
          sum(CASE WHEN is_buy THEN sol_amount ELSE -sol_amount END) net_sol
        FROM valid_trade
        GROUP BY mint,bucket_seconds
      ), buyer_bucket AS (
        SELECT DISTINCT mint,
          CASE
            WHEN seconds_since_launch<=30 THEN 30
            WHEN seconds_since_launch<=60 THEN 60
            WHEN seconds_since_launch<=180 THEN 180
            WHEN seconds_since_launch<=300 THEN 300
            WHEN seconds_since_launch<=600 THEN 600
            ELSE 1800
          END bucket_seconds,
          user_wallet
        FROM valid_trade
        WHERE is_buy
      ), buyer_flow AS (
        SELECT b.mint,l.timestamp_seconds,count(DISTINCT b.user_wallet) independent_buyers
        FROM buyer_bucket b
        JOIN landmarks l ON b.bucket_seconds <= l.timestamp_seconds
        GROUP BY b.mint,l.timestamp_seconds
      ), clean_flow AS (
        SELECT b.mint,l.timestamp_seconds,
          sum(b.valid_trades) valid_trades,
          sum(b.valid_buys) valid_buys,
          sum(b.valid_sells) valid_sells,
          sum(b.buy_sol) buy_sol,
          sum(b.sell_sol) sell_sol,
          sum(b.net_sol) net_sol
        FROM flow_bucket b
        JOIN landmarks l ON b.bucket_seconds <= l.timestamp_seconds
        GROUP BY b.mint,l.timestamp_seconds
      ), drawdown AS (
        SELECT mint,min(max_adverse_excursion) max_adverse_excursion
        FROM edge_3m GROUP BY mint
      )
      SELECT r.mint,t.creator,cast(r.decision_at AS DATE) decision_day,
        r.timestamp_seconds,
        r.peak_multiple,coalesce(r.terminal_failure,false) terminal_failure,
        d.max_adverse_excursion,r.graduated_at IS NOT NULL graduated,r.stage,
        r.current_market_cap,
        r.curve_progress,r.market_cap_growth,r.price_return_pct,r.trade_count,
        r.buy_count,r.sell_count,r.buyer_count,r.buyer_growth,r.momentum_score,
        r.vertical_acceleration,r.creator_past_tokens,r.creator_past_rugs,
        r.initial_top10_pct_corrected,r.dev_buy_pct_corrected,
        r.snapshot_staleness_seconds,r.creator_score,r.concentration_score,
        r.liquidity_score,r.survival_score,r.payoff_score,r.tradeability_score,
        r.buyer_growth_score,r.poor_tradeability,r.toxic_creator,
        r.concentration_unknown,r.buyer_collapse_proxy,
        f.valid_trades,f.valid_buys,f.valid_sells,bf.independent_buyers,
        f.buy_sol,f.sell_sol,f.net_sol
      FROM runner_autopsy_replay r
      JOIN read_parquet('{tokens}') t USING(mint)
      LEFT JOIN clean_flow f
        ON f.mint=r.mint AND f.timestamp_seconds=r.timestamp_seconds
      LEFT JOIN buyer_flow bf
        ON bf.mint=r.mint AND bf.timestamp_seconds=r.timestamp_seconds
      LEFT JOIN drawdown d USING(mint)
      WHERE r.timestamp_seconds IN (30,60,180,300,600,1800)
        AND r.evaluated AND r.market_cap_unit='SOL'
        AND r.current_market_cap BETWEEN .01 AND 1000000
        AND r.peak_multiple IS NOT NULL
        AND NOT coalesce(t.top10_pct_suspect,false)
        AND NOT (r.initial_top10_pct_corrected IS NOT NULL
                 AND r.initial_top10_pct_corrected>100)
      ORDER BY r.mint,r.timestamp_seconds
    """
    values = connection.execute(query).fetchnumpy()
    connection.close()

    def numeric(name: str) -> Any:
        value = values[name]
        if hasattr(value, "filled"):
            value = np.ma.asarray(value, dtype=float).filled(np.nan)
        return np.asarray(value, dtype=float)

    def safe_log(name: str) -> Any:
        return np.log1p(np.maximum(numeric(name), 0))

    creator_tokens = numeric("creator_past_tokens")
    creator_rugs = numeric("creator_past_rugs")
    creator_success = np.where(
        creator_tokens > 0,
        1 - np.minimum(creator_rugs / np.maximum(creator_tokens, 1), 1),
        0.5,
    )
    day_strings = np.asarray([str(value)[:10] for value in values["decision_day"]])
    day_ordinals = np.asarray(
        [(date.fromisoformat(value) - date(2026, 6, 5)).days for value in day_strings],
        dtype=float,
    )
    unique_days, day_counts = np.unique(day_strings, return_counts=True)
    ordered_counts = np.sort(day_counts)
    intensity = {
        day: float(np.searchsorted(ordered_counts, count, side="right") / len(ordered_counts))
        for day, count in zip(unique_days, day_counts, strict=True)
    }
    valid_trades = numeric("valid_trades")
    timestamp_seconds = numeric("timestamp_seconds")
    buy_sol = numeric("buy_sol")
    sell_sol = numeric("sell_sol")
    clean_total = buy_sol + sell_sol
    clean_pressure = np.divide(
        buy_sol,
        clean_total,
        out=np.full(len(day_strings), np.nan),
        where=clean_total > 0,
    )
    raw_trades = numeric("trade_count")
    clean_coverage = np.divide(
        valid_trades,
        raw_trades,
        out=np.zeros(len(day_strings)),
        where=raw_trades > 0,
    )
    feature_values = {
        "log_market_cap": safe_log("current_market_cap"),
        "curve_progress": np.clip(numeric("curve_progress"), 0, 100),
        "market_cap_growth": numeric("market_cap_growth"),
        "price_return_pct": numeric("price_return_pct"),
        "log_trade_count": safe_log("valid_trades"),
        "log_buy_count": safe_log("valid_buys"),
        "log_sell_count": safe_log("valid_sells"),
        "log_independent_buyers": safe_log("independent_buyers"),
        "buyer_growth": numeric("buyer_growth"),
        "clean_buy_pressure": clean_pressure,
        "clean_net_sol": numeric("net_sol"),
        "clean_trade_velocity": valid_trades / np.maximum(timestamp_seconds, 1),
        "momentum_score": numeric("momentum_score") / 100.0,
        "vertical_acceleration": numeric("vertical_acceleration"),
        "creator_success_rate": creator_success,
        "log_creator_history": np.log1p(np.maximum(creator_tokens, 0)),
        "initial_top10_pct_corrected": numeric("initial_top10_pct_corrected") / 100.0,
        "dev_buy_pct_corrected": numeric("dev_buy_pct_corrected") / 100.0,
        "snapshot_staleness_seconds": numeric("snapshot_staleness_seconds") / 60.0,
        "clean_flow_coverage": np.clip(clean_coverage, 0, 1),
        "regime_day": day_ordinals / max(day_ordinals.max(), 1),
        "launch_intensity": np.asarray([intensity[value] for value in day_strings]),
        **{
            f"time_{landmark}s": (timestamp_seconds == landmark).astype(float)
            for landmark in (30, 60, 180, 300, 600, 1800)
        },
    }
    control_score = _control_reconstruction(values, np)
    return ResearchData(
        mint=np.asarray(values["mint"]),
        creator=np.asarray(values["creator"]),
        decision_day=day_strings,
        timestamp_seconds=timestamp_seconds.astype(int),
        peak_multiple=numeric("peak_multiple"),
        terminal_failure=np.asarray(values["terminal_failure"], dtype=bool),
        max_adverse_excursion=numeric("max_adverse_excursion"),
        graduated=np.asarray(values["graduated"], dtype=bool),
        features=feature_values,
        control_score=control_score,
    )


def _control_reconstruction(values: Mapping[str, Any], np: Any) -> Any:
    def numeric(name: str) -> Any:
        value = values[name]
        if hasattr(value, "filled"):
            value = np.ma.asarray(value, dtype=float).filled(np.nan)
        return np.asarray(value, dtype=float)

    n = len(values["mint"])
    scores = np.zeros(n)
    stage = np.asarray(values["stage"])
    candidates = {
        "curve": numeric("curve_progress"),
        "momentum": numeric("momentum_score"),
        "buyers": numeric("buyer_growth_score"),
        "creator": numeric("creator_score"),
        "concentration": numeric("concentration_score"),
        "liquidity": numeric("liquidity_score"),
        "survival": numeric("survival_score"),
        "payoff": numeric("payoff_score"),
        "tradeability": numeric("tradeability_score"),
    }
    for index in range(n):
        if stage[index] == "MIGRATED":
            names = (
                "liquidity",
                "tradeability",
                "buyers",
                "buyers",
                "concentration",
                "momentum",
                "survival",
                "payoff",
            )
        elif stage[index] == "BONDING":
            names = ("curve", "momentum", "buyers", "buyers", "survival", "payoff")
        else:
            names = (
                "momentum",
                "concentration",
                "creator",
                "liquidity",
                "survival",
                "payoff",
            )
        known = [
            candidates[name][index] for name in names if math.isfinite(candidates[name][index])
        ]
        scores[index] = statistics.fmean(known) / 100 if known else 0.0
    return scores


def feature_matrix(data: ResearchData, features: Sequence[str], mask: Any) -> Any:
    _, np = _dependencies()
    return np.column_stack([data.features[name][mask] for name in features])


def chronological_masks(data: ResearchData) -> dict[str, Any]:
    _, np = _dependencies()
    masks = {
        name: (data.decision_day >= lower) & (data.decision_day < upper)
        for name, (lower, upper) in WINDOWS.items()
    }
    future = masks["calibration"] | masks["outer_1"] | masks["outer_2"] | masks["outer_3_partial"]
    future_creators = set(data.creator[future])
    outer_all_times = masks["outer_1"] | masks["outer_2"] | masks["outer_3_partial"]
    outer_creators = set(data.creator[outer_all_times])
    masks["train"] &= np.asarray(
        [creator not in future_creators for creator in data.creator], dtype=bool
    )
    masks["calibration"] &= np.asarray(
        [creator not in outer_creators for creator in data.creator], dtype=bool
    )
    masks["calibration_eval"] = masks["calibration"] & (data.timestamp_seconds == 60)
    for name in tuple(masks):
        if name.startswith("outer_"):
            masks[name] &= data.timestamp_seconds == 60
    return masks


def fit_probability_model(
    data: ResearchData,
    features: Sequence[str],
    train_mask: Any,
    calibration_mask: Any,
    target: int,
    *,
    kind: str,
) -> FittedProbability:
    labels = (data.peak_multiple >= target).astype(int)
    return fit_binary_probability(
        data,
        features,
        train_mask,
        calibration_mask,
        labels,
        target=target,
        kind=kind,
    )


def fit_binary_probability(
    data: ResearchData,
    features: Sequence[str],
    train_mask: Any,
    calibration_mask: Any,
    labels: Any,
    *,
    target: int,
    kind: str,
) -> FittedProbability:
    import numpy as np
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler

    train_x = feature_matrix(data, features, train_mask)
    train_y = labels[train_mask]
    if len(set(train_y.tolist())) < 2:
        estimator = ConstantProbabilityEstimator((float(train_y.sum()) + 1) / (len(train_y) + 2))
    elif kind == "logistic":
        estimator = Pipeline(
            [
                ("imputer", SimpleImputer(strategy="median", add_indicator=True)),
                ("scale", StandardScaler()),
                ("model", LogisticRegression(max_iter=300, C=0.35, solver="lbfgs")),
            ]
        )
    elif kind == "gradient_boosted":
        estimator = Pipeline(
            [
                ("imputer", SimpleImputer(strategy="median", add_indicator=True)),
                (
                    "model",
                    HistGradientBoostingClassifier(
                        learning_rate=0.07,
                        max_iter=100,
                        max_leaf_nodes=15,
                        min_samples_leaf=80,
                        l2_regularization=1.0,
                        random_state=17,
                    ),
                ),
            ]
        )
    else:
        raise ValueError(f"unknown model kind: {kind}")
    if not isinstance(estimator, ConstantProbabilityEstimator):
        estimator.fit(train_x, train_y)
    calibration_x = feature_matrix(data, features, calibration_mask)
    calibration_y = labels[calibration_mask]
    raw = np.clip(estimator.predict_proba(calibration_x)[:, 1], 1e-8, 1 - 1e-8)
    logits = np.log(raw / (1 - raw)).reshape(-1, 1)
    calibrator = None
    if calibration_y.sum() >= 10 and calibration_y.sum() < len(calibration_y):
        calibrator = LogisticRegression(C=1.0, max_iter=200).fit(logits, calibration_y)
    return FittedProbability(estimator, calibrator, tuple(features), kind, target)


def monotonic_probability_matrix(probabilities: Mapping[int, Any]) -> Any:
    _, np = _dependencies()
    matrix = np.column_stack([probabilities[target] for target in TARGETS])
    matrix[:, 0] = np.clip(matrix[:, 0], 0, 1)
    for column in range(1, matrix.shape[1]):
        matrix[:, column] = np.minimum(np.clip(matrix[:, column], 0, 1), matrix[:, column - 1])
    return matrix


def stable_random_scores(mints: Sequence[str]) -> Any:
    _, np = _dependencies()
    return np.asarray(
        [
            int(hashlib.sha256(str(mint).encode()).hexdigest()[:13], 16) / (16**13 - 1)
            for mint in mints
        ]
    )


def wilson_interval(
    successes: int, sample: int, z: float = 1.959963984540054
) -> list[float] | None:
    if sample <= 0:
        return None
    rate = successes / sample
    denominator = 1 + z * z / sample
    center = (rate + z * z / (2 * sample)) / denominator
    margin = z / denominator * math.sqrt(rate * (1 - rate) / sample + z * z / (4 * sample**2))
    return [max(0.0, center - margin), min(1.0, center + margin)]


def selection_metrics(
    data: ResearchData, mask: Any, scores: Any, frequency: float
) -> dict[str, Any]:
    _, np = _dependencies()
    local = np.flatnonzero(mask)
    count = max(1, round(len(local) * frequency))
    order = np.lexsort((data.mint[local].astype(str), -scores))
    selected = local[order[:count]]
    peak = data.peak_multiple[selected]
    universe_peak = data.peak_multiple[local]
    failures = data.terminal_failure[selected]
    adverse = data.max_adverse_excursion[selected]
    finite_adverse = adverse[np.isfinite(adverse)]
    result: dict[str, Any] = {
        "frequency": frequency,
        "signals": len(selected),
        "signals_per_day": len(selected) / max(1, len(set(data.decision_day[local]))),
        "signals_per_week": len(selected) / max(1, len(set(data.decision_day[local]))) * 7,
        "terminal_failure_rate": float(failures.mean()) if len(failures) else None,
        "liquidity_collapse_rate": None,
        "median_adverse_excursion": (
            float(np.median(finite_adverse)) if len(finite_adverse) else None
        ),
        "mae_coverage": len(finite_adverse) / len(selected) if len(selected) else None,
        "median_time_to_target": None,
        "copyability": None,
    }
    for target in (2, 3, 5, 10):
        successes = int((peak >= target).sum())
        result[f"{target}x_precision"] = successes / len(selected)
        result[f"{target}x_wilson_95"] = wilson_interval(successes, len(selected))
    for target in (20, 50):
        available = int((universe_peak >= target).sum())
        result[f"{target}x_recall"] = int((peak >= target).sum()) / available if available else None
    result["median_entry_market_cap"] = float(
        np.nanmedian(data.features["log_market_cap"][selected])
    )
    return result


def calibration_metrics(labels: Any, probabilities: Any, bins: int = 10) -> dict[str, Any]:
    import numpy as np
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score

    labels = np.asarray(labels, dtype=int)
    probabilities = np.clip(np.asarray(probabilities, dtype=float), 1e-8, 1 - 1e-8)
    squared_errors = (labels - probabilities) ** 2
    random = np.random.default_rng(17)
    bootstrap_brier = sorted(
        float(squared_errors[random.integers(0, len(labels), size=len(labels))].mean())
        for _ in range(100)
    )
    edges = np.linspace(0, 1, bins + 1)
    reliability = []
    ece = 0.0
    for index in range(bins):
        upper_inclusive = index == bins - 1
        chosen = (probabilities >= edges[index]) & (
            probabilities <= edges[index + 1]
            if upper_inclusive
            else probabilities < edges[index + 1]
        )
        if not chosen.any():
            continue
        predicted = float(probabilities[chosen].mean())
        observed = float(labels[chosen].mean())
        support = int(chosen.sum())
        ece += support / len(labels) * abs(predicted - observed)
        reliability.append(
            {
                "lower": edges[index],
                "upper": edges[index + 1],
                "support": support,
                "predicted": predicted,
                "observed": observed,
            }
        )
    logits = np.log(probabilities / (1 - probabilities))
    if len(set(labels)) > 1:
        calibration_fit = LogisticRegression(C=1e6, max_iter=300).fit(logits.reshape(-1, 1), labels)
        slope = float(calibration_fit.coef_[0, 0])
        intercept = float(calibration_fit.intercept_[0])
    else:
        slope, intercept = None, None
    return {
        "brier": float(brier_score_loss(labels, probabilities)),
        "brier_bootstrap_95": [bootstrap_brier[2], bootstrap_brier[97]],
        "probability_support_count": len(labels),
        "ece": ece,
        "pr_auc": float(average_precision_score(labels, probabilities)),
        "roc_auc": float(roc_auc_score(labels, probabilities)) if len(set(labels)) > 1 else None,
        "calibration_slope": slope,
        "calibration_intercept": intercept,
        "reliability": reliability,
    }


def _purge_creators(data: ResearchData, fit_mask: Any, held_mask: Any) -> Any:
    _, np = _dependencies()
    held = set(data.creator[held_mask])
    return fit_mask & np.asarray([creator not in held for creator in data.creator], dtype=bool)


def fit_two_stage(
    data: ResearchData,
    masks: Mapping[str, Any],
    features: Sequence[str],
    full_stage_a: Mapping[int, FittedProbability],
    full_failure: FittedProbability,
) -> tuple[Any, Any, tuple[str, ...]]:
    """Fit Stage B only on temporal out-of-fold Stage-A predictions."""

    import numpy as np
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler

    folds = (
        ("2026-06-05", "2026-06-11", "2026-06-11", "2026-06-13", "2026-06-13", "2026-06-16"),
        ("2026-06-05", "2026-06-16", "2026-06-16", "2026-06-18", "2026-06-18", "2026-06-21"),
    )
    oof_rows: list[Any] = []
    oof_targets: list[Any] = []
    gate_features = (
        "log_market_cap",
        "curve_progress",
        "clean_flow_coverage",
        "snapshot_staleness_seconds",
        "regime_day",
    )
    for train_start, train_end, cal_start, cal_end, held_start, held_end in folds:
        train = (data.decision_day >= train_start) & (data.decision_day < train_end)
        calibration = (data.decision_day >= cal_start) & (data.decision_day < cal_end)
        held = (data.decision_day >= held_start) & (data.decision_day < held_end)
        train = _purge_creators(data, train, calibration | held)
        calibration = _purge_creators(data, calibration, held)
        if train.sum() < 100 or calibration.sum() < 100 or held.sum() < 100:
            continue
        specialists = {
            target: fit_probability_model(
                data, features, train, calibration, target, kind="logistic"
            )
            for target in TARGETS
        }
        failure = fit_binary_probability(
            data,
            features,
            train,
            calibration,
            data.terminal_failure.astype(int),
            target=-1,
            kind="logistic",
        )
        raw = {
            target: specialists[target].probabilities(feature_matrix(data, features, held))
            for target in TARGETS
        }
        probabilities = monotonic_probability_matrix(raw)
        failure_probability = failure.probabilities(feature_matrix(data, features, held))
        gate_x = np.column_stack(
            [probabilities, failure_probability, feature_matrix(data, gate_features, held)]
        )
        oof_rows.append(gate_x)
        oof_targets.append((data.peak_multiple[held] >= 2).astype(int))
    if not oof_rows:
        raise ValueError("temporal cross-fitting produced no Stage-B rows")
    stage_b = Pipeline(
        [
            ("imputer", SimpleImputer(strategy="median", add_indicator=True)),
            ("scale", StandardScaler()),
            ("model", LogisticRegression(max_iter=300, C=0.25, solver="lbfgs")),
        ]
    ).fit(np.vstack(oof_rows), np.concatenate(oof_targets))
    calibration = masks["calibration"]
    stage_a_cal = monotonic_probability_matrix(
        {
            target: model.probabilities(feature_matrix(data, features, calibration))
            for target, model in full_stage_a.items()
        }
    )
    failure_cal = full_failure.probabilities(feature_matrix(data, features, calibration))
    gate_cal_x = np.column_stack(
        [stage_a_cal, failure_cal, feature_matrix(data, gate_features, calibration)]
    )
    raw = np.clip(stage_b.predict_proba(gate_cal_x)[:, 1], 1e-8, 1 - 1e-8)
    logits = np.log(raw / (1 - raw)).reshape(-1, 1)
    labels = (data.peak_multiple[calibration] >= 2).astype(int)
    calibrator = LogisticRegression(C=1.0, max_iter=200).fit(logits, labels)
    return stage_b, calibrator, gate_features


def two_stage_probabilities(
    data: ResearchData,
    mask: Any,
    features: Sequence[str],
    stage_a: Mapping[int, FittedProbability],
    failure_model: FittedProbability,
    stage_b: Any,
    calibrator: Any,
    gate_features: Sequence[str],
) -> Any:
    specialist = monotonic_probability_matrix(
        {
            target: model.probabilities(feature_matrix(data, features, mask))
            for target, model in stage_a.items()
        }
    )
    failure = failure_model.probabilities(feature_matrix(data, features, mask))
    return _two_stage_from_predictions(
        data,
        mask,
        specialist,
        failure,
        stage_b,
        calibrator,
        gate_features,
    )


def _two_stage_from_predictions(
    data: ResearchData,
    mask: Any,
    specialist: Any,
    failure: Any,
    stage_b: Any,
    calibrator: Any,
    gate_features: Sequence[str],
) -> Any:
    import numpy as np

    gate_x = np.column_stack([specialist, failure, feature_matrix(data, gate_features, mask)])
    raw = np.clip(stage_b.predict_proba(gate_x)[:, 1], 1e-8, 1 - 1e-8)
    logits = np.log(raw / (1 - raw)).reshape(-1, 1)
    return calibrator.predict_proba(logits)[:, 1]


def _audit_features(data: ResearchData) -> dict[str, Any]:
    _, np = _dependencies()
    result = {}
    boundaries = np.flatnonzero(np.concatenate(([True], data.mint[1:] != data.mint[:-1])))
    units = {
        "log_market_cap": "log1p_SOL",
        "curve_progress": "percent",
        "market_cap_growth": "source_ratio",
        "price_return_pct": "percent",
        "clean_buy_pressure": "ratio",
        "clean_net_sol": "SOL",
        "clean_trade_velocity": "trades_per_second",
        "snapshot_staleness_seconds": "minutes_scaled",
    }
    for name, values in data.features.items():
        finite = np.isfinite(values)
        token_known = np.maximum.reduceat(finite.astype(int), boundaries)
        result[name] = {
            "row_coverage": float(finite.mean()),
            "token_coverage": float(token_known.mean()),
            "date_start": min(data.decision_day.tolist()),
            "date_end": max(data.decision_day.tolist()),
            "point_in_time": True,
            "unit": units.get(name, "dimensionless"),
            "source": (
                "cleaned_raw_trade_reconstruction"
                if name.startswith(("clean_", "log_independent"))
                else "runner_autopsy_point_in_time_replay"
            ),
            "live_reproducible": name not in {"regime_day", "launch_intensity"},
            "license": "CC_BY_4_0_CORPUS",
            "missingness_informative": name
            in {"clean_buy_pressure", "clean_net_sol", "clean_flow_coverage"},
        }
    return result


def _prevalence(data: ResearchData, mask: Any) -> dict[str, Any]:
    peak = data.peak_multiple[mask]
    return {
        f"{target}x": {"count": int((peak >= target).sum()), "rate": float((peak >= target).mean())}
        for target in (2, 3, 5, 10, 20, 50)
    }


def _uncertainty_summary(
    data: ResearchData,
    train_mask: Any,
    outer_mask: Any,
    logistic_probabilities: Any,
    boosted_probabilities: Any,
    calibration_ece: float,
) -> dict[str, Any]:
    _, np = _dependencies()
    train_x = feature_matrix(data, AVAILABLE_FEATURES, train_mask)
    outer_x = feature_matrix(data, AVAILABLE_FEATURES, outer_mask)
    centers = np.nanmedian(train_x, axis=0)
    scales = np.nanpercentile(train_x, 75, axis=0) - np.nanpercentile(train_x, 25, axis=0)
    scales = np.where(scales > 1e-9, scales, 1.0)
    train_distance = np.nanmean(np.abs((train_x - centers) / scales), axis=1)
    outer_distance = np.nanmean(np.abs((outer_x - centers) / scales), axis=1)
    threshold = float(np.nanpercentile(train_distance, 99))
    ood = np.clip(outer_distance / max(threshold, 1e-9), 0, 2) / 2
    disagreement = np.mean(np.abs(logistic_probabilities - boosted_probabilities), axis=1)
    coverage = np.isfinite(outer_x).mean(axis=1)
    train_end = date.fromisoformat(WINDOWS["train"][1])
    regime_distance = np.asarray(
        [
            min(1.0, max(0, (date.fromisoformat(day) - train_end).days) / 24)
            for day in data.decision_day[outer_mask]
        ]
    )
    predictive = np.clip(
        0.35 * np.minimum(1.0, disagreement / 0.10)
        + 0.25 * ood
        + 0.15 * (1 - coverage)
        + 0.15 * min(1.0, calibration_ece / 0.05)
        + 0.10 * regime_distance,
        0,
        1,
    )

    def distribution(values: Any) -> dict[str, float]:
        return {
            "median": float(np.nanmedian(values)),
            "p90": float(np.nanpercentile(values, 90)),
            "maximum": float(np.nanmax(values)),
        }

    return {
        "evidence_coverage": distribution(coverage),
        "data_quality": distribution(coverage),
        "model_disagreement": distribution(disagreement),
        "calibration_uncertainty_ece": calibration_ece,
        "regime_distance": distribution(regime_distance),
        "out_of_distribution_score": distribution(ood),
        "out_of_distribution_rate": float((outer_distance > threshold).mean()),
        "predictive_uncertainty": distribution(predictive),
        "formula": (
            "0.35*scaled_model_disagreement + 0.25*OOD + 0.15*missingness + "
            "0.15*scaled_ECE + 0.10*regime_distance"
        ),
        "claim": "bounded transparent diagnostic; not Bayesian posterior uncertainty",
    }


def _persist_shadow_replay(
    output: str | Path | None,
    data: ResearchData,
    mask: Any,
    target_probabilities: Any,
    comparison_probabilities: Any,
    failure_probabilities: Any,
    calibration_probabilities: Any,
    calibration_ece: float,
    inference_seconds: float,
) -> dict[str, Any]:
    if output is None:
        return {
            "state": "NOT_RUN_NO_EXTERNAL_OUTPUT_REQUESTED",
            "public_route": False,
        }
    _, np = _dependencies()
    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    local = np.flatnonzero(mask)
    thresholds = np.quantile(calibration_probabilities, 0.99, axis=0)
    feature_matrix_outer = feature_matrix(data, AVAILABLE_FEATURES, mask)
    coverage = np.isfinite(feature_matrix_outer).mean(axis=1)
    disagreement = np.mean(np.abs(target_probabilities - comparison_probabilities), axis=1)
    regime_distance = np.asarray(
        [
            min(
                1.0,
                max(
                    0,
                    (date.fromisoformat(day) - date.fromisoformat(WINDOWS["train"][1])).days / 24,
                ),
            )
            for day in data.decision_day[mask]
        ]
    )
    predictive_uncertainty = np.clip(
        0.50 * np.minimum(1.0, disagreement / 0.10)
        + 0.20 * (1 - coverage)
        + 0.20 * min(1.0, calibration_ece / 0.05)
        + 0.10 * regime_distance,
        0,
        1,
    )
    connection = sqlite3.connect(output_path)
    try:
        with connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS intelligence_v3_research_replay ("
                "mint TEXT NOT NULL,decision_day TEXT NOT NULL,timestamp_seconds INTEGER NOT NULL,"
                "available_features_json TEXT NOT NULL,target_probabilities_json TEXT NOT NULL,"
                "failure_probabilities_json TEXT NOT NULL,actionability_json TEXT NOT NULL,"
                "uncertainty_json TEXT NOT NULL,nomination TEXT NOT NULL,research_tier TEXT NOT NULL,"
                "precision_gate TEXT NOT NULL,latency_json TEXT NOT NULL,model_version TEXT NOT NULL,"
                "feature_version TEXT NOT NULL,public_route INTEGER NOT NULL DEFAULT 0 CHECK(public_route=0),"
                "PRIMARY KEY(mint,decision_day,timestamp_seconds,model_version))"
            )
            connection.execute("DELETE FROM intelligence_v3_research_replay")

            def rows() -> Any:
                target_names = ("QUICK_2X", "MID_5X", "RIGHT_TAIL_10X", "EXTREME_RIGHT_TAIL_20X")
                for position, source_index in enumerate(local):
                    ratios = target_probabilities[position] / np.maximum(thresholds, 1e-12)
                    nominated = bool((ratios >= 1).any())
                    nomination = (
                        target_names[int(np.argmax(ratios))] if nominated else "NOT_NOMINATED"
                    )
                    known_features = {
                        name: float(data.features[name][source_index])
                        for name in AVAILABLE_FEATURES
                        if math.isfinite(float(data.features[name][source_index]))
                    }
                    probabilities = {
                        f"{target}x": float(target_probabilities[position, target_index])
                        for target_index, target in enumerate(TARGETS)
                    }
                    yield (
                        str(data.mint[source_index]),
                        str(data.decision_day[source_index]),
                        int(data.timestamp_seconds[source_index]),
                        json.dumps(known_features, sort_keys=True, separators=(",", ":")),
                        json.dumps(probabilities, sort_keys=True, separators=(",", ":")),
                        json.dumps(
                            {
                                "terminal_failure": float(failure_probabilities[position]),
                                "liquidity_failure": None,
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        json.dumps(
                            {
                                "entry_actionable_proxy": bool(
                                    data.features["clean_flow_coverage"][source_index] > 0
                                    and data.features["snapshot_staleness_seconds"][source_index]
                                    <= 2
                                ),
                                "copyability": None,
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        json.dumps(
                            {
                                "evidence_coverage": float(coverage[position]),
                                "model_disagreement": float(disagreement[position]),
                                "calibration_uncertainty_ece": calibration_ece,
                                "regime_distance": float(regime_distance[position]),
                                "out_of_distribution_score": None,
                                "predictive_uncertainty": float(predictive_uncertainty[position]),
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        nomination,
                        "SILENT_WATCH" if nominated else "NOT_NOMINATED",
                        "ABSTAIN_UNSEALED_VALIDATION",
                        json.dumps(
                            {
                                "batch_seconds": inference_seconds,
                                "per_candidate_ms": inference_seconds / max(1, len(local)) * 1000,
                                "provider_discord_user_latency": None,
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        "INTELLIGENCE_V3_TARGET_GRADIENT_AVAILABLE_DATA",
                        "V3_NATIVE_LANDMARK_FEATURES_2026_08_28",
                        0,
                    )

            connection.executemany(
                "INSERT INTO intelligence_v3_research_replay VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows(),
            )
        count = int(
            connection.execute("SELECT count(*) FROM intelligence_v3_research_replay").fetchone()[0]
        )
        public_count = int(
            connection.execute(
                "SELECT count(*) FROM intelligence_v3_research_replay WHERE public_route<>0"
            ).fetchone()[0]
        )
    finally:
        connection.close()
    digest = hashlib.sha256(output_path.read_bytes()).hexdigest()
    return {
        "state": "RETROSPECTIVE_REPLAY_PERSISTED_EXTERNAL_RESEARCH_ONLY",
        "rows": count,
        "database": str(output_path),
        "sha256": digest,
        "public_route_rows": public_count,
        "public_route_invariant": public_count == 0,
        "validation_gate": "ABSTAIN_UNSEALED_VALIDATION",
    }


def _tier_metrics(data: ResearchData, mask: Any, scores: Any) -> dict[str, Any]:
    _, np = _dependencies()
    calibration_count = len(scores)
    premium_count = min(calibration_count, max(75, round(calibration_count * 0.001)))
    combined_count = min(calibration_count, max(250, round(calibration_count * 0.01)))
    order = np.argsort(-scores)
    ordered = scores[order]
    labels = data.peak_multiple[mask][order] >= 2
    premium_rate = float(labels[:premium_count].mean())
    strong_rate = float(labels[premium_count:combined_count].mean())
    combined_rate = float(labels[:combined_count].mean())
    premium_threshold = float(ordered[premium_count - 1])
    strong_threshold = float(ordered[combined_count - 1])
    return {
        "premium_threshold": premium_threshold,
        "strong_threshold": strong_threshold,
        "threshold_policy": "INNER_CALIBRATION_MINIMUM_75_PREMIUM_175_STRONG",
        "calibration_premium_signals": premium_count,
        "calibration_strong_signals": combined_count - premium_count,
        "calibration_premium_2x_precision": premium_rate,
        "calibration_strong_2x_precision": strong_rate,
        "calibration_combined_2x_precision": combined_rate,
        "premium_target_met_on_calibration": premium_rate >= 0.70,
        "strong_target_met_on_calibration": strong_rate >= 0.55,
        "combined_target_met_on_calibration": combined_rate >= 0.60,
    }


def _evaluate_fixed_threshold(
    data: ResearchData, mask: Any, scores: Any, threshold: float, upper: float | None = None
) -> dict[str, Any]:
    _, np = _dependencies()
    local = np.flatnonzero(mask)
    chosen = scores >= threshold
    if upper is not None:
        chosen &= scores < upper
    selected = local[chosen]
    if not len(selected):
        return {
            "signals": 0,
            "2x_precision": None,
            "5x_precision": None,
            "10x_precision": None,
            "20x_recall": 0.0,
            "50x_recall": 0.0,
            "terminal_failure_rate": None,
            "median_adverse_excursion": None,
        }
    peak = data.peak_multiple[selected]
    universe = data.peak_multiple[local]
    return {
        "signals": len(selected),
        "2x_precision": float((peak >= 2).mean()),
        "2x_wilson_95": wilson_interval(int((peak >= 2).sum()), len(selected)),
        "5x_precision": float((peak >= 5).mean()),
        "10x_precision": float((peak >= 10).mean()),
        "20x_recall": float((peak >= 20).sum() / max(1, (universe >= 20).sum())),
        "50x_recall": float((peak >= 50).sum() / max(1, (universe >= 50).sum())),
        "terminal_failure_rate": float(data.terminal_failure[selected].mean()),
        "median_adverse_excursion": float(np.nanmedian(data.max_adverse_excursion[selected])),
    }


def run_available_data_experiment(
    database: str | Path,
    corpus: str | Path,
    *,
    shadow_output: str | Path | None = None,
) -> dict[str, Any]:
    import numpy as np
    from sklearn.metrics import average_precision_score

    data = load_available_data(database, corpus)
    masks = chronological_masks(data)
    train, calibration = masks["train"], masks["calibration"]
    outer = masks["outer_1"] | masks["outer_2"] | masks["outer_3_partial"]
    calibration_eval = masks["calibration_eval"]
    market_features = FEATURE_GROUPS["market_cap_stage"]
    flow_features = FEATURE_GROUPS["buyer_order_flow"]
    market_flow_features = (*market_features, *flow_features)
    all_features = AVAILABLE_FEATURES

    market_model = fit_probability_model(
        data, market_features, train, calibration, 2, kind="logistic"
    )
    flow_model = fit_probability_model(data, flow_features, train, calibration, 2, kind="logistic")
    market_flow_model = fit_probability_model(
        data, market_flow_features, train, calibration, 2, kind="logistic"
    )
    logistic = {
        target: fit_probability_model(
            data, all_features, train, calibration, target, kind="logistic"
        )
        for target in TARGETS
    }
    boosted = {
        target: fit_probability_model(
            data, all_features, train, calibration, target, kind="gradient_boosted"
        )
        for target in TARGETS
    }
    failure_model = fit_binary_probability(
        data,
        all_features,
        train,
        calibration,
        data.terminal_failure.astype(int),
        target=-1,
        kind="gradient_boosted",
    )
    stage_b, stage_b_calibrator, gate_features = fit_two_stage(
        data, masks, all_features, logistic, failure_model
    )

    evaluation_masks = {
        "all_outer": outer,
        **{name: masks[name] for name in WINDOWS if name.startswith("outer")},
    }
    models: dict[str, dict[str, Any]] = {}
    predictions: dict[str, dict[str, Any]] = {}
    inference_started = time.perf_counter()
    logistic_outer = monotonic_probability_matrix(
        {
            target: model.probabilities(feature_matrix(data, all_features, outer))
            for target, model in logistic.items()
        }
    )
    boosted_outer = monotonic_probability_matrix(
        {
            target: model.probabilities(feature_matrix(data, all_features, outer))
            for target, model in boosted.items()
        }
    )
    failure_outer = failure_model.probabilities(feature_matrix(data, all_features, outer))
    all_outer_predictions = {
        "random": stable_random_scores(data.mint[outer]),
        "market_cap_stage": market_model.probabilities(
            feature_matrix(data, market_features, outer)
        ),
        "buyer_order_flow": flow_model.probabilities(feature_matrix(data, flow_features, outer)),
        "market_cap_plus_buyer_flow": market_flow_model.probabilities(
            feature_matrix(data, market_flow_features, outer)
        ),
        "control_reconstruction": data.control_score[outer],
        "v3_target_specific_logistic": logistic_outer[:, 0],
        "v3_target_specific_gradient": boosted_outer[:, 0],
        "v3_two_stage": _two_stage_from_predictions(
            data,
            outer,
            logistic_outer,
            failure_outer,
            stage_b,
            stage_b_calibrator,
            gate_features,
        ),
    }
    target_inference_seconds = time.perf_counter() - inference_started
    for window, mask in evaluation_masks.items():
        selector = mask[outer]
        logistic_matrix = logistic_outer[selector]
        boosted_matrix = boosted_outer[selector]
        predictions[window] = {
            name: score[selector] for name, score in all_outer_predictions.items()
        }
        for name, score in predictions[window].items():
            models.setdefault(name, {})[window] = {
                "frontier": [
                    selection_metrics(data, mask, score, frequency) for frequency in FREQUENCIES
                ]
            }
        models["v3_target_specific_logistic"][window]["specialists"] = {
            f"{target}x": [
                selection_metrics(data, mask, logistic_matrix[:, index], frequency)
                for frequency in FREQUENCIES
            ]
            for index, target in enumerate(TARGETS)
        }
        models["v3_target_specific_gradient"][window]["specialists"] = {
            f"{target}x": [
                selection_metrics(data, mask, boosted_matrix[:, index], frequency)
                for frequency in FREQUENCIES
            ]
            for index, target in enumerate(TARGETS)
        }
    calibration_reports = {
        family: {
            f"{target}x": calibration_metrics(
                (data.peak_multiple[outer] >= target).astype(int), matrix[:, index]
            )
            for index, target in enumerate(TARGETS)
        }
        for family, matrix in {
            "logistic": logistic_outer,
            "gradient_boosted": boosted_outer,
        }.items()
    }
    calibration_reports["failure"] = {
        "terminal_failure": calibration_metrics(
            data.terminal_failure[outer].astype(int), failure_outer
        )
    }

    ablations = {}
    full_pr = average_precision_score(
        (data.peak_multiple[outer] >= 2).astype(int), logistic_outer[:, 0]
    )
    for family, removed in FEATURE_GROUPS.items():
        kept = tuple(feature for feature in all_features if feature not in removed)
        model = fit_probability_model(data, kept, train, calibration, 2, kind="logistic")
        score = model.probabilities(feature_matrix(data, kept, outer))
        pr_auc = average_precision_score((data.peak_multiple[outer] >= 2).astype(int), score)
        ablations[family] = {
            "state": "MEASURED",
            "pr_auc": float(pr_auc),
            "delta_vs_logistic_full": float(pr_auc - full_pr),
            "comparison_note": "same-family target-specific logistic ablation",
        }
    for unavailable in (
        "real_reserve_liquidity_velocity",
        "wallet_skill",
        "wallet_copyability",
        "wallet_consensus",
        "funder_cluster",
        "bundle_adjustment",
        "wash_adjustment",
        "social_infrastructure",
        "narrative",
        "terminal_failure_as_input",
    ):
        ablations[unavailable] = {"state": "NOT_AVAILABLE"}

    calibration_stage_score = two_stage_probabilities(
        data,
        calibration_eval,
        all_features,
        logistic,
        failure_model,
        stage_b,
        stage_b_calibrator,
        gate_features,
    )
    tier_policy = _tier_metrics(data, calibration_eval, calibration_stage_score)
    tier_results = {}
    for name, mask in evaluation_masks.items():
        score = predictions[name]["v3_two_stage"]
        tier_results[name] = {
            "premium": _evaluate_fixed_threshold(
                data, mask, score, tier_policy["premium_threshold"]
            ),
            "strong": _evaluate_fixed_threshold(
                data,
                mask,
                score,
                tier_policy["strong_threshold"],
                tier_policy["premium_threshold"],
            ),
        }

    stop_sensitivity = {}
    score = predictions["all_outer"]["v3_two_stage"]
    local = np.flatnonzero(outer)
    selected = local[np.argsort(-score)[: max(1, round(len(local) * 0.01))]]
    for stop in (0.30, 0.50, 0.70):
        known = selected[np.isfinite(data.max_adverse_excursion[selected])]
        success = (data.peak_multiple[known] >= 2) & (data.max_adverse_excursion[known] > -stop)
        stop_sensitivity[f"minus_{int(stop * 100)}_percent"] = {
            "sample": len(known),
            "conservative_2x_precision": float(success.mean()) if len(known) else None,
            "ordering_limitation": "MAE timing relative to target is unavailable",
        }
    stop_sensitivity["terminal_failure_only"] = {
        "sample": len(selected),
        "2x_precision": float((data.peak_multiple[selected] >= 2).mean()),
    }
    uncertainty = _uncertainty_summary(
        data,
        train,
        outer,
        logistic_outer,
        boosted_outer,
        calibration_reports["gradient_boosted"]["2x"]["ece"],
    )
    boosted_calibration = monotonic_probability_matrix(
        {
            target: model.probabilities(feature_matrix(data, all_features, calibration_eval))
            for target, model in boosted.items()
        }
    )
    shadow_replay = _persist_shadow_replay(
        shadow_output,
        data,
        outer,
        boosted_outer,
        logistic_outer,
        failure_outer,
        boosted_calibration,
        calibration_reports["gradient_boosted"]["2x"]["ece"],
        target_inference_seconds,
    )

    unavailable = {
        "exact_control": "production provider vectors absent",
        "real_sol_reserve": "virtual curve reserve is not an exact real reserve",
        "liquidity_collapse": "pre-graduation corpus has no point-in-time real-liquidity path",
        "seven_day_targets": "source outcomes mature at 48 hours",
        "sealed_test": "all June/July outcomes were already inspected in prior diagnostics",
        "wallet_copyability": "measured separately only where trade-price paths pass validity filters",
        "social_attention_velocity": "only infrastructure presence is available",
        "funder_cluster": "no complete PIT funder graph in this corpus",
    }
    feature_audit = _audit_features(data)
    return {
        "version": "INTELLIGENCE_V3_AVAILABLE_DATA_2026_08_28",
        "truth_state": "FITTED_CALIBRATED_RETROSPECTIVE_NOT_SEALED",
        "decision_horizon_seconds": 60,
        "fit_landmarks_seconds": [30, 60, 180, 300, 600, 1800],
        "evaluation_landmark_seconds": 60,
        "outcome_horizon_hours": 48,
        "windows": WINDOWS,
        "rows": {
            "loaded_observations": len(data),
            "loaded_unique_mints": len(set(data.mint.tolist())),
            **{name: int(mask.sum()) for name, mask in masks.items()},
        },
        "natural_prevalence": {
            name: _prevalence(data, mask) for name, mask in {**masks, "all_outer": outer}.items()
        },
        "training_prevalence": _prevalence(data, train),
        "effective_weighted_prevalence": _prevalence(data, train),
        "sampling": "NONE_NATURAL_PREVALENCE",
        "creator_group_purge": True,
        "maturity_embargo_hours": 48,
        "feature_audit": feature_audit,
        "feature_categories": {
            "available": list(AVAILABLE_FEATURES),
            "partially_available": [
                name for name, audit in feature_audit.items() if audit["row_coverage"] < 1
            ],
            "unavailable": sorted(unavailable),
            "corrupt_or_rejected": [
                "system-program wallet trades",
                "SOL/token/price unit ratio outside 0.01..100",
                "suspect top-10 concentration",
                "top-10 concentration above 100 percent",
            ],
            "live_reproducible": [
                name for name, audit in feature_audit.items() if audit["live_reproducible"]
            ],
            "research_only_licensed": list(AVAILABLE_FEATURES),
        },
        "models": models,
        "calibration": calibration_reports,
        "tier_policy": tier_policy,
        "tier_results": tier_results,
        "stop_sensitivity": stop_sensitivity,
        "primary_stop_policy": "TERMINAL_FAILURE_ONLY_V1",
        "uncertainty": uncertainty,
        "latency": {
            "target_specialist_batch_seconds": target_inference_seconds,
            "target_specialist_per_candidate_ms": (
                target_inference_seconds / max(1, int(outer.sum())) * 1000
            ),
            "measurement": "local retrospective batch inference",
        },
        "shadow_replay": shadow_replay,
        "ablations": ablations,
        "unavailable": unavailable,
        "approved_features": 0,
        "sealed_validation": False,
        "production_ready": False,
    }


def write_results(result: Mapping[str, Any], output: str | Path) -> None:
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, sort_keys=True, default=str) + "\n")


def run_red_pump_social_incremental(root: str | Path) -> dict[str, Any]:
    """Measure social-infrastructure increment on RED-PUMP without calling it attention."""

    import numpy as np
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import average_precision_score, brier_score_loss
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler

    duckdb, _ = _dependencies()
    root = Path(root)
    launches = (root / "red_pump_2026_v1_launches.jsonl.gz").as_posix()
    outcomes = (root / "red_pump_2026_v1_outcomes.csv.gz").as_posix()
    connection = duckdb.connect()
    query = f"""
      WITH launches AS (
        SELECT * EXCLUDE(rn) FROM (
          SELECT *,row_number() OVER(PARTITION BY mint ORDER BY seenAt) rn
          FROM read_json_auto('{launches}')
        ) WHERE rn=1
      ), outcomes AS (
        SELECT mint,outcome FROM read_csv(
          '{outcomes}',header=true,delim=',',strict_mode=false,
          null_padding=true,ignore_errors=true
        ) WHERE outcome IN ('GRADUATED','TIMEOUT')
          AND try_cast(minutes_to_outcome AS DOUBLE)>0
        QUALIFY row_number() OVER(PARTITION BY mint ORDER BY outcome)=1
      )
      SELECT l.mint,cast(epoch_ms(l.t) AS DATE) launch_day,
        extract(hour FROM epoch_ms(l.t)) launch_hour,l.initial_market_cap_sol,
        l.description_length,coalesce(l.has_twitter,false) has_twitter,
        coalesce(l.has_website,false) has_website,
        coalesce(l.has_telegram,false) has_telegram,
        o.outcome='GRADUATED' graduated
      FROM launches l JOIN outcomes o USING(mint)
      WHERE l.t IS NOT NULL
      ORDER BY l.t,l.mint
    """
    rows = connection.execute(query).fetchnumpy()
    connection.close()

    def numeric(name: str) -> Any:
        value = rows[name]
        if hasattr(value, "filled"):
            value = np.ma.asarray(value, dtype=float).filled(np.nan)
        return np.asarray(value, dtype=float)

    days = np.asarray([str(value)[:10] for value in rows["launch_day"]])
    day_index = np.asarray(
        [(date.fromisoformat(value) - date(2026, 5, 8)).days for value in days], dtype=float
    )
    twitter = numeric("has_twitter")
    website = numeric("has_website")
    telegram = numeric("has_telegram")
    y = np.asarray(rows["graduated"], dtype=int)
    base = np.column_stack(
        [
            np.log1p(np.maximum(numeric("initial_market_cap_sol"), 0)),
            numeric("description_length"),
            numeric("launch_hour"),
            day_index,
        ]
    )
    candidates = {
        "base": base,
        "base_plus_telegram": np.column_stack([base, telegram]),
        "base_plus_x": np.column_stack([base, twitter]),
        "base_plus_website": np.column_stack([base, website]),
        "base_plus_multi_platform": np.column_stack(
            [base, twitter, website, telegram, twitter + website + telegram]
        ),
    }
    train = days < "2026-05-25"
    calibration = (days >= "2026-05-25") & (days < "2026-06-01")
    test = days >= "2026-06-01"
    results = {}
    for name, matrix in candidates.items():
        estimator = Pipeline(
            [
                ("imputer", SimpleImputer(strategy="median", add_indicator=True)),
                ("scale", StandardScaler()),
                ("model", LogisticRegression(max_iter=300, C=0.5)),
            ]
        ).fit(matrix[train], y[train])
        raw_cal = np.clip(estimator.predict_proba(matrix[calibration])[:, 1], 1e-8, 1 - 1e-8)
        logits_cal = np.log(raw_cal / (1 - raw_cal)).reshape(-1, 1)
        calibrator = LogisticRegression(max_iter=200).fit(logits_cal, y[calibration])
        raw_test = np.clip(estimator.predict_proba(matrix[test])[:, 1], 1e-8, 1 - 1e-8)
        logits_test = np.log(raw_test / (1 - raw_test)).reshape(-1, 1)
        probabilities = calibrator.predict_proba(logits_test)[:, 1]
        results[name] = {
            "pr_auc": float(average_precision_score(y[test], probabilities)),
            "brier": float(brier_score_loss(y[test], probabilities)),
            "ece": calibration_metrics(y[test], probabilities)["ece"],
            "test_prevalence": float(y[test].mean()),
        }
    telegram_yes = y[telegram == 1]
    telegram_no = y[telegram == 0]
    return {
        "state": "MEASURED_RESEARCH_ONLY_COLLECTOR_BIASED",
        "rows": len(y),
        "dates": {
            "train": ["2026-05-08", "2026-05-24"],
            "calibration": ["2026-05-25", "2026-05-31"],
            "test": ["2026-06-01", "2026-06-10"],
        },
        "natural_prevalence": float(y.mean()),
        "raw_telegram_rate": float(telegram_yes.mean()),
        "raw_no_telegram_rate": float(telegram_no.mean()),
        "models": results,
        "telegram_incremental_pr_auc": (
            results["base_plus_telegram"]["pr_auc"] - results["base"]["pr_auc"]
        ),
        "limitations": [
            "collector newest-50 visibility is roughly six minutes, not 24 hours",
            "creator identity is unavailable in the launch release",
            "presence is infrastructure, not measured attention or sentiment",
            "association is not causal",
        ],
    }


def run_wallet_copyability_study(
    database: str | Path, corpus: str | Path, sample_modulus: int = 20
) -> dict[str, Any]:
    """Measure follower outcomes on a deterministic raw-trade wallet subset."""

    import numpy as np

    duckdb, _ = _dependencies()
    corpus = Path(corpus)
    trades = (corpus / "trades" / "*.parquet").as_posix()
    tokens = (corpus / "tokens.parquet").as_posix()
    outcomes = (corpus / "postgard_outcomes.parquet").as_posix()
    connection = duckdb.connect(str(database), read_only=True)
    query = f"""
      WITH valid_trade AS (
        SELECT mint,user_wallet,event_time,seconds_since_launch,is_buy,market_cap_sol
        FROM read_parquet('{trades}')
        WHERE seconds_since_launch>=0 AND user_wallet<>'{SYSTEM_PROGRAM}'
          AND sol_amount IS NOT NULL AND token_amount IS NOT NULL AND price_sol IS NOT NULL
          AND token_amount*price_sol>0
          AND sol_amount/(token_amount*price_sol) BETWEEN .01 AND 100
          AND market_cap_sol BETWEEN .01 AND 1000000
      ), entries AS (
        SELECT v.user_wallet,v.mint,min(v.seconds_since_launch) first_buy_seconds,
          min(v.event_time) first_buy_at
        FROM valid_trade v JOIN read_parquet('{tokens}') t USING(mint)
        WHERE v.is_buy AND v.user_wallet<>t.creator
        GROUP BY v.user_wallet,v.mint
      ), eligible AS (
        SELECT *,count(*) OVER(PARTITION BY user_wallet) launches_entered
        FROM entries
      ), requests AS (
        SELECT e.*,d.delay_seconds,e.first_buy_seconds+d.delay_seconds target_seconds
        FROM eligible e CROSS JOIN (VALUES (15),(30),(60),(120)) d(delay_seconds)
        WHERE e.launches_entered>=20 AND e.launches_entered<=500
          AND hash(e.user_wallet)%{sample_modulus}=0
      ), path AS (
        SELECT mint,seconds_since_launch,market_cap_sol,
          max(market_cap_sol) OVER(PARTITION BY mint ORDER BY seconds_since_launch DESC
            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) future_peak_mcap,
          min(market_cap_sol) OVER(PARTITION BY mint ORDER BY seconds_since_launch DESC
            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) future_min_mcap
        FROM valid_trade
      ), follower AS (
        SELECT r.*,p.seconds_since_launch priced_seconds,p.market_cap_sol entry_market_cap,
          p.future_peak_mcap,p.future_min_mcap
        FROM requests r ASOF LEFT JOIN path p
          ON r.mint=p.mint AND r.target_seconds<=p.seconds_since_launch
      )
      SELECT f.*,t.detected_at,t.creator,o.rug_detected,o.outcome_label
      FROM follower f JOIN read_parquet('{tokens}') t USING(mint)
      LEFT JOIN read_parquet('{outcomes}') o USING(mint)
      WHERE f.entry_market_cap IS NOT NULL AND f.future_peak_mcap IS NOT NULL
      ORDER BY f.delay_seconds,f.user_wallet,f.first_buy_at,f.mint
    """
    rows = connection.execute(query).fetchnumpy()
    connection.close()
    wallet = np.asarray(rows["user_wallet"])
    delay = np.asarray(rows["delay_seconds"], dtype=int)
    first_buy_at = np.asarray(rows["first_buy_at"])
    day = np.asarray([str(value)[:10] for value in first_buy_at])

    def numeric(name: str) -> Any:
        value = rows[name]
        if hasattr(value, "filled"):
            value = np.ma.asarray(value, dtype=float).filled(np.nan)
        return np.asarray(value, dtype=float)

    entry = numeric("entry_market_cap")
    peak = numeric("future_peak_mcap")
    trough = numeric("future_min_mcap")
    multiple = peak / entry
    adverse = trough / entry - 1
    results: dict[str, Any] = {}
    for seconds in (15, 30, 60, 120):
        chosen = delay == seconds
        train = chosen & (day < "2026-07-03")
        test = chosen & (day >= "2026-07-05")
        histories: dict[str, list[int]] = {}
        for index in np.flatnonzero(train):
            histories.setdefault(str(wallet[index]), []).append(index)
        eligible = {name: indexes for name, indexes in histories.items() if len(indexes) >= 5}
        skills = {
            name: ((sum(multiple[index] >= 2 for index in indexes) + 1) / (len(indexes) + 2))
            for name, indexes in eligible.items()
        }
        threshold = float(np.quantile(list(skills.values()), 0.9)) if skills else math.inf
        skilled = {name for name, score in skills.items() if score >= threshold}
        test_indexes = np.flatnonzero(test)
        selected = np.asarray(
            [index for index in test_indexes if str(wallet[index]) in skilled], dtype=int
        )
        overall_multiple = multiple[test_indexes]
        selected_multiple = multiple[selected] if len(selected) else np.asarray([])
        results[f"{seconds}s"] = {
            "priced_entries": int(chosen.sum()),
            "train_entries": int(train.sum()),
            "test_entries": len(test_indexes),
            "eligible_history_wallets": len(eligible),
            "top_decile_skilled_wallets": len(skilled),
            "selected_test_entries": len(selected),
            "overall_2x_rate": float((overall_multiple >= 2).mean())
            if len(overall_multiple)
            else None,
            "copyable_2x_skill": float((selected_multiple >= 2).mean())
            if len(selected_multiple)
            else None,
            "copyable_5x_skill": float((selected_multiple >= 5).mean())
            if len(selected_multiple)
            else None,
            "copyable_right_tail_skill": float((selected_multiple >= 10).mean())
            if len(selected_multiple)
            else None,
            "copyability_confidence_2x_wilson_95": wilson_interval(
                int((selected_multiple >= 2).sum()), len(selected_multiple)
            ),
            "median_adverse_excursion": float(np.nanmedian(adverse[selected]))
            if len(selected)
            else None,
        }
    return {
        "state": "MEASURED_DETERMINISTIC_SUBSET_PREGRAD_PRICE_PATH_ONLY",
        "sample_modulus": sample_modulus,
        "rows": len(wallet),
        "wallets": len(set(wallet)),
        "results": results,
        "exclusions": [
            "system program",
            "token creators",
            "unit-corrupt or missing SOL/price trades",
            "wallets with fewer than 20 or more than 500 launches",
            "one-hit-wonder histories with fewer than five matured training entries",
        ],
        "limitations": [
            "future peak is the valid pre-graduation trade path and omits post-migration upside",
            "insider, arbitrage, market-maker, funder and bundle labels are unavailable",
            "deterministic wallet sampling limits population inference",
            "independent-wallet consensus is not claimed without a complete linkage graph",
        ],
    }
