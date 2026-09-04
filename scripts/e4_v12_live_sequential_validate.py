#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping

from memecoin_bot import e4_sequential_hazard_v12 as sequential

import e4_v12_live_causal_choice_validate as common


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Chronologically validate frozen V12 sequential hazard against live E4 intentions"
    )
    parser.add_argument("--events", action="append", default=[], type=Path)
    parser.add_argument("--batch", action="append", default=[], type=Path)
    parser.add_argument("--attempts", action="append", default=[], type=Path)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--minimum-intents", type=int, default=5)
    args = parser.parse_args()
    if not args.events or not args.batch:
        parser.error("at least one --events and --batch file is required")

    model_payload = json.loads(args.model.read_text(encoding="utf-8"))
    state_payload = json.loads(args.state.read_text(encoding="utf-8"))
    model = sequential.SequentialHazardModel(model_payload)
    runtime = sequential.SequentialHazardRuntime(model, state_payload)
    sequential.ENABLED = True

    rows = common.load_events(args.events)
    events_by_mint: dict[str, list[dict[str, Any]]] = defaultdict(list)
    create_by_mint: dict[str, dict[str, Any]] = {}
    success_markers: dict[str, int] = {}
    for row in rows:
        mint = str(row.get("mint") or "")
        events_by_mint[mint].append(row)
        kind = str(row.get("kind") or "").upper()
        if kind == "CREATE":
            create_by_mint.setdefault(mint, row)
        if (
            str(row.get("trader") or "") == sequential.E4_WALLET
            and kind in {"BUY", "PUMPSWAP_BUY"}
        ):
            success_markers.setdefault(mint, common.integer(row.get("received_ns")))

    failed = common.load_failed(args.attempts)
    failed_markers = {
        mint: common.failed_timestamp(
            mint,
            row,
            events_by_mint,
            create_by_mint,
        )
        for mint, row in failed.items()
        if mint in events_by_mint
    }
    intent_markers = dict(failed_markers)
    for mint, timestamp in success_markers.items():
        if mint not in intent_markers or timestamp < intent_markers[mint]:
            intent_markers[mint] = timestamp

    predictions: dict[str, dict[str, Any]] = {}
    for row in rows:
        mint = str(row.get("mint") or "")
        raw = row.get("raw") if isinstance(row.get("raw"), Mapping) else {}
        if str(row.get("kind") or "").upper() == "CREATE":
            context = sequential.v6._CONTEXT_BY_MINT.setdefault(mint, {})
            for key, value in {
                "creator": row.get("creator") or raw.get("creator") or row.get("trader"),
                "token_program": raw.get("token_program"),
                "is_mayhem_mode": raw.get("is_mayhem_mode"),
            }.items():
                if value not in (None, ""):
                    context[key] = value
        event = common.event_from_row(row)
        runtime.observe_pre(event)
        kind = str(row.get("kind") or "").upper()
        is_e4 = (
            str(row.get("trader") or "") == sequential.E4_WALLET
            and kind in {"BUY", "PUMPSWAP_BUY"}
        )
        if not is_e4:
            decision = runtime.decision(
                mint,
                common.integer(row.get("received_ns")),
            )
            if decision is not None and mint not in predictions:
                predictions[mint] = {
                    "mint": mint,
                    "decision_ns": decision.observed_ns,
                    "probability": decision.probability,
                    "margin": decision.margin,
                }
        runtime.observe_post(event)

    predicted = set(predictions)
    intents = set(intent_markers)
    true = predicted & intents
    false = predicted - intents
    missed = intents - predicted
    pre_intent = {
        mint
        for mint in true
        if common.integer(predictions[mint]["decision_ns"])
        < common.integer(intent_markers[mint])
        and (
            common.integer(intent_markers[mint])
            - common.integer(predictions[mint]["decision_ns"])
        )
        / 1e6
        <= model.horizon_ms
    }
    successful = set(success_markers)
    failed_only = set(failed_markers) - successful
    positions = common.batch_positions(args.batch)
    predicted_success_positions = [
        positions[mint]
        for mint in predicted & successful
        if mint in positions
    ]
    wins = sum(
        common.finite(row.get("pnl_sol") or row.get("gross_pnl_sol")) > 0
        for row in predicted_success_positions
    )

    metrics = {
        "captured_launches": len(create_by_mint),
        "successful_e4_intents": len(successful),
        "mapped_failed_e4_intents": len(failed_only),
        "total_e4_intents": len(intents),
        "predictions": len(predicted),
        "true": len(pre_intent),
        "late_or_too_early_intent_matches": len(true - pre_intent),
        "false_positives": len(false) + len(true - pre_intent),
        "precision": len(pre_intent) / len(predicted) if predicted else 0.0,
        "precision_wilson_low": common.wilson_lower(
            len(pre_intent), len(predicted)
        ),
        "recall": len(pre_intent) / len(intents) if intents else 0.0,
        "success_recall": (
            len(pre_intent & successful) / len(successful)
            if successful
            else 0.0
        ),
        "failed_attempt_recall": (
            len(pre_intent & failed_only) / len(failed_only)
            if failed_only
            else 0.0
        ),
        "all_true_pre_intent": len(pre_intent) == len(true),
        "predicted_success_outcomes": len(predicted_success_positions),
        "predicted_success_wins": wins,
        "predicted_success_e4_win_rate": (
            wins / len(predicted_success_positions)
            if predicted_success_positions
            else None
        ),
        "predicted_success_e4_net_pnl_sol": sum(
            common.finite(row.get("pnl_sol") or row.get("gross_pnl_sol"))
            for row in predicted_success_positions
        ),
    }
    sufficient = len(intents) >= args.minimum_intents
    confirmed = bool(
        sufficient
        and metrics["true"] >= 2
        and metrics["precision"] >= 0.50
        and metrics["recall"] >= 0.10
        and metrics["all_true_pre_intent"]
        and metrics["false_positives"] <= metrics["true"]
    )
    status = (
        "FRESH_LIVE_CONFIRMED"
        if confirmed
        else "INSUFFICIENT_LIVE_SAMPLE"
        if not sufficient
        else "FRESH_LIVE_REJECTED"
    )
    result = {
        "version": "e4-v12-live-sequential-hazard-validation-v1",
        "generated_at_unix": int(time.time()),
        "status": status,
        "model_version": model_payload.get("version"),
        "model_status": model_payload.get("status"),
        "horizon_ms": model.horizon_ms,
        "state_source_runs": state_payload.get("source_runs"),
        "causality": {
            "decision": "recorded only after a non-E4 CREATE/BUY event",
            "successful_marker": "first observed E4 buy",
            "failed_marker": "mapped failed E4 transaction slot/index",
            "pass_rule": "prediction must precede intent by no more than the frozen hazard horizon",
        },
        "metrics": metrics,
        "predictions": [
            {
                **row,
                "label": (
                    "SUCCESS"
                    if mint in successful
                    else "FAILED_ATTEMPT"
                    if mint in failed_only
                    else "FALSE_POSITIVE"
                ),
                "intent_ns": intent_markers.get(mint),
                "lead_ms": (
                    common.integer(intent_markers[mint])
                    - common.integer(row["decision_ns"])
                )
                / 1e6
                if mint in intent_markers
                else None,
                "within_frozen_horizon": mint in pre_intent,
                "e4_pnl_sol": (
                    common.finite(
                        (positions.get(mint) or {}).get("pnl_sol")
                        or (positions.get(mint) or {}).get("gross_pnl_sol")
                    )
                    if mint in positions
                    else None
                ),
            }
            for mint, row in sorted(
                predictions.items(),
                key=lambda item: common.integer(item[1]["decision_ns"]),
            )
        ],
        "missed_intent_mints": sorted(missed),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(json.dumps({"status": status, "metrics": metrics}, indent=2, sort_keys=True))
    return 0 if confirmed else 8


if __name__ == "__main__":
    raise SystemExit(main())
