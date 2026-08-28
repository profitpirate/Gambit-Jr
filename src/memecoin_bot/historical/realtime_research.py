from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from memecoin_bot.historical.intelligence_v3_execution import (
    FREQUENCIES,
    ResearchData,
    _control_reconstruction,
    feature_matrix,
    fit_binary_probability,
    fit_probability_model,
    selection_metrics,
)

BANDS = ("0-15", "15-30", "30-60", "60-90", "90-120", "120-180")


def _dependencies() -> tuple[Any, Any]:
    try:
        import duckdb
        import numpy as np
    except ImportError as exc:  # pragma: no cover - research extra guard
        raise RuntimeError("install the research extra: pip install -e .[research]") from exc
    return duckdb, np


def _numeric(values: Any, name: str, np: Any) -> Any:
    value = values[name]
    if hasattr(value, "filled"):
        value = np.ma.asarray(value, dtype=float).filled(np.nan)
    return np.asarray(value, dtype=float)


def _load_replay_data(database: str | Path) -> tuple[ResearchData, tuple[str, ...]]:
    duckdb, np = _dependencies()
    connection = duckdb.connect(str(database), read_only=True)
    band_features = []
    for index, band in enumerate(BANDS):
        for metric in ("trade_count", "new_buyer_count", "net_sol", "sell_pressure"):
            band_features.append(f"b{index}_{metric}")
    projections = []
    for index, band in enumerate(BANDS):
        projections.extend(
            (
                f"coalesce(max(b.trade_count) FILTER(b.band='{band}'),0) b{index}_trade_count",
                f"coalesce(max(b.new_buyer_count) FILTER(b.band='{band}'),0) b{index}_new_buyer_count",
                f"coalesce(max(b.net_sol) FILTER(b.band='{band}'),0) b{index}_net_sol",
                f"max(b.sell_sol/nullif(b.buy_sol+b.sell_sol,0)) FILTER(b.band='{band}') "
                + f"b{index}_sell_pressure",
            )
        )
    query = f"""
      WITH drawdown AS (
        SELECT mint,min(max_adverse_excursion) max_adverse_excursion
        FROM edge_3m GROUP BY mint
      ), trajectory AS (
        SELECT r.mint,x.creator,cast(r.decision_at AS DATE) decision_day,
          r.peak_multiple,coalesce(r.terminal_failure,false) terminal_failure,
          d.max_adverse_excursion,r.stage,r.current_market_cap,
          r.curve_progress,r.momentum_score,r.buyer_growth_score,r.creator_score,
          r.concentration_score,r.liquidity_score,r.survival_score,r.payoff_score,
          r.tradeability_score,{','.join(projections)},
          max(x.first_sell_seconds) first_sell_seconds,
          max(x.buyers_after_first_sell) buyers_after_first_sell
        FROM runner_autopsy_replay r
        JOIN realtime_token_trajectory_v15 x ON x.canonical_token=r.mint
        LEFT JOIN realtime_event_bands_v15 b ON b.canonical_token=r.mint
        LEFT JOIN drawdown d USING(mint)
        WHERE r.timestamp_seconds=180 AND r.evaluated AND r.market_cap_unit='SOL'
          AND r.current_market_cap BETWEEN .01 AND 1000000
          AND r.peak_multiple IS NOT NULL AND NOT coalesce(x.top10_pct_suspect,false)
          AND NOT (r.initial_top10_pct_corrected IS NOT NULL
                   AND r.initial_top10_pct_corrected>100)
        GROUP BY r.mint,x.creator,cast(r.decision_at AS DATE),r.peak_multiple,
          r.terminal_failure,d.max_adverse_excursion,r.stage,r.current_market_cap,
          r.curve_progress,r.momentum_score,r.buyer_growth_score,r.creator_score,
          r.concentration_score,r.liquidity_score,r.survival_score,r.payoff_score,
          r.tradeability_score
      ) SELECT * FROM trajectory ORDER BY mint
    """
    try:
        values = connection.execute(query).fetchnumpy()
    finally:
        connection.close()
    feature_values: dict[str, Any] = {}
    for name in band_features:
        value = _numeric(values, name, np)
        feature_values[name] = (
            np.log1p(np.maximum(value, 0))
            if name.endswith(("trade_count", "new_buyer_count"))
            else value
        )
    feature_values["first_sell_seconds"] = _numeric(values, "first_sell_seconds", np)
    feature_values["buyers_after_first_sell"] = np.log1p(
        np.maximum(_numeric(values, "buyers_after_first_sell", np), 0)
    )
    feature_values["log_market_cap"] = np.log1p(
        np.maximum(_numeric(values, "current_market_cap", np), 0)
    )
    all_features = tuple(feature_values)
    control = _control_reconstruction(values, np)
    data = ResearchData(
        mint=np.asarray(values["mint"]),
        creator=np.asarray(values["creator"]),
        decision_day=np.asarray([str(value)[:10] for value in values["decision_day"]]),
        timestamp_seconds=np.full(len(values["mint"]), 180),
        peak_multiple=_numeric(values, "peak_multiple", np),
        terminal_failure=np.asarray(values["terminal_failure"], dtype=bool),
        max_adverse_excursion=_numeric(values, "max_adverse_excursion", np),
        graduated=np.zeros(len(values["mint"]), dtype=bool),
        features=feature_values,
        control_score=control,
    )
    return data, all_features


