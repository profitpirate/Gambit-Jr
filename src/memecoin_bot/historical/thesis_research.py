from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from memecoin_bot.historical.intelligence_v3_execution import FREQUENCIES, selection_metrics
from memecoin_bot.historical.realtime_research import _dependencies, _load_replay_data, _mask


def _robust_scale(values: Any, development: Any, np: Any) -> Any:
    observed = np.asarray(values, dtype=float)
    fit = observed[development & np.isfinite(observed)]
    if not len(fit):
        return np.zeros(len(observed), dtype=float)
    median = float(np.median(fit))
    lower, upper = np.quantile(fit, [0.25, 0.75])
    scale = max(float(upper - lower), 1e-9)
    return np.clip(np.nan_to_num((observed - median) / scale, nan=-1.0), -4.0, 4.0)


def thesis_archetype_scores(data: Any, development: Any, np: Any) -> dict[str, Any]:
    """Construct pre-declared, label-free thesis scores using development scaling only."""

    feature = data.features
    trades_early = feature["b0_trade_count"] + feature["b1_trade_count"]
    buyers_early = feature["b0_new_buyer_count"] + feature["b1_new_buyer_count"]
    net_early = feature["b0_net_sol"] + feature["b1_net_sol"]
    trades_mid = feature["b2_trade_count"] + feature["b3_trade_count"]
    buyers_mid = feature["b2_new_buyer_count"] + feature["b3_new_buyer_count"]
    net_mid = feature["b2_net_sol"] + feature["b3_net_sol"]
    trades_late = feature["b4_trade_count"] + feature["b5_trade_count"]
    buyers_late = feature["b4_new_buyer_count"] + feature["b5_new_buyer_count"]
    net_late = feature["b4_net_sol"] + feature["b5_net_sol"]
    sell_values = np.column_stack(
        [feature["b4_sell_pressure"], feature["b5_sell_pressure"]]
    )
    sell_known = np.isfinite(sell_values)
    sell_late = np.divide(
        np.nansum(sell_values, axis=1),
        sell_known.sum(axis=1),
        out=np.ones(len(sell_values), dtype=float),
        where=sell_known.sum(axis=1) > 0,
    )
    absorption_observed = np.isfinite(feature["first_sell_seconds"]).astype(float)
    buyers_after_sell = feature["buyers_after_first_sell"]

    early_curve = (
        0.35 * _robust_scale(trades_early, development, np)
        + 0.35 * _robust_scale(buyers_early, development, np)
        + 0.30 * _robust_scale(net_early, development, np)
    )
    organic_acceleration = (
        0.20 * _robust_scale(trades_late - trades_mid, development, np)
        + 0.25 * _robust_scale(buyers_late - buyers_mid, development, np)
        + 0.25 * _robust_scale(net_late - net_mid, development, np)
        + 0.15 * _robust_scale(buyers_after_sell, development, np)
        + 0.15 * (1.0 - sell_late)
    )
    sell_absorption = (
        0.20 * absorption_observed
        + 0.30 * _robust_scale(buyers_after_sell, development, np)
        + 0.30 * _robust_scale(net_late, development, np)
        + 0.20 * (1.0 - sell_late)
    )
    revival = (
        0.35 * _robust_scale(trades_late - trades_mid, development, np)
        + 0.35 * _robust_scale(buyers_late - buyers_mid, development, np)
        + 0.30 * _robust_scale(net_late - net_mid, development, np)
    )
    runner = np.maximum.reduce([early_curve, organic_acceleration, sell_absorption, revival])
    failure = (
        0.45 * sell_late
        + 0.30 * _robust_scale(-net_late, development, np)
        + 0.25 * _robust_scale(-buyers_late, development, np)
    )
    actionable = runner - np.maximum(failure, 0) - 0.15 * _robust_scale(
        feature["log_market_cap"], development, np
    )
    return {
        "EARLY_CURVE_ACCELERATION": early_curve,
        "ORGANIC_ACCELERATION": organic_acceleration,
        "SECOND_LEG_SELL_ABSORPTION": sell_absorption,
        "REVIVAL": revival,
        "RUNNER_THESIS_MAX": runner,
        "INDEPENDENT_FAILURE_RISK": failure,
        "ACTIONABLE_THESIS": actionable,
    }


