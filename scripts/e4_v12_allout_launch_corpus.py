#!/usr/bin/env python3
"""Build a compact causal launch-level corpus from all pinned V12 captures.

The canonical V2 risk-set dataset contains the authoritative E4 selection labels.
This script joins those labels to every captured launch, reconstructs early causal
features at fixed millisecond cutoffs, records independent market trajectories and
attaches observed E4 outcomes where a landed position is available.

It never modifies production code and never sends a transaction.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import statistics
from collections import Counter, defaultdict, deque
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlparse

import pandas as pd

E4_WALLET = "E4EzXdwf7NNdqM2XGswWaWHfxgucVCo24PTCcrimTKBz"
BUY_KINDS = {"BUY", "PUMPSWAP_BUY"}
SELL_KINDS = {"SELL", "PUMPSWAP_SELL"}
EARLY_WINDOWS_MS = (0, 1, 2, 5, 10, 20, 50, 100, 250, 500, 1_000)
OUTCOME_WINDOWS_MS = (1_000, 2_000, 3_000, 5_000, 10_000, 15_000, 30_000, 60_000, 120_000, 300_000)
TP_LEVELS = (1.10, 1.20, 1.30, 1.50, 2.00, 3.00)
SL_LEVELS = (0.90, 0.80, 0.70, 0.50)
FEE_BPS = 125
ENTRY_BUDGET_SOL = 0.0555
PRIORITY_AND_TIP_SOL = 0.00015


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


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def normal_sol(value: Any) -> float:
    amount = finite(value)
    return amount / 1_000_000_000.0 if amount >= 1_000_000.0 else amount


def normal_tokens(value: Any) -> float:
    amount = finite(value)
    return amount / 1_000_000.0 if amount >= 10_000_000_000.0 else amount


def uri_host(value: str) -> str:
    try:
        return (urlparse(value).hostname or "").lower()
    except ValueError:
        return ""


def text_features(prefix: str, value: str) -> dict[str, Any]:
    text = str(value or "")
    alpha = [character for character in text if character.isalpha()]
    digits = [character for character in text if character.isdigit()]
    upper = [character for character in alpha if character.isupper()]
    return {
        f"{prefix}_length": len(text),
        f"{prefix}_word_count": len(text.split()),
        f"{prefix}_digit_fraction": len(digits) / max(1, len(text)),
        f"{prefix}_uppercase_fraction": len(upper) / max(1, len(alpha)),
        f"{prefix}_has_url": "http://" in text.lower() or "https://" in text.lower(),
        f"{prefix}_has_non_ascii": any(ord(character) > 127 for character in text),
    }


def load_v2_labels(path: Path) -> dict[str, dict[str, Any]]:
    labels: dict[str, dict[str, Any]] = {}
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            if not row.get("selected_by_e4"):
                continue
            mint = str(row.get("candidate_mint") or "")
            if not mint:
                continue
            candidate = {
                "selection_label": str(row.get("selection_label") or ""),
                "decision_ns": integer(row.get("decision_ns")),
                "decision_ns_lower_bound": integer(row.get("decision_ns_lower_bound")),
                "decision_ns_upper_bound": row.get("decision_ns_upper_bound"),
                "decision_time_is_lower_bound": bool(row.get("decision_time_is_lower_bound")),
                "source_run_id": str(row.get("source_run_id") or ""),
                "observed_source_sol": row.get("observed_source_sol"),
                "observed_source_tokens": row.get("observed_source_tokens"),
                "source_priority_fee": row.get("source_priority_fee"),
                "source_tip": row.get("source_tip"),
                "source_output_floor": row.get("source_output_floor"),
                "estimated_curve_deterioration_bps": row.get("estimated_curve_deterioration_bps"),
                "failure_program": row.get("failure_program"),
                "failure_code": row.get("failure_code"),
                "failure_message": row.get("failure_message"),
            }
            previous = labels.get(mint)
            if previous and previous != candidate:
                raise ValueError(f"conflicting V2 selected label for {mint}")
            labels[mint] = candidate
    return labels


def state_from_event(row: Mapping[str, Any]) -> dict[str, Any] | None:
    raw = row.get("raw") if isinstance(row.get("raw"), Mapping) else {}
    virtual_sol = normal_sol(raw.get("virtual_sol_reserves"))
    virtual_tokens = normal_tokens(raw.get("virtual_token_reserves"))
    real_tokens = normal_tokens(raw.get("real_token_reserves"))
    if virtual_sol <= 0 or virtual_tokens <= 0:
        return None
    return {
        "ns": integer(row.get("received_ns")),
        "slot": integer(row.get("slot"), -1),
        "vsol": virtual_sol,
        "vtok": virtual_tokens,
        "rtok": real_tokens if real_tokens > 0 else float("inf"),
        "price": finite(row.get("price_sol")),
        "fdv": finite(row.get("fdv_usd")),
        "complete": bool(row.get("complete")),
    }


def quote_buy(total_budget_sol: float, state: Mapping[str, Any]) -> tuple[float, float]:
    budget = max(0.0, total_budget_sol - PRIORITY_AND_TIP_SOL)
    curve_input = budget / (1.0 + FEE_BPS / 10_000.0)
    virtual_sol = finite(state.get("vsol"))
    virtual_tokens = finite(state.get("vtok"))
    real_tokens = finite(state.get("rtok"), float("inf"))
    if curve_input <= 0 or virtual_sol <= 0 or virtual_tokens <= 0:
        return 0.0, 0.0
    tokens = curve_input * virtual_tokens / (virtual_sol + curve_input)
    tokens = min(tokens, real_tokens)
    return max(0.0, tokens), curve_input


def quote_sell(tokens: float, state: Mapping[str, Any]) -> float:
    virtual_sol = finite(state.get("vsol"))
    virtual_tokens = finite(state.get("vtok"))
    if tokens <= 0 or virtual_sol <= 0 or virtual_tokens <= 0:
        return 0.0
    gross = tokens * virtual_sol / (virtual_tokens + tokens)
    return max(0.0, gross * (1.0 - FEE_BPS / 10_000.0) - PRIORITY_AND_TIP_SOL)


def latest_state(states: Sequence[Mapping[str, Any]], target_ns: int) -> Mapping[str, Any] | None:
    result = None
    for state in states:
        if integer(state.get("ns")) > target_ns:
            break
        result = state
    return result or (states[0] if states else None)


def pnl_multiple(tokens: float, state: Mapping[str, Any]) -> tuple[float, float]:
    proceeds = quote_sell(tokens, state)
    pnl = proceeds - ENTRY_BUDGET_SOL
    return pnl, proceeds / ENTRY_BUDGET_SOL if ENTRY_BUDGET_SOL > 0 else 0.0


def first_crossing(
    states: Sequence[Mapping[str, Any]],
    entry_ns: int,
    tokens: float,
    take_profit: float,
    stop_loss: float,
    maximum_ns: int,
) -> tuple[float, float, int, str]:
    last = None
    for state in states:
        timestamp = integer(state.get("ns"))
        if timestamp < entry_ns:
            continue
        if timestamp > maximum_ns:
            break
        last = state
        pnl, multiple = pnl_multiple(tokens, state)
        if multiple >= take_profit:
            return pnl, multiple, timestamp, "TP"
        if multiple <= stop_loss:
            return pnl, multiple, timestamp, "SL"
    state = last or latest_state(states, maximum_ns)
    if state is None:
        return -ENTRY_BUDGET_SOL, 0.0, maximum_ns, "NO_STATE"
    pnl, multiple = pnl_multiple(tokens, state)
    return pnl, multiple, min(maximum_ns, integer(state.get("ns"))), "TIME"


def parse_run(events_path: Path, batch_path: Path, run_id: str, split: str, labels: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    batch = json.loads(batch_path.read_text(encoding="utf-8"))
    launches: dict[str, dict[str, Any]] = {}
    events: dict[str, list[dict[str, Any]]] = defaultdict(list)
    with events_path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{events_path}:{line_number}: {exc}") from exc
            mint = str(row.get("mint") or "")
            if not mint:
                continue
            kind = str(row.get("kind") or "").upper()
            timestamp = integer(row.get("received_ns"))
            raw = row.get("raw") if isinstance(row.get("raw"), Mapping) else {}
            event = {
                "ns": timestamp,
                "slot": integer(row.get("slot"), -1),
                "event_index": integer(row.get("event_index")),
                "kind": kind,
                "trader": str(row.get("trader") or ""),
                "signature": str(row.get("signature") or ""),
                "sol": max(0.0, finite(row.get("sol_amount"))),
                "tokens": max(0.0, finite(row.get("token_amount"))),
                "price": max(0.0, finite(row.get("price_sol"))),
                "fdv": max(0.0, finite(row.get("fdv_usd"))),
                "state": state_from_event(row),
                "complete": bool(row.get("complete")),
            }
            events[mint].append(event)
            if kind == "CREATE":
                if mint in launches:
                    raise ValueError(f"duplicate CREATE for {mint} in {run_id}")
                launches[mint] = {
                    "mint": mint,
                    "source_run_id": run_id,
                    "split": split,
                    "create_ns": timestamp,
                    "create_slot": integer(row.get("slot"), -1),
                    "create_event_index": integer(row.get("event_index")),
                    "create_signature": str(row.get("signature") or ""),
                    "creator": str(row.get("creator") or row.get("trader") or raw.get("creator") or raw.get("user") or ""),
                    "name": str(raw.get("name") or ""),
                    "symbol": str(raw.get("symbol") or ""),
                    "metadata_uri": str(raw.get("uri") or ""),
                    "metadata_uri_host": uri_host(str(raw.get("uri") or "")),
                    "token_program": str(raw.get("token_program") or ""),
                    "mayhem_mode": bool(raw.get("is_mayhem_mode", False)),
                    "cashback_enabled": bool(raw.get("is_cashback_enabled", False)),
                    "initial_virtual_sol": normal_sol(raw.get("virtual_sol_reserves")),
                    "initial_virtual_tokens": normal_tokens(raw.get("virtual_token_reserves")),
                    "initial_real_tokens": normal_tokens(raw.get("real_token_reserves")),
                    "create_price_sol": max(0.0, finite(row.get("price_sol"))),
                    "create_fdv_usd": max(0.0, finite(row.get("fdv_usd"))),
                }

    positions = {
        str(row.get("mint") or ""): dict(row)
        for row in (batch.get("actual_e4_fresh_sample") or {}).get("positions") or []
        if row.get("mint")
    }
    rows: list[dict[str, Any]] = []
    for mint, launch in launches.items():
        trajectory = sorted(events[mint], key=lambda row: (row["ns"], row["slot"], row["event_index"], row["signature"]))
        states = [row["state"] for row in trajectory if row.get("state")]
        if not states:
            continue
        creator = launch["creator"]
        label_data = dict(labels.get(mint) or {})
        selection_label = str(label_data.get("selection_label") or "TRUE_IGNORE")
        selected = selection_label != "TRUE_IGNORE"
        position = positions.get(mint) or {}
        row: dict[str, Any] = {
            **launch,
            **text_features("name", launch["name"]),
            **text_features("symbol", launch["symbol"]),
            "selection_label": selection_label,
            "selected_by_e4": selected,
            "landed_successfully": selection_label == "SELECTED_LANDED",
            "failed_fill_selection": selection_label == "SELECTED_FILL_REJECTED",
            "decision_ns": label_data.get("decision_ns"),
            "decision_time_is_lower_bound": bool(label_data.get("decision_time_is_lower_bound")),
            "decision_delay_ms": (
                (integer(label_data.get("decision_ns")) - launch["create_ns"]) / 1_000_000.0
                if selected and integer(label_data.get("decision_ns")) > 0
                else None
            ),
            "observed_source_sol": label_data.get("observed_source_sol"),
            "observed_source_tokens": label_data.get("observed_source_tokens"),
            "source_priority_fee": label_data.get("source_priority_fee"),
            "source_tip": label_data.get("source_tip"),
            "source_output_floor": label_data.get("source_output_floor"),
            "estimated_curve_deterioration_bps": label_data.get("estimated_curve_deterioration_bps"),
            "failure_program": label_data.get("failure_program"),
            "failure_code": label_data.get("failure_code"),
            "failure_message": label_data.get("failure_message"),
            "e4_cost_sol": position.get("cost_sol"),
            "e4_proceeds_sol": position.get("proceeds_sol"),
            "e4_pnl_sol": position.get("pnl_sol"),
            "e4_win": finite(position.get("pnl_sol")) > 0 if position else None,
            "e4_hold_ms": position.get("hold_ms"),
            "e4_sell_count": position.get("sell_count"),
            "e4_buy_count": position.get("buy_count"),
            "e4_first_partial_fraction": position.get("first_partial_fraction"),
            "e4_entry_time_s": position.get("entry_time"),
            "e4_exit_time_s": position.get("exit_time"),
            "event_count": len(trajectory),
            "captured_duration_ms": max(0.0, (trajectory[-1]["ns"] - launch["create_ns"]) / 1_000_000.0),
            "ever_completed": any(event["complete"] for event in trajectory),
        }
        e4_buys = [event for event in trajectory if event["kind"] in BUY_KINDS and event["trader"] == E4_WALLET]
        row["observed_e4_buy_event_count"] = len(e4_buys)
        row["observed_e4_first_buy_ns"] = e4_buys[0]["ns"] if e4_buys else None
        row["observed_e4_first_buy_delay_ms"] = (
            (e4_buys[0]["ns"] - launch["create_ns"]) / 1_000_000.0 if e4_buys else None
        )

        for window_ms in EARLY_WINDOWS_MS:
            cutoff = launch["create_ns"] + window_ms * 1_000_000
            visible = [event for event in trajectory if event["ns"] <= cutoff and event["kind"] != "CREATE" and event["trader"] != E4_WALLET]
            buys = [event for event in visible if event["kind"] in BUY_KINDS]
            sells = [event for event in visible if event["kind"] in SELL_KINDS]
            creator_buys = [event for event in buys if event["trader"] == creator]
            outside_buys = [event for event in buys if event["trader"] and event["trader"] != creator]
            outside_buyers = []
            for event in outside_buys:
                if event["trader"] not in outside_buyers:
                    outside_buyers.append(event["trader"])
            signatures = Counter(event["signature"] for event in buys if event["signature"])
            state = latest_state(states, cutoff)
            suffix = f"{window_ms}ms"
            row.update(
                {
                    f"buy_count_{suffix}": len(buys),
                    f"sell_count_{suffix}": len(sells),
                    f"creator_buy_count_{suffix}": len(creator_buys),
                    f"creator_buy_sol_{suffix}": sum(event["sol"] for event in creator_buys),
                    f"outside_buy_sol_{suffix}": sum(event["sol"] for event in outside_buys),
                    f"unique_outside_buyers_{suffix}": len(outside_buyers),
                    f"first_outside_buyers_{suffix}": outside_buyers[:12],
                    f"distinct_buy_signatures_{suffix}": len(signatures),
                    f"max_buys_per_signature_{suffix}": max(signatures.values(), default=0),
                    f"same_slot_buy_count_{suffix}": sum(event["slot"] == launch["create_slot"] for event in buys),
                    f"same_slot_outside_buyer_count_{suffix}": len({event["trader"] for event in outside_buys if event["slot"] == launch["create_slot"]}),
                    f"same_create_signature_buy_count_{suffix}": sum(event["signature"] == launch["create_signature"] for event in buys),
                    f"fdv_{suffix}": finite(state.get("fdv")) if state else launch["create_fdv_usd"],
                    f"price_{suffix}": finite(state.get("price")) if state else launch["create_price_sol"],
                    f"virtual_sol_{suffix}": finite(state.get("vsol")) if state else launch["initial_virtual_sol"],
                    f"virtual_tokens_{suffix}": finite(state.get("vtok")) if state else launch["initial_virtual_tokens"],
                    f"real_tokens_{suffix}": finite(state.get("rtok")) if state else launch["initial_real_tokens"],
                }
            )

        for window_ms in OUTCOME_WINDOWS_MS:
            cutoff = launch["create_ns"] + window_ms * 1_000_000
            visible_states = [state for state in states if integer(state.get("ns")) <= cutoff]
            effective = visible_states or [states[0]]
            fdv_values = [finite(state.get("fdv")) for state in effective if finite(state.get("fdv")) > 0]
            last_state = effective[-1]
            create_fdv = max(1e-12, launch["create_fdv_usd"])
            suffix = f"{window_ms}ms"
            row[f"max_fdv_{suffix}"] = max(fdv_values, default=launch["create_fdv_usd"])
            row[f"min_fdv_{suffix}"] = min(fdv_values, default=launch["create_fdv_usd"])
            row[f"last_fdv_{suffix}"] = finite(last_state.get("fdv"), launch["create_fdv_usd"])
            row[f"max_multiple_{suffix}"] = row[f"max_fdv_{suffix}"] / create_fdv
            row[f"min_multiple_{suffix}"] = row[f"min_fdv_{suffix}"] / create_fdv
            row[f"last_multiple_{suffix}"] = row[f"last_fdv_{suffix}"] / create_fdv

        for latency_ms in (0, 1, 2, 5, 10):
            entry_ns = launch["create_ns"] + latency_ms * 1_000_000
            entry_state = latest_state(states, entry_ns)
            tokens, curve_input = quote_buy(ENTRY_BUDGET_SOL, entry_state or {})
            prefix = f"paper_{latency_ms}ms"
            row[f"{prefix}_tokens"] = tokens
            row[f"{prefix}_curve_input_sol"] = curve_input
            row[f"{prefix}_entry_fdv"] = finite((entry_state or {}).get("fdv"), launch["create_fdv_usd"])
            for hold_ms in (1_000, 2_000, 3_000, 5_000, 10_000, 15_000, 30_000, 60_000):
                exit_state = latest_state(states, entry_ns + hold_ms * 1_000_000)
                pnl, multiple = pnl_multiple(tokens, exit_state or {})
                row[f"{prefix}_hold_{hold_ms}ms_pnl_sol"] = pnl
                row[f"{prefix}_hold_{hold_ms}ms_multiple"] = multiple
            maximum_ns = entry_ns + 60_000 * 1_000_000
            for take_profit in TP_LEVELS:
                for stop_loss in SL_LEVELS:
                    pnl, multiple, exit_ns, reason = first_crossing(
                        states,
                        entry_ns,
                        tokens,
                        take_profit,
                        stop_loss,
                        maximum_ns,
                    )
                    key = f"{prefix}_tp{int(take_profit * 100)}_sl{int(stop_loss * 100)}"
                    row[f"{key}_pnl_sol"] = pnl
                    row[f"{key}_multiple"] = multiple
                    row[f"{key}_exit_delay_ms"] = (exit_ns - launch["create_ns"]) / 1_000_000.0
                    row[f"{key}_reason"] = reason
        rows.append(row)
    if len(rows) != 3_000:
        raise ValueError(f"{run_id}: expected 3000 launch rows, got {len(rows)}")
    return rows


def add_causal_history(rows: list[dict[str, Any]]) -> None:
    rows.sort(key=lambda row: (integer(row["create_ns"]), str(row["mint"])))
    creator_launches: Counter[str] = Counter()
    creator_selections: Counter[str] = Counter()
    creator_landed: Counter[str] = Counter()
    creator_failed: Counter[str] = Counter()
    creator_wins: Counter[str] = Counter()
    creator_losses: Counter[str] = Counter()
    creator_pnl: Counter[str] = Counter()
    last_creator_launch: dict[str, int] = {}
    last_creator_selection: dict[str, int] = {}
    buyer_selections: Counter[str] = Counter()
    buyer_wins: Counter[str] = Counter()
    creator_buyer_pairs: Counter[tuple[str, str]] = Counter()
    uri_counts: Counter[str] = Counter()
    name_counts: Counter[str] = Counter()
    symbol_counts: Counter[str] = Counter()
    pending_outcomes: list[tuple[int, str, bool, float]] = []
    pending_index = 0

    for row in rows:
        now = integer(row["create_ns"])
        pending_outcomes.sort(key=lambda item: item[0])
        while pending_index < len(pending_outcomes) and pending_outcomes[pending_index][0] <= now:
            _, creator, won, pnl = pending_outcomes[pending_index]
            if won:
                creator_wins[creator] += 1
            else:
                creator_losses[creator] += 1
            creator_pnl[creator] += pnl
            pending_index += 1

        creator = str(row.get("creator") or "")
        row["creator_prior_launch_count"] = creator_launches[creator]
        row["creator_prior_selection_count"] = creator_selections[creator]
        row["creator_prior_landed_count"] = creator_landed[creator]
        row["creator_prior_failed_fill_count"] = creator_failed[creator]
        row["creator_prior_known_wins"] = creator_wins[creator]
        row["creator_prior_known_losses"] = creator_losses[creator]
        row["creator_prior_known_pnl_sol"] = creator_pnl[creator]
        row["creator_prior_known_win_rate"] = creator_wins[creator] / max(1, creator_wins[creator] + creator_losses[creator])
        row["time_since_creator_launch_ms"] = (
            (now - last_creator_launch[creator]) / 1_000_000.0 if creator in last_creator_launch else None
        )
        row["time_since_creator_selection_ms"] = (
            (now - last_creator_selection[creator]) / 1_000_000.0 if creator in last_creator_selection else None
        )
        uri = str(row.get("metadata_uri") or "")
        name = str(row.get("name") or "").strip().lower()
        symbol = str(row.get("symbol") or "").strip().lower()
        row["prior_exact_uri_count"] = uri_counts[uri] if uri else 0
        row["prior_exact_name_count"] = name_counts[name] if name else 0
        row["prior_exact_symbol_count"] = symbol_counts[symbol] if symbol else 0

        for window_ms in EARLY_WINDOWS_MS:
            buyers = row.get(f"first_outside_buyers_{window_ms}ms") or []
            row[f"known_e4_buyer_count_{window_ms}ms"] = sum(buyer_selections[buyer] > 0 for buyer in buyers)
            row[f"buyer_prior_selection_sum_{window_ms}ms"] = sum(buyer_selections[buyer] for buyer in buyers)
            row[f"buyer_prior_win_sum_{window_ms}ms"] = sum(buyer_wins[buyer] for buyer in buyers)
            row[f"creator_buyer_pair_max_{window_ms}ms"] = max(
                (creator_buyer_pairs[(creator, buyer)] for buyer in buyers), default=0
            )

        creator_launches[creator] += 1
        last_creator_launch[creator] = now
        if uri:
            uri_counts[uri] += 1
        if name:
            name_counts[name] += 1
        if symbol:
            symbol_counts[symbol] += 1

        if row.get("selected_by_e4"):
            creator_selections[creator] += 1
            last_creator_selection[creator] = integer(row.get("decision_ns"), now)
            if row.get("landed_successfully"):
                creator_landed[creator] += 1
            elif row.get("failed_fill_selection"):
                creator_failed[creator] += 1
            buyers = row.get("first_outside_buyers_10ms") or row.get("first_outside_buyers_100ms") or []
            for buyer in buyers:
                buyer_selections[buyer] += 1
                creator_buyer_pairs[(creator, buyer)] += 1
                if row.get("e4_win") is True:
                    buyer_wins[buyer] += 1
            if row.get("e4_pnl_sol") is not None:
                exit_ns = integer(row.get("e4_exit_time_s")) * 1_000_000_000
                pending_outcomes.append((max(now, exit_ns), creator, bool(row.get("e4_win")), finite(row.get("e4_pnl_sol"))))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--v2-jsonl", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    labels = load_v2_labels(args.v2_jsonl)
    rows: list[dict[str, Any]] = []
    source_audit = []
    for capture in manifest.get("captures") or []:
        run_id = str(capture["run_id"])
        split = str(capture["split"])
        root = args.source_root / run_id
        batches = list(root.rglob("e4-v12-forward-batch.json"))
        event_files = list(root.rglob("e4-v12-forward-batch-live-events.jsonl"))
        if len(batches) != 1 or len(event_files) != 1:
            raise ValueError(f"{run_id}: expected one batch and event file")
        batch_path, events_path = batches[0], event_files[0]
        run_rows = parse_run(events_path, batch_path, run_id, split, labels)
        rows.extend(run_rows)
        source_audit.append(
            {
                "run_id": run_id,
                "split": split,
                "batch_path": str(batch_path),
                "batch_bytes": batch_path.stat().st_size,
                "batch_sha256": sha256_path(batch_path),
                "events_path": str(events_path),
                "events_bytes": events_path.stat().st_size,
                "events_sha256": sha256_path(events_path),
                "launch_rows": len(run_rows),
                "selected_rows": sum(bool(row["selected_by_e4"]) for row in run_rows),
            }
        )
        print(json.dumps(source_audit[-1], sort_keys=True), flush=True)

    add_causal_history(rows)
    if len(rows) != 66_000:
        raise ValueError(f"expected 66,000 launches, got {len(rows)}")
    selected_mints = {row["mint"] for row in rows if row["selected_by_e4"]}
    missing_labels = sorted(set(labels) - selected_mints)
    if missing_labels:
        raise ValueError(f"selected labels absent from launch corpus: {missing_labels[:10]}")

    output = args.output_dir
    output.mkdir(parents=True, exist_ok=True)
    jsonl_gz = output / "e4-v12-allout-launch-corpus.jsonl.gz"
    with gzip.open(jsonl_gz, "wt", encoding="utf-8", compresslevel=6) as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
    frame = pd.DataFrame(rows)
    parquet = output / "e4-v12-allout-launch-corpus.parquet"
    frame.to_parquet(parquet, index=False, compression="zstd")

    summary = {
        "version": "e4-v12-allout-launch-corpus-v1",
        "rows": len(rows),
        "capture_runs": len(source_audit),
        "selected": int(frame["selected_by_e4"].sum()),
        "landed": int(frame["landed_successfully"].sum()),
        "failed_fill": int(frame["failed_fill_selection"].sum()),
        "true_ignores": int((~frame["selected_by_e4"]).sum()),
        "selected_with_e4_pnl": int(frame["e4_pnl_sol"].notna().sum()),
        "splits": frame.groupby("split").size().to_dict(),
        "selection_by_split": frame[frame["selected_by_e4"]].groupby("split").size().to_dict(),
        "outcomes": {
            "landed_wins": int((frame["e4_win"] == True).sum()),
            "landed_losses": int((frame["e4_win"] == False).sum()),
            "observed_e4_pnl_sol": float(frame["e4_pnl_sol"].fillna(0).sum()),
        },
        "files": {
            jsonl_gz.name: {"bytes": jsonl_gz.stat().st_size, "sha256": sha256_path(jsonl_gz)},
            parquet.name: {"bytes": parquet.stat().st_size, "sha256": sha256_path(parquet)},
        },
        "source_audit": source_audit,
        "causality": {
            "entry_features": "CREATE and fixed event cutoffs only",
            "creator_and_buyer_history": "strictly prior launch/selection/outcome state",
            "future_fields": "outcome columns are explicitly separated and must never enter entry models",
        },
    }
    write_json(output / "e4-v12-allout-launch-corpus-summary.json", summary)
    print(json.dumps({key: value for key, value in summary.items() if key != "source_audit"}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