def _mask(data: ResearchData, start: str, end: str, np: Any) -> Any:
    return np.asarray([(start <= day < end) for day in data.decision_day], dtype=bool)


def _cohort_autopsy(data: ResearchData, outer: Any, scores: Any, np: Any) -> dict[str, Any]:
    local = np.flatnonzero(outer)
    selected_count = max(1, round(len(local) * 0.01))
    selected = local[np.lexsort((data.mint[local].astype(str), -scores))[:selected_count]]
    true = selected[data.peak_multiple[selected] >= 2]
    false = selected[data.peak_multiple[selected] < 2]
    missed = local[(data.peak_multiple[local] >= 5) & ~np.isin(local, selected)]
    differences = []
    for name, values in data.features.items():
        false_values = values[false]
        true_values = values[true]
        missed_values = values[missed]
        differences.append(
            {
                "feature": name,
                "false_positive_minus_true_winner": (
                    float(np.nanmedian(false_values) - np.nanmedian(true_values))
                    if len(false_values) and len(true_values)
                    else None
                ),
                "missed_5x_minus_selected_true": (
                    float(np.nanmedian(missed_values) - np.nanmedian(true_values))
                    if len(missed_values) and len(true_values)
                    else None
                ),
            }
        )
    ranked = sorted(
        differences,
        key=lambda row: abs(row["false_positive_minus_true_winner"] or 0),
        reverse=True,
    )
    return {
        "selected_1pct": len(selected),
        "true_2x": len(true),
        "false_positives": len(false),
        "missed_5x": len(missed),
        "terminal_failures": int(data.terminal_failure[selected].sum()),
        "ranked_root_cause_differences": ranked[:10],
        "autopsy_scope": "retired_June_July_outer_transaction_trajectory",
    }


def _effects(
    data: ResearchData, development: Any, validation: Any, np: Any
) -> list[dict[str, Any]]:
    findings = []
    for name, values in data.features.items():
        row: dict[str, Any] = {"feature": name}
        for label, mask in (("development", development), ("validation", validation)):
            winner = values[mask & (data.peak_multiple >= 2)]
            other = values[mask & (data.peak_multiple < 2)]
            combined = values[mask]
            scale = float(np.nanstd(combined))
            row[f"{label}_effect"] = (
                float((np.nanmean(winner) - np.nanmean(other)) / scale)
                if len(winner) and len(other) and scale > 0
                else None
            )
        dev, valid = row["development_effect"], row["validation_effect"]
        row["status"] = (
            "CHALLENGER_RETIRED_WINDOW_ONLY"
            if dev is not None and valid is not None and dev * valid > 0
            else "REJECTED_UNSTABLE_DIRECTION"
        )
        row["human_approval_required"] = True
        findings.append(row)
    return sorted(
        findings,
        key=lambda row: abs(row["development_effect"] or 0),
        reverse=True,
    )


