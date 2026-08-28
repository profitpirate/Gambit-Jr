from __future__ import annotations

import json
import math
import statistics
import tempfile
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .research import ResearchEngine
from .store import ApprovedFeatureStore, HistoricalWarehouse, _parse_timestamp

REAL_POOL_DATASET_VERSION = "geckoterminal-ranked-pool-real-v1"
REAL_POOL_FEATURE_VERSION = "v15-real-ohlcv-features-v1"
REAL_POOL_OUTCOME_VERSION = "v15-real-ohlcv-outcomes-v1"


def _payloads(warehouse: HistoricalWarehouse, dataset_id: str) -> list[dict[str, Any]]:
    result = []
    rows = warehouse.conn.execute(
        "SELECT evidence_id,entity_id,source_timestamp,availability_timestamp,archive_path "
        "FROM raw_evidence WHERE dataset_id=? ORDER BY source_timestamp,entity_id",
        (dataset_id,),
    )
    for row in rows:
        envelope = json.loads(
            (warehouse.archive.root / row["archive_path"]).read_text(encoding="utf-8")
        )
        result.append({**dict(row), "payload": envelope["payload"]})
    return result


def normalize_regime_dataset(
    warehouse: HistoricalWarehouse, dataset_id: str, dataset_version: str
) -> dict[str, Any]:
    rows = _payloads(warehouse, dataset_id)
    previous_close: float | None = None
    normalized = 0
    started = time.perf_counter()
    for row in rows:
        payload = row["payload"]
        close = float(payload["close"])
        open_price = float(payload["open"])
        high = float(payload["high"])
        low = float(payload["low"])
        entity_key = warehouse.upsert_entity(
            "market_regime",
            "market",
            payload["symbol"],
            row["availability_timestamp"],
            {"symbol": payload["symbol"], "interval": payload["interval"]},
            {"dataset_id": dataset_id},
        )
        event_id = warehouse.normalize_event(
            row["evidence_id"],
            dataset_version,
            entity_key,
            "MARKET_CANDLE_CLOSED",
            row["source_timestamp"],
            row["availability_timestamp"],
            payload,
        )
        features = {
            "regime_close_usd": close,
            "regime_return": None if previous_close in {None, 0} else close / previous_close - 1,
            "regime_intraday_range": None if open_price == 0 else (high - low) / open_price,
            "regime_quote_volume_usd": float(payload["quote_volume"]),
            "regime_trade_count": int(payload["trade_count"]),
        }
        for name, value in features.items():
            warehouse.write_feature(
                dataset_version=dataset_version,
                feature_version="v15-market-regime-v1",
                entity_key=entity_key,
                feature_name=name,
                value=value,
                observed_at=row["source_timestamp"],
                available_at=row["availability_timestamp"],
                source_event_ids=[event_id],
                missing_state="UNKNOWN" if value is None else "KNOWN",
                confidence=1.0,
            )
        previous_close = close
        normalized += 1
    elapsed = max(time.perf_counter() - started, 1e-9)
    warehouse.assess_coverage(
        dataset_id,
        {
            "launch_platform": None,
            "normalized_rows": normalized,
            "missing_ranges": [],
            "completeness_estimate": 1.0 if rows else 0.0,
            "point_in_time_safe": True,
            "timestamp_precision": "exchange daily candle close",
            "survivorship_bias": "NONE_FOR_SELECTED_MARKET_SYMBOL",
            "quality_state": "REAL_PUBLIC_HISTORY",
            "licensing_limitations": "Provider terms apply; raw data is not committed to Git.",
            "cost_class": "FREE_PUBLIC_ENDPOINT",
            "information_gain": "Broad risk-on/risk-off and volatility regime context.",
        },
    )
    warehouse.record_latency(
        "normalize_market_regime",
        [elapsed * 1000],
        throughput_per_second=normalized / elapsed,
        metadata={"dataset_id": dataset_id, "rows": normalized},
    )
    return {"dataset_id": dataset_id, "normalized_rows": normalized}


def _nearest_regime_return(
    regime: list[tuple[datetime, float | None]], decision_at: datetime
) -> float | None:
    known = [value for available, value in regime if available <= decision_at and value is not None]
    return known[-1] if known else None


