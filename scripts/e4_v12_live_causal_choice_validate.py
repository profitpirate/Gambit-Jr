#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import time
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping, Sequence

from memecoin_bot import e4_causal_entry_v12 as causal


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


def tx_index(row: Mapping[str, Any]) -> int:
    raw = row.get("raw") if isinstance(row.get("raw"), Mapping) else {}
    for key in ("transaction_index", "transactionIndex", "tx_index", "txIndex"):
        if row.get(key) is not None:
            return integer(row.get(key), -1)
        if raw.get(key) is not None:
            return integer(raw.get(key), -1)
    return -1


def event_key(row: Mapping[str, Any]) -> tuple[int, int, int, int]:
    return (
        integer(row.get("slot"), -1),
        tx_index(row) if tx_index(row) >= 0 else 1_000_000,
        integer(row.get("event_index"), 0),
        integer(row.get("received_ns"), 0),
    )


def event_from_row(row: Mapping[str, Any]) -> SimpleNamespace:
    raw = row.get("raw") if isinstance(row.get("raw"), Mapping) else {}
    return SimpleNamespace(
        mint=str(row.get("mint") or ""),
        kind=str(row.get("kind") or ""),
        received_ns=integer(row.get("received_ns")),
        source_ns=integer(row.get("source_ns"), integer(row.get("received_ns"))),
        trader=str(row.get("trader") or ""),
        creator=str(row.get("creator") or raw.get("creator") or ""),
        sol_amount=finite(row.get("sol_amount")),
        token_amount=finite(row.get("token_amount")),
        fdv_usd=finite(row.get("fdv_usd")),
        price_sol=finite(row.get("price_sol")),
        signature=str(row.get("signature") or ""),
        slot=integer(row.get("slot")),
        raw=raw,
    )