def run_realtime_trajectory_research(database: str | Path) -> dict[str, Any]:
    """Fit a transaction-trajectory challenger without altering production routing."""
    _, np = _dependencies()
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler

    data, features = _load_replay_data(database)
    train = _mask(data, "2026-06-05", "2026-06-21", np)
    calibration = _mask(data, "2026-06-21", "2026-06-28", np)
    outer = _mask(data, "2026-06-28", "2026-07-15", np)
    outer_creators = set(data.creator[outer])
    train &= np.asarray([creator not in outer_creators for creator in data.creator])
    calibration &= np.asarray([creator not in outer_creators for creator in data.creator])
    candidate = fit_probability_model(
        data, features, train, calibration, 2, kind="logistic"
    )
    failure = fit_binary_probability(
        data,
        features,
        train,
        calibration,
        data.terminal_failure.astype(int),
        target=-1,
        kind="logistic",
    )
    candidate_cal = candidate.probabilities(feature_matrix(data, features, calibration))
    failure_cal = failure.probabilities(feature_matrix(data, features, calibration))
    hybrid = Pipeline(
        [
            ("imputer", SimpleImputer(strategy="median", add_indicator=True)),
            ("scale", StandardScaler()),
            ("model", LogisticRegression(max_iter=300, C=0.35, solver="lbfgs")),
        ]
    ).fit(
        np.column_stack(
            [data.control_score[calibration], candidate_cal, failure_cal]
        ),
        (data.peak_multiple[calibration] >= 2).astype(int),
    )
    candidate_outer = candidate.probabilities(feature_matrix(data, features, outer))
    failure_outer = failure.probabilities(feature_matrix(data, features, outer))
    hybrid_outer = hybrid.predict_proba(
        np.column_stack([data.control_score[outer], candidate_outer, failure_outer])
    )[:, 1]
    models = {
        "CONTROL_RECONSTRUCTION": data.control_score[outer],
        "REALTIME_TRANSACTION_TRAJECTORY": candidate_outer,
        "CONTROL_X_REALTIME_FAILURE_FILTER": hybrid_outer,
    }
    frontiers = {
        name: [selection_metrics(data, outer, score, frequency) for frequency in FREQUENCIES]
        for name, score in models.items()
    }
    for rows in frontiers.values():
        for row in rows:
            if row["median_entry_market_cap"] is not None:
                row["median_entry_market_cap"] = float(
                    np.expm1(row["median_entry_market_cap"])
                )
    autopsy = _cohort_autopsy(data, outer, candidate_outer, np)
    hypotheses = _effects(data, train, outer, np)
    one_percent = {
        name: next(row for row in rows if row["frequency"] == 0.01)
        for name, rows in frontiers.items()
    }
    control_precision = one_percent["CONTROL_RECONSTRUCTION"]["2x_precision"] or 0
    candidate_precision = one_percent["REALTIME_TRANSACTION_TRAJECTORY"]["2x_precision"] or 0
    hybrid_precision = one_percent["CONTROL_X_REALTIME_FAILURE_FILTER"]["2x_precision"] or 0
    result = {
        "version": "REALTIME_TRANSACTION_TRAJECTORY_RESEARCH_V1",
        "truth_state": "FITTED_RETROSPECTIVE_RETIRED_WINDOWS_NOT_SEALED",
        "decision_horizon_seconds": 180,
        "development": {"start": "2026-06-05", "end": "2026-06-21", "rows": int(train.sum())},
        "calibration": {
            "start": "2026-06-21",
            "end": "2026-06-28",
            "rows": int(calibration.sum()),
            "hybrid_fit_only_here": True,
        },
        "outer": {"start": "2026-06-28", "end": "2026-07-15", "rows": int(outer.sum())},
        "features": list(features),
        "frontiers": frontiers,
        "one_percent": one_percent,
        "low_performance_autopsy": autopsy,
        "hypotheses": hypotheses[:20],
        "decisions": {
            "REALTIME_TRANSACTION_TRAJECTORY": (
                "REJECTED_NO_FIXED_FREQUENCY_LIFT"
                if candidate_precision <= control_precision
                else "CHALLENGER_PROSPECTIVE_VALIDATION_REQUIRED"
            ),
            "CONTROL_X_REALTIME_FAILURE_FILTER": (
                "REJECTED_NO_FIXED_FREQUENCY_LIFT"
                if hybrid_precision <= control_precision
                else "CHALLENGER_PROSPECTIVE_VALIDATION_REQUIRED"
            ),
        },
        "approved_features": 0,
        "public_route": False,
        "sealed_validation": False,
        "production_ready": False,
        "limitations": {
            "real_reserves": "UNAVAILABLE",
            "wallet_linkage": "UNAVAILABLE",
            "provider_latency": "UNAVAILABLE",
            "outer_window": "RETIRED_BY_PRIOR_V3_DIAGNOSTICS",
        },
    }
    connection = _dependencies()[0].connect(str(database))
    try:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS realtime_research_runs_v15("
            "version VARCHAR PRIMARY KEY,created_at TIMESTAMPTZ DEFAULT current_timestamp,"
            "public_route BOOLEAN CHECK(public_route=false),result_json JSON NOT NULL)"
        )
        connection.execute(
            "INSERT OR REPLACE INTO realtime_research_runs_v15(version,public_route,result_json) "
            "VALUES(?,false,?)",
            (result["version"], json.dumps(result, default=str, sort_keys=True)),
        )
    finally:
        connection.close()
    return result


def write_realtime_research(result: dict[str, Any], output: str | Path) -> None:
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, default=str, sort_keys=True), encoding="utf-8")