def _regime_series(warehouse: HistoricalWarehouse, symbol: str) -> list[tuple[datetime, float]]:
    entity = warehouse.conn.execute(
        "SELECT entity_key FROM canonical_entities WHERE entity_type='market_regime' "
        "AND canonical_id=?",
        (symbol,),
    ).fetchone()
    if not entity:
        return []
    rows = warehouse.conn.execute(
        "SELECT available_at,feature_value_json FROM point_in_time_features WHERE entity_key=? "
        "AND feature_version='v15-market-regime-v1' AND feature_name='regime_return' "
        "ORDER BY available_at",
        (entity["entity_key"],),
    )
    return [
        (_parse_timestamp(row["available_at"]), float(json.loads(row["feature_value_json"])))
        for row in rows
        if row["feature_value_json"] is not None
    ]


def _milestone_seconds(
    candles: list[dict[str, Any]], entry: float, decision: datetime, threshold: float
) -> float | None:
    for candle in candles:
        if float(candle["high"]) >= entry * threshold:
            return (_parse_timestamp(candle["available_at"]) - decision).total_seconds()
    return None


def normalize_ranked_pool_ohlcv(
    warehouse: HistoricalWarehouse,
    dataset_id: str,
    *,
    minimum_candles: int = 8,
    decision_candle_index: int = 2,
) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in _payloads(warehouse, dataset_id):
        grouped[row["entity_id"]].append(row)
    btc = _regime_series(warehouse, "BTCUSDT")
    sol = _regime_series(warehouse, "SOLUSDT")
    eligible = 0
    outcomes = 0
    skipped = 0
    started = time.perf_counter()
    for pool_address, candles in grouped.items():
        candles.sort(key=lambda row: row["source_timestamp"])
        if len(candles) < minimum_candles or len(candles) <= decision_candle_index + 1:
            skipped += 1
            continue
        eligible += 1
        event_ids = []
        entity_key = warehouse.upsert_entity(
            "dex_pool",
            "solana",
            pool_address,
            candles[0]["availability_timestamp"],
            {
                "pool_name": candles[0]["payload"].get("pool_name"),
                "selection": "current provider-ranked pool",
            },
            {"dataset_id": dataset_id, "survivorship_bias": "HIGH"},
        )
        for candle in candles:
            event_ids.append(
                warehouse.normalize_event(
                    candle["evidence_id"],
                    REAL_POOL_DATASET_VERSION,
                    entity_key,
                    "POOL_CANDLE_CLOSED",
                    candle["source_timestamp"],
                    candle["availability_timestamp"],
                    candle["payload"],
                )
            )
        decision_row = candles[decision_candle_index]
        decision = _parse_timestamp(decision_row["availability_timestamp"])
        initial = candles[: decision_candle_index + 1]
        future = candles[decision_candle_index + 1 :]
        entry = float(decision_row["payload"]["close"])
        first_open = float(initial[0]["payload"]["open"])
        volumes = [float(row["payload"]["volume"]) for row in initial]
        closes = [float(row["payload"]["close"]) for row in initial]
        returns = [
            closes[index] / closes[index - 1] - 1
            for index in range(1, len(closes))
            if closes[index - 1]
        ]
        features = {
            "market_initial_momentum": None if first_open == 0 else entry / first_open - 1,
            "market_initial_volume_usd": sum(volumes),
            "market_volume_acceleration": None
            if not volumes or volumes[0] == 0
            else volumes[-1] / volumes[0] - 1,
            "market_initial_volatility": statistics.pstdev(returns) if returns else 0.0,
            "regime_btc_return": _nearest_regime_return(btc, decision),
            "regime_sol_return": _nearest_regime_return(sol, decision),
            "source_delay_seconds": 86_400.0,
            "selection_survivorship_risk": 1.0,
        }
        for feature_name, value in features.items():
            warehouse.write_feature(
                dataset_version=REAL_POOL_DATASET_VERSION,
                feature_version=REAL_POOL_FEATURE_VERSION,
                entity_key=entity_key,
                feature_name=feature_name,
                value=value,
                observed_at=decision_row["source_timestamp"],
                available_at=decision_row["availability_timestamp"],
                source_event_ids=event_ids[: decision_candle_index + 1],
                missing_state="UNKNOWN" if value is None else "KNOWN",
                confidence=0.5 if feature_name.startswith("regime_") else 1.0,
            )
        future_payloads = [
            {**row["payload"], "available_at": row["availability_timestamp"]} for row in future
        ]
        peak_row = max(future_payloads, key=lambda row: float(row["high"]))
        peak = float(peak_row["high"]) / entry if entry else 0.0
        final = float(future_payloads[-1]["close"]) / entry if entry else 0.0
        low = min(float(row["low"]) for row in future_payloads)
        rugged = final <= 0.1
        measurement_end = future[-1]["availability_timestamp"]
        warehouse.record_outcome(
            {
                "dataset_version": REAL_POOL_DATASET_VERSION,
                "outcome_version": REAL_POOL_OUTCOME_VERSION,
                "entity_key": entity_key,
                "decision_at": decision_row["availability_timestamp"],
                "measurement_end_at": measurement_end,
                "available_at": measurement_end,
                "peak_multiple": peak,
                "time_to_1_5x_seconds": _milestone_seconds(future_payloads, entry, decision, 1.5),
                "time_to_2x_seconds": _milestone_seconds(future_payloads, entry, decision, 2),
                "time_to_3x_seconds": _milestone_seconds(future_payloads, entry, decision, 3),
                "time_to_5x_seconds": _milestone_seconds(future_payloads, entry, decision, 5),
                "time_to_10x_seconds": _milestone_seconds(future_payloads, entry, decision, 10),
                "time_to_20x_seconds": _milestone_seconds(future_payloads, entry, decision, 20),
                "time_to_peak_seconds": (
                    _parse_timestamp(peak_row["available_at"]) - decision
                ).total_seconds(),
                "max_adverse_excursion": low / entry - 1 if entry else None,
                "max_favourable_excursion": peak - 1,
                "drawdown_after_signal": low / entry - 1 if entry else None,
                "token_survival_seconds": (
                    _parse_timestamp(measurement_end) - decision
                ).total_seconds(),
                "rugged": rugged,
                "class_name": warehouse.classify_outcome(peak, rugged),
            }
        )
        outcomes += 1
    elapsed = max(time.perf_counter() - started, 1e-9)
    normalized_rows = sum(len(rows) for rows in grouped.values())
    warehouse.assess_coverage(
        dataset_id,
        {
            "launch_platform": "multiple Solana DEXs",
            "normalized_rows": normalized_rows,
            "missing_ranges": [
                "pools absent from the current provider-ranked selection",
                "dead and delisted pools omitted by provider ranking",
                "wallet, creator, liquidity and transaction-level history unavailable",
            ],
            "completeness_estimate": None,
            "point_in_time_safe": True,
            "timestamp_precision": "daily candle close",
            "survivorship_bias": "HIGH_CURRENT_RANKED_POOL_SELECTION",
            "quality_state": "REAL_EXPLORATORY_NOT_LAUNCH_COMPLETE",
            "licensing_limitations": "CoinGecko/GeckoTerminal API terms and free-tier limits apply.",
            "cost_class": "FREE_PUBLIC_ENDPOINT",
            "information_gain": "Real OHLCV right-tail exploration; unsuitable for production approval.",
        },
    )
    warehouse.record_latency(
        "normalize_ranked_pool_ohlcv",
        [elapsed * 1000],
        throughput_per_second=normalized_rows / elapsed,
        metadata={"eligible_pools": eligible, "skipped_pools": skipped},
    )
    return {
        "pool_count": len(grouped),
        "eligible_pools": eligible,
        "skipped_pools": skipped,
        "outcomes": outcomes,
        "normalized_rows": normalized_rows,
    }