def load_events(paths: Sequence[Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in paths:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                value = json.loads(line)
                if value.get("mint") and integer(value.get("received_ns")) > 0:
                    rows.append(value)
    rows.sort(key=event_key)
    return rows


def load_failed(paths: Sequence[Path]) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}

    def walk(value: Any) -> None:
        if isinstance(value, Mapping):
            failed = value.get("failed_attempts")
            rows = failed.get("rows") if isinstance(failed, Mapping) else None
            if isinstance(rows, list):
                for row in rows:
                    if not isinstance(row, Mapping) or not row.get("mapping_ok", True):
                        continue
                    mint = str(row.get("mapped_mint") or row.get("mint") or "")
                    if mint and row.get("captured_mint", True):
                        existing = output.get(mint)
                        if existing is None or (
                            integer(row.get("attempt_slot")),
                            integer(row.get("attempt_transaction_index"), 1_000_000),
                        ) < (
                            integer(existing.get("attempt_slot")),
                            integer(existing.get("attempt_transaction_index"), 1_000_000),
                        ):
                            output[mint] = dict(row)
            for item in value.values():
                if isinstance(item, (Mapping, list)):
                    walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    for root in paths:
        candidates = [root] if root.is_file() else list(root.rglob("*.json")) if root.exists() else []
        for path in candidates:
            try:
                walk(json.loads(path.read_text(encoding="utf-8")))
            except Exception:
                continue
    return output


def batch_positions(paths: Sequence[Path]) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    for path in paths:
        data = json.loads(path.read_text(encoding="utf-8"))
        for row in (data.get("actual_e4_fresh_sample") or {}).get("positions") or []:
            mint = str(row.get("mint") or "")
            if mint:
                output[mint] = dict(row)
    return output


def failed_timestamp(
    mint: str,
    attempt: Mapping[str, Any],
    events_by_mint: Mapping[str, Sequence[Mapping[str, Any]]],
    create_by_mint: Mapping[str, Mapping[str, Any]],
) -> int:
    slot = integer(attempt.get("attempt_slot"), -1)
    index = integer(attempt.get("attempt_transaction_index"), -1)
    same_slot = [
        row for row in events_by_mint.get(mint, ())
        if integer(row.get("slot"), -2) == slot
    ]
    if same_slot:
        ordered = sorted(same_slot, key=event_key)
        if index >= 0:
            later = [row for row in ordered if tx_index(row) >= index and tx_index(row) >= 0]
            if later:
                return integer(later[0].get("received_ns"))
        return integer(ordered[0].get("received_ns"))
    create = create_by_mint.get(mint) or {}
    create_ns = integer(create.get("received_ns"))
    create_slot = integer(create.get("slot"), slot)
    return create_ns + max(0, slot - create_slot) * 400_000_000


def wilson_lower(successes: int, total: int, z: float = 1.96) -> float:
    if total <= 0:
        return 0.0
    p = successes / total
    denominator = 1.0 + z * z / total
    centre = p + z * z / (2.0 * total)
    spread = z * math.sqrt((p * (1.0 - p) + z * z / (4.0 * total)) / total)
    return max(0.0, (centre - spread) / denominator)


def main() -> int:
    parser = argparse.ArgumentParser(description="Chronologically validate V12 causal choice model on fresh live E4 events")
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
    model = causal.ConditionalChoiceModel(model_payload)
    runtime = causal.CausalChoiceRuntime(model, state_payload)
    causal.ENABLED = True

    rows = load_events(args.events)
    events_by_mint: dict[str, list[dict[str, Any]]] = defaultdict(list)
    create_by_mint: dict[str, dict[str, Any]] = {}
    success_markers: dict[str, int] = {}
    for row in rows:
        mint = str(row.get("mint") or "")
        events_by_mint[mint].append(row)
        kind = str(row.get("kind") or "").upper()
        if kind == "CREATE":
            create_by_mint.setdefault(mint, row)
        if str(row.get("trader") or "") == causal.E4_WALLET and kind in {"BUY", "PUMPSWAP_BUY"}:
            success_markers.setdefault(mint, integer(row.get("received_ns")))

    failed = load_failed(args.attempts)
    failed_markers = {
        mint: failed_timestamp(mint, row, events_by_mint, create_by_mint)
        for mint, row in failed.items()
        if mint in events_by_mint
    }
    intent_markers = dict(failed_markers)
    for mint, timestamp in success_markers.items():
        if mint not in intent_markers or timestamp < intent_markers[mint]:
            intent_markers[mint] = timestamp

    predictions: dict[str, dict[str, Any]] = {}
    e4_seen: set[str] = set()
    for row in rows:
        mint = str(row.get("mint") or "")
        raw = row.get("raw") if isinstance(row.get("raw"), Mapping) else {}
        if str(row.get("kind") or "").upper() == "CREATE":
            context = v = causal.v6._CONTEXT_BY_MINT.setdefault(mint, {})
            for key, value in {
                "creator": row.get("creator") or raw.get("creator") or row.get("trader"),
                "uri": raw.get("uri"),
                "token_program": raw.get("token_program"),
                "is_mayhem_mode": raw.get("is_mayhem_mode"),
                "is_cashback_enabled": raw.get("is_cashback_enabled"),
            }.items():
                if value not in (None, ""):
                    context[key] = value
        event = event_from_row(row)
        runtime.observe_pre(event)
        kind = str(row.get("kind") or "").upper()
        is_e4 = str(row.get("trader") or "") == causal.E4_WALLET and kind in {"BUY", "PUMPSWAP_BUY"}
        if not is_e4:
            decision = runtime.decision(mint, integer(row.get("received_ns")))
            if decision is not None and mint not in predictions:
                predictions[mint] = {
                    "mint": mint,
                    "decision_ns": decision.observed_ns,
                    "utility": decision.utility,
                    "margin": decision.margin,
                    "rank": decision.rank,
                    "mode": decision.mode,
                }
        runtime.observe_post(event)
        if is_e4:
            e4_seen.add(mint)

    predicted = set(predictions)
    intents = set(intent_markers)
    true = predicted & intents
    false = predicted - intents
    missed = intents - predicted
    pre_intent = {
        mint for mint in true
        if integer(predictions[mint]["decision_ns"]) < integer(intent_markers[mint])
    }
    successful = set(success_markers)
    failed_only = set(failed_markers) - successful
    positions = batch_positions(args.batch)
    predicted_success_positions = [positions[mint] for mint in predicted & successful if mint in positions]
    wins = sum(finite(row.get("pnl_sol") or row.get("gross_pnl_sol")) > 0 for row in predicted_success_positions)

    metrics = {
        "captured_launches": len(create_by_mint),
        "successful_e4_intents": len(successful),
        "mapped_failed_e4_intents": len(failed_only),
        "total_e4_intents": len(intents),
        "predictions": len(predicted),
        "true": len(true),
        "false_positives": len(false),
        "precision": len(true) / len(predicted) if predicted else 0.0,
        "precision_wilson_low": wilson_lower(len(true), len(predicted)),
        "recall": len(true) / len(intents) if intents else 0.0,
        "success_recall": len(predicted & successful) / len(successful) if successful else 0.0,
        "failed_attempt_recall": len(predicted & failed_only) / len(failed_only) if failed_only else 0.0,
        "pre_intent_true": len(pre_intent),
        "all_true_pre_intent": len(pre_intent) == len(true),
        "predicted_success_outcomes": len(predicted_success_positions),
        "predicted_success_wins": wins,
        "predicted_success_e4_win_rate": wins / len(predicted_success_positions) if predicted_success_positions else None,
        "predicted_success_e4_net_pnl_sol": sum(finite(row.get("pnl_sol") or row.get("gross_pnl_sol")) for row in predicted_success_positions),
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
    status = "FRESH_LIVE_CONFIRMED" if confirmed else "INSUFFICIENT_LIVE_SAMPLE" if not sufficient else "FRESH_LIVE_REJECTED"
    result = {
        "version": "e4-v12-live-causal-choice-validation-v1",
        "generated_at_unix": int(time.time()),
        "status": status,
        "model_version": model_payload.get("version"),
        "model_status": model_payload.get("status"),
        "state_source_runs": state_payload.get("source_runs"),
        "causality": {
            "decision": "recorded only after non-E4 events",
            "successful_marker": "first observed E4 buy received_ns",
            "failed_marker": "mapped failed E4 transaction slot/index aligned to captured events",
            "pass_rule": "prediction must precede the first E4 intent marker",
        },
        "metrics": metrics,
        "predictions": [
            {
                **row,
                "label": "SUCCESS" if mint in successful else "FAILED_ATTEMPT" if mint in failed_only else "FALSE_POSITIVE",
                "intent_ns": intent_markers.get(mint),
                "lead_ms": (integer(intent_markers[mint]) - integer(row["decision_ns"])) / 1e6 if mint in intent_markers else None,
                "e4_pnl_sol": finite((positions.get(mint) or {}).get("pnl_sol") or (positions.get(mint) or {}).get("gross_pnl_sol")) if mint in positions else None,
            }
            for mint, row in sorted(predictions.items(), key=lambda item: integer(item[1]["decision_ns"]))
        ],
        "missed_intent_mints": sorted(missed),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({"status": status, "metrics": metrics}, indent=2, sort_keys=True), flush=True)
    return 0 if confirmed else 6


if __name__ == "__main__":
    raise SystemExit(main())