def run_runner_thesis_research(database: str | Path) -> dict[str, Any]:
    """Evaluate pre-declared runner archetypes on a retired chronological outer window."""

    duckdb, np = _dependencies()
    data, _ = _load_replay_data(database)
    development = _mask(data, "2026-06-05", "2026-06-21", np)
    calibration = _mask(data, "2026-06-21", "2026-06-28", np)
    outer = _mask(data, "2026-06-28", "2026-07-15", np)
    outer_creators = set(data.creator[outer])
    development &= np.asarray([creator not in outer_creators for creator in data.creator])
    calibration &= np.asarray([creator not in outer_creators for creator in data.creator])
    scores = thesis_archetype_scores(data, development, np)
    frontiers = {
        "CONTROL_RECONSTRUCTION": [
            selection_metrics(data, outer, data.control_score[outer], frequency)
            for frequency in FREQUENCIES
        ],
        **{
            name: [selection_metrics(data, outer, score[outer], frequency) for frequency in FREQUENCIES]
            for name, score in scores.items()
            if name != "INDEPENDENT_FAILURE_RISK"
        },
    }
    one_percent = {
        name: next(row for row in rows if row["frequency"] == 0.01)
        for name, rows in frontiers.items()
    }
    for rows in frontiers.values():
        for row in rows:
            if row["median_entry_market_cap"] is not None:
                row["median_entry_market_cap"] = float(
                    np.expm1(row["median_entry_market_cap"])
                )
    control_precision = one_percent["CONTROL_RECONSTRUCTION"]["2x_precision"] or 0.0
    candidate_precision = one_percent["ACTIONABLE_THESIS"]["2x_precision"] or 0.0
    result = {
        "version": "RUNNER_THESIS_ARCHETYPE_RESEARCH_V1",
        "truth_state": "RETROACTIVE_RETIRED_WINDOW_DIAGNOSTIC",
        "decision_horizon_seconds": 180,
        "development": {
            "start": "2026-06-05",
            "end": "2026-06-21",
            "rows": int(development.sum()),
            "scaling_fit_only_here": True,
        },
        "calibration": {
            "start": "2026-06-21",
            "end": "2026-06-28",
            "rows": int(calibration.sum()),
            "not_used_for_rule_discovery": True,
        },
        "outer": {
            "start": "2026-06-28",
            "end": "2026-07-15",
            "rows": int(outer.sum()),
            "retired_not_sealed": True,
        },
        "archetypes": [
            "EARLY_CURVE_ACCELERATION",
            "ORGANIC_ACCELERATION",
            "SECOND_LEG_SELL_ABSORPTION",
            "REVIVAL",
        ],
        "not_evaluable": {
            "SMART_WALLET_CONSENSUS": "wallet independence/linkage unavailable",
            "MIGRATION_CONTINUATION": "post-migration sequence unavailable at decision horizon",
        },
        "frontiers": frontiers,
        "one_percent": one_percent,
        "decision": (
            "PROSPECTIVE_SHADOW_REQUIRED"
            if candidate_precision > control_precision
            else "REJECTED_NO_FIXED_FREQUENCY_LIFT"
        ),
        "approved_features": 0,
        "public_route": False,
        "sealed_validation": False,
        "production_ready": False,
        "limitations": [
            "outer window was already inspected during prior V3 research",
            "only 40 days of deep transaction history are available",
            "real reserve, linked-wallet independence and provider latency are unavailable",
            "scores are rankings, not calibrated production probabilities",
        ],
    }
    connection = duckdb.connect(str(database))
    try:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS runner_thesis_research_runs_v15("
            "version VARCHAR PRIMARY KEY,created_at TIMESTAMPTZ DEFAULT current_timestamp,"
            "public_route BOOLEAN CHECK(public_route=false),result_json JSON NOT NULL)"
        )
        connection.execute(
            "INSERT OR REPLACE INTO runner_thesis_research_runs_v15 "
            "(version,public_route,result_json) VALUES(?,false,?)",
            (result["version"], json.dumps(result, default=str, sort_keys=True)),
        )
    finally:
        connection.close()
    return result


def write_runner_thesis_research(result: dict[str, Any], output: str | Path) -> None:
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, default=str, sort_keys=True), encoding="utf-8")