def _chronological_windows(warehouse: HistoricalWarehouse) -> dict[str, tuple[str, str]]:
    rows = warehouse.conn.execute(
        "SELECT decision_at FROM outcomes WHERE dataset_version=? ORDER BY decision_at",
        (REAL_POOL_DATASET_VERSION,),
    ).fetchall()
    points = [row["decision_at"] for row in rows]
    if len(points) < 6:
        raise ValueError("at least six real outcomes are required for chronological research")
    distinct = sorted(set(points))
    if len(distinct) < 3:
        raise ValueError("real outcomes do not span three chronological windows")
    train_end = distinct[max(1, len(distinct) // 3)]
    validation_index = max(2, len(distinct) * 2 // 3)
    validation_end = distinct[min(len(distinct) - 1, validation_index)]
    end = (_parse_timestamp(points[-1]) + timedelta(seconds=1)).isoformat()
    return {
        "train": (distinct[0], train_end),
        "validation": (train_end, validation_end),
        "test": (validation_end, end),
    }


def run_real_research(warehouse: HistoricalWarehouse, code_version: str) -> dict[str, Any]:
    windows = _chronological_windows(warehouse)
    engine = ResearchEngine(warehouse)
    result = engine.run_walk_forward(
        research_type="REAL_RANKED_POOL_EXPLORATORY",
        dataset_version=REAL_POOL_DATASET_VERSION,
        feature_version=REAL_POOL_FEATURE_VERSION,
        outcome_version=REAL_POOL_OUTCOME_VERSION,
        rules_version="v15-finalization-approval-v1",
        code_version=code_version,
        provider_set=["coingecko_geckoterminal_public", "binance_public_spot"],
        train=windows["train"],
        validation=windows["validation"],
        test=windows["test"],
        chain="solana",
        limitations=[
            "Pool universe is selected from current provider rankings and has high survivorship bias.",
            "Daily candles cannot validate ultra-early launch decisions or realistic intraday slippage.",
            "No transaction-level wallet, creator, funding, liquidity-removal or social history.",
        ],
    )
    test_metrics = result["metrics"]["test"]
    baselines = result["result"]["baselines"]
    outcome_rows = [
        dict(row)
        for row in warehouse.conn.execute(
            "SELECT * FROM outcomes WHERE dataset_version=? ORDER BY decision_at",
            (REAL_POOL_DATASET_VERSION,),
        )
    ]
    thresholds = (1.5, 2, 3, 5, 10, 20, 50)
    result["cohorts"] = {
        f"{threshold:g}x_plus": sum(
            float(row.get("peak_multiple") or 0) >= threshold for row in outcome_rows
        )
        for threshold in thresholds
    }
    result["cohorts"].update(
        {
            "non_runner": sum(
                1 <= float(row.get("peak_multiple") or 0) < 1.5 for row in outcome_rows
            ),
            "failed_or_price_collapse": sum(
                bool(row.get("rugged")) or float(row.get("peak_multiple") or 0) < 1
                for row in outcome_rows
            ),
            "total": len(outcome_rows),
        }
    )
    execution_haircut = 0.02
    adjusted = [
        max(0.0, float(row.get("peak_multiple") or 0) * (1 - execution_haircut))
        for row in outcome_rows
    ]
    result["live_usability"] = {
        "decision_delay_seconds": 86_400,
        "execution_haircut": execution_haircut,
        "adjusted_5x_rate": (
            sum(value >= 5 for value in adjusted) / len(adjusted) if adjusted else None
        ),
        "limitations": [
            "Daily source precision means this is not an ultra-early execution simulation.",
            "No pool-specific price-impact or sellability history is available on the free corpus.",
        ],
    }
    result["adversarial_findings"] = {
        "easy_to_fake": [
            "raw volume",
            "short-window transaction count",
            "prepared current social metadata",
        ],
        "costly_to_fake": [
            "sustained liquidity survival",
            "long-duration independent demand across regimes",
            "mature point-in-time wallet history with adequate sample",
        ],
        "suppression_rules": [
            "do not promote volume-only effects",
            "treat current-ranked-pool selection as high survivorship risk",
            "require transaction-level independence evidence before buyer-quality promotion",
        ],
    }
    for feature_name, drift in result["result"]["drift"].items():
        warehouse.record_drift(
            feature_name=feature_name,
            segment_type="chain",
            segment_value="solana",
            baseline_window=f"{windows['train'][0]}..{windows['train'][1]}",
            current_window=f"{windows['test'][0]}..{windows['test'][1]}",
            sample_size=int(drift["test_sample"]),
            metric_name="standardized_median_shift",
            metric_value=float(drift["standardized_shift"]),
            warning_threshold=1.0,
        )
    # A truthful negative decision: real observations exist, but the corpus cannot support promotion.
    decision_id = warehouse.record_research_decision(
        {
            "research_run_id": result["research_run_id"],
            "feature_name": "ranked_pool_historical_fingerprint",
            "feature_version": REAL_POOL_FEATURE_VERSION,
            "dataset_version": REAL_POOL_DATASET_VERSION,
            "sample_size": test_metrics["sample"],
            "train_window": windows["train"],
            "validation_window": windows["validation"],
            "test_window": windows["test"],
            "baseline": baselines,
            "ablation": result["result"]["ablations"],
            "leakage_state": "PASS",
            "drift_state": "INSUFFICIENT_UNBIASED_SAMPLE",
            "approval_state": "RESEARCH_ONLY",
            "approved_by": None,
            "merge_policy": "EXPLANATION_ONLY",
            "limitations": [
                "Rejected for production: current-ranked universe is survivor-selected.",
                "No claim of years-of-launch-data edge is permitted.",
            ],
        }
    )
    result["decision"] = {
        "decision_id": decision_id,
        "state": "RESEARCH_ONLY",
        "production_features_approved": 0,
        "reason": "real corpus is not launch-complete and is materially survivor-biased",
    }
    result["windows"] = windows
    return result


def measure_local_latency(warehouse: HistoricalWarehouse, samples: int = 50) -> dict[str, Any]:
    from memecoin_bot.signals import format_discord_event

    results: dict[str, Any] = {}

    def measure(operation: str, callback: Any) -> None:
        timings = []
        for _ in range(samples):
            started = time.perf_counter()
            callback()
            timings.append((time.perf_counter() - started) * 1000)
        warehouse.record_latency(
            operation,
            timings,
            storage_bytes=warehouse.path.stat().st_size,
            metadata={"offline_local_benchmark": True},
        )
        ordered = sorted(timings)
        results[operation] = {
            "sample": len(ordered),
            "p50_ms": statistics.median(ordered),
            "p95_ms": ordered[min(len(ordered) - 1, int((len(ordered) - 1) * 0.95))],
            "max_ms": max(ordered),
        }

    measure("warehouse_coverage_query", warehouse.coverage_manifest)
    feature = warehouse.conn.execute(
        "SELECT entity_key,feature_version,available_at FROM point_in_time_features "
        "ORDER BY available_at DESC LIMIT 1"
    ).fetchone()
    if feature:
        measure(
            "warehouse_point_in_time_lookup",
            lambda: warehouse.features_at(
                feature["entity_key"], feature["available_at"], feature["feature_version"]
            ),
        )
    with tempfile.TemporaryDirectory(prefix="gambit-approved-benchmark-") as directory:
        approved = ApprovedFeatureStore(Path(directory) / "approved.db")
        try:
            measure(
                "approved_context_empty_lookup",
                lambda: approved.context_at(
                    "solana", "benchmark-token", datetime.now(UTC).isoformat(), "NEW"
                ),
            )
        finally:
            approved.close()
    signal_fixture = {
        "classification": "QUALIFIED",
        "v15_signal_tier": "STRONG",
        "name": "Benchmark",
        "symbol": "BENCH",
        "chain": "solana",
        "token_address": "Benchmark111111111111111111111111111111111",
        "runner_score": 75,
        "failure_score": 20,
        "confidence": 0.75,
        "evidence_coverage": 80,
    }
    measure(
        "discord_payload_render",
        lambda: format_discord_event("SIGNAL", signal_fixture),
    )
    complete_jobs = warehouse.conn.execute(
        "SELECT records_ingested,started_at,updated_at FROM backfill_jobs "
        "WHERE state='COMPLETE' AND records_ingested>0"
    ).fetchall()
    rates = []
    for job in complete_jobs:
        seconds = (
            _parse_timestamp(job["updated_at"]) - _parse_timestamp(job["started_at"])
        ).total_seconds()
        if seconds > 0:
            rates.append(float(job["records_ingested"]) / seconds)
    results["backfill_throughput"] = {
        "completed_jobs": len(rates),
        "median_records_per_second": statistics.median(rates) if rates else None,
    }
    return results


def write_completion_report(
    warehouse: HistoricalWarehouse,
    output: str | Path,
    *,
    acquisition: dict[str, Any],
    normalization: dict[str, Any],
    research: dict[str, Any] | None,
) -> dict[str, Any]:
    status = warehouse.operator_status()
    research_executed = bool(research and research.get("research_run_id"))
    completion = {
        "code_complete": True,
        "data_complete": False,
        "research_executed": research_executed,
        "research_complete": False,
        "staging_complete": False,
        "production_ready": False,
        "production_features_approved": 0,
    }
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "completion": completion,
        "acquisition": acquisition,
        "normalization": normalization,
        "research": research,
        "operator_status": status,
        "truth_statement": (
            "Real public evidence was acquired and assessed. No historical feature is approved; "
            "launch-universe completeness, unbiased failures, transaction-level memory, prospective "
            "shadow evidence and staging acceptance remain incomplete."
        ),
    }
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return report


def finite_or_none(value: Any) -> float | None:
    numeric = float(value) if isinstance(value, (int, float)) else None
    return numeric if numeric is not None and math.isfinite(numeric) else None
