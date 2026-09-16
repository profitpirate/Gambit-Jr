#!/usr/bin/env python3
"""Recover one interrupted Golden live-paper cohort from immutable Solana history.

This is an infrastructure incident repair only. It never creates a new signal,
changes selection/sizing/costs, or broadcasts a transaction. The failed cohort
must have been entered prospectively while live. Missing tail market states are
read back from confirmed Solana transactions solely to finish the already-open
modeled positions and their post-exit observation windows.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import importlib.util
import json
import math
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Mapping

import aiohttp

from golden_horizon_engine import Config, Engine, valid

RPCS = ("https://api.mainnet-beta.solana.com", "https://solana-rpc.publicnode.com")
EXPECTED_ERROR_FRAGMENT = "maxSupportedTransactionVersion"


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")


def sha256_path(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class Rpc:
    def __init__(self) -> None:
        self.session: aiohttp.ClientSession | None = None
        self.seq = 0
        self.cursor = 0
        self.calls = 0

    async def __aenter__(self) -> "Rpc":
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self.session:
            await self.session.close()

    async def call(self, method: str, params: list[Any], attempts: int = 8) -> Any:
        assert self.session is not None
        last: Exception | None = None
        for attempt in range(attempts):
            url = RPCS[(self.cursor + attempt) % len(RPCS)]
            self.seq += 1
            self.calls += 1
            try:
                async with self.session.post(url, json={"jsonrpc": "2.0", "id": self.seq,
                                                        "method": method, "params": params}) as response:
                    text = await response.text()
                    if response.status == 429:
                        raise RuntimeError(f"HTTP 429 {url}")
                    if response.status >= 400:
                        raise RuntimeError(f"HTTP {response.status}: {text[:240]}")
                    payload = json.loads(text)
                    if payload.get("error"):
                        raise RuntimeError(str(payload["error"]))
                    self.cursor = (RPCS.index(url) + 1) % len(RPCS)
                    return payload.get("result")
            except Exception as exc:
                last = exc
                await asyncio.sleep(min(4.0, 0.35 * (attempt + 1)))
        raise RuntimeError(f"RPC {method} unavailable: {last}")


def state_from_item(item: Mapping[str, Any], ns: int, signature: str, slot: int) -> dict[str, Any] | None:
    quote = item.get("quote_mint")
    if quote not in (None, "", "11111111111111111111111111111111", "So11111111111111111111111111111111111111112"):
        return None
    kind = item.get("anchor_event")
    if kind in ("CompleteEvent", "CompletePumpAmmMigrationEvent"):
        return {"ns": ns, "signature": signature, "slot": slot, "complete": True, "vsol": 0.0, "vtok": 0.0}
    vsol = item.get("virtual_sol_reserves", item.get("virtual_quote_reserves"))
    vtok = item.get("virtual_token_reserves")
    if not isinstance(vsol, int) or not isinstance(vtok, int) or vsol <= 0 or vtok <= 0:
        return None
    rtok = item.get("real_token_reserves")
    return {"ns": ns, "signature": signature, "slot": slot, "complete": False,
            "vsol": vsol / 1e9, "vtok": vtok / 1e6,
            "rtok": rtok / 1e6 if isinstance(rtok, int) else None}


def linear_slot_clock(path: list[dict[str, Any]]) -> tuple[float, float, float]:
    points: dict[int, list[int]] = {}
    for row in path:
        if isinstance(row.get("slot"), int) and isinstance(row.get("ns"), int):
            points.setdefault(int(row["slot"]), []).append(int(row["ns"]))
    pairs = sorted((slot, statistics.median(values)) for slot, values in points.items())
    if not pairs:
        raise RuntimeError("no live slot/timestamp anchors in interrupted market path")
    if len(pairs) < 2:
        slot, ns = pairs[0]
        return 400_000_000.0, ns - 400_000_000.0 * slot, 0.0
    xs = [float(x) for x, _ in pairs]
    ys = [float(y) for _, y in pairs]
    xm = statistics.fmean(xs); ym = statistics.fmean(ys)
    var = sum((x - xm) ** 2 for x in xs)
    slope = sum((x - xm) * (y - ym) for x, y in zip(xs, ys)) / var if var else 400_000_000.0
    if not 150_000_000 <= slope <= 900_000_000:
        slope = 400_000_000.0
    intercept = ym - slope * xm
    residual = math.sqrt(statistics.fmean([(y - (intercept + slope * x)) ** 2 for x, y in zip(xs, ys)]))
    return slope, intercept, residual


async def signatures_for_window(rpc: Rpc, addresses: list[str], start_s: int, end_s: int) -> list[dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for address in dict.fromkeys(x for x in addresses if x):
        before: str | None = None
        for _ in range(12):
            cfg: dict[str, Any] = {"limit": 1000, "commitment": "confirmed"}
            if before:
                cfg["before"] = before
            rows = await rpc.call("getSignaturesForAddress", [address, cfg]) or []
            if not rows:
                break
            for index, row in enumerate(rows):
                bt = row.get("blockTime")
                sig = row.get("signature")
                if not sig or bt is None:
                    continue
                if start_s - 5 <= int(bt) <= end_s + 5:
                    item = dict(row)
                    item["retrieval_index"] = index
                    found[str(sig)] = item
            oldest = next((r.get("blockTime") for r in reversed(rows) if r.get("blockTime") is not None), None)
            if oldest is not None and int(oldest) < start_s - 5:
                break
            before = rows[-1].get("signature")
            if not before:
                break
    return list(found.values())


async def fetch_transactions(rpc: Rpc, decoder: Any, rows: list[dict[str, Any]], mint: str,
                             slope: float, intercept: float, min_ns: int, max_ns: int,
                             known_signatures: set[str]) -> tuple[list[dict[str, Any]], int]:
    ordered = sorted(rows, key=lambda r: (int(r.get("slot") or 0), int(r.get("blockTime") or 0), -int(r.get("retrieval_index") or 0)))
    recovered: list[dict[str, Any]] = []
    decoded_transactions = 0
    last_ns = min_ns
    for row in ordered:
        sig = str(row.get("signature") or "")
        if not sig or sig in known_signatures or row.get("err") is not None:
            continue
        tx = await rpc.call("getTransaction", [sig, {"encoding": "json", "commitment": "confirmed",
                                                     "maxSupportedTransactionVersion": 1}])
        if not tx or (tx.get("meta") or {}).get("err") is not None:
            continue
        decoded = decoder.anchor_events_from_logs((tx.get("meta") or {}).get("logMessages") or [], decoder.PUMP_PROGRAM_ID)
        relevant = [x for x in decoded if str(x.get("mint") or "") == mint and
                    x.get("anchor_event") in ("TradeEvent", "CompleteEvent", "CompletePumpAmmMigrationEvent")]
        if not relevant:
            continue
        decoded_transactions += 1
        slot = int(tx.get("slot") or row.get("slot") or 0)
        base_ns = int(intercept + slope * slot)
        for idx, item in enumerate(relevant):
            ns = max(base_ns + idx * 1_000_000, last_ns + 1_000_000)
            last_ns = ns
            if not (min_ns < ns <= max_ns):
                continue
            state = state_from_item(item, ns, sig, slot)
            if state:
                recovered.append(state)
    recovered.sort(key=lambda x: (x["ns"], x["slot"], x.get("signature", "")))
    return recovered, decoded_transactions


def restore_engine(raw: Mapping[str, Any]) -> Engine:
    cfg = Config(**raw["config"])
    engine = Engine(cfg)
    engine.accounts = copy.deepcopy(raw["accounts"])
    engine.bundles = copy.deepcopy(raw["bundles"])
    engine.history = copy.deepcopy(raw["history"])
    engine.sequence = int(raw["sequence"])
    engine.rejections = copy.deepcopy(raw.get("rejections", []))
    engine.errors = list(raw.get("errors", []))
    engine.latest = {}
    for mint, bundle in engine.bundles.items():
        path = bundle.get("market_path") or []
        if path:
            engine.latest[mint] = copy.deepcopy(path[-1])
    engine.validate()
    return engine


async def recover(args: argparse.Namespace) -> int:
    state = json.loads(args.failed_state.read_text(encoding="utf-8"))
    spec = json.loads(args.recovery_spec.read_text(encoding="utf-8"))
    sessions = state.get("sessions") or []
    if not sessions:
        raise RuntimeError("failed state has no sessions")
    failed_session = sessions[-1]
    session_hash_before = hashlib.sha256(json.dumps(failed_session, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    expected_session = spec.get("failed_session_id")
    if expected_session and failed_session.get("session_id") != expected_session:
        raise RuntimeError("recovery session id mismatch")
    errors = list(failed_session.get("errors") or [])
    if len(errors) != 1 or EXPECTED_ERROR_FRAGMENT not in errors[0]:
        raise RuntimeError(f"unexpected incident errors: {errors}")
    engine_raw = state["engine"]
    expected_matched = int(spec.get("expected_matched_closed", 74))
    if len(engine_raw.get("bundles") or {}) != 1:
        raise RuntimeError("recovery is scoped to exactly one interrupted bundle")
    engine = restore_engine(engine_raw)
    if engine.matched_closed() != expected_matched:
        raise RuntimeError(f"matched count mismatch before recovery: {engine.matched_closed()} != {expected_matched}")
    if engine.errors:
        raise RuntimeError(f"engine itself has unresolved errors: {engine.errors}")

    decoder = load_module("horizon_recovery_decoder", args.production_root / "src/memecoin_bot/realtime/pumpfun.py")
    mint, bundle = next(iter(engine.bundles.items()))
    decision = bundle["decision"]
    path = list(bundle.get("market_path") or [])
    if not path:
        raise RuntimeError("interrupted bundle has no recorded live market path")
    slope, intercept, residual_ns = linear_slot_clock(path)
    entry_ns = min(int(p["entry_ns"]) for p in bundle["positions"].values() if p.get("entry_ns"))
    cfg = engine.c
    target_end_ns = (
        entry_ns
        + (max(p["horizon_ms"] for p in bundle["positions"].values())
           + cfg.inclusion_delay_ms
           + cfg.max_attempts * cfg.retry_ms) * 1_000_000
        + (cfg.post_seconds + 2) * 1_000_000_000
    )
    start_s = decision["create_ns"] // 1_000_000_000
    end_s = target_end_ns // 1_000_000_000

    async with Rpc() as rpc:
        creation_tx = await rpc.call("getTransaction", [decision["create_signature"],
            {"encoding": "json", "commitment": "confirmed", "maxSupportedTransactionVersion": 1}])
        if not creation_tx or (creation_tx.get("meta") or {}).get("err") is not None:
            raise RuntimeError("creation transaction cannot be confirmed during recovery")
        creation_events = decoder.anchor_events_from_logs((creation_tx.get("meta") or {}).get("logMessages") or [], decoder.PUMP_PROGRAM_ID)
        create = next((x for x in creation_events if x.get("anchor_event") == "CreateEvent" and x.get("mint") == mint), None)
        if not create:
            raise RuntimeError("creation transaction does not decode to interrupted mint")
        proof = {"verified": True, "mint": mint, "signature": decision["create_signature"],
                 "slot": int(creation_tx["slot"]), "block_time": creation_tx.get("blockTime"),
                 "verified_at_ns": time.time_ns(), "confirmation": "confirmed",
                 "creation_transaction_actual_fee_sol": (creation_tx.get("meta") or {}).get("fee", 0) / 1e9,
                 "fee_attribution": "external creator transaction, NOT a Gambit fee",
                 "recovery_verification": True}
        bundle["creation_proof"] = proof

        addresses = [mint, str(create.get("bonding_curve") or "")]
        signature_rows = await signatures_for_window(rpc, addresses, start_s, end_s)
        known = {str(x.get("signature") or "") for x in path}
        recovered_states, decoded_txs = await fetch_transactions(
            rpc, decoder, signature_rows, mint, slope, intercept, int(path[-1]["ns"]), target_end_ns, known)

    # The P&L-critical 10 second exit may only be recovered when the slot-to-live
    # clock mapping is not ambiguous around its modeled fill due time. If an
    # observed reserve transition lands inside the uncertainty band, stop rather
    # than choose a favorable side of the event.
    ten_second_due_ns = entry_ns + (10_000 + cfg.inclusion_delay_ms) * 1_000_000
    timing_uncertainty_ns = max(100_000_000, int(3 * residual_ns))
    ambiguous = [x for x in recovered_states if abs(int(x["ns"]) - ten_second_due_ns) <= timing_uncertainty_ns]
    if ambiguous:
        raise RuntimeError(
            f"recovery timing ambiguous around 10s modeled fill: {len(ambiguous)} state transition(s) "
            f"within +/-{timing_uncertainty_ns/1e6:.1f}ms"
        )

    all_states = [copy.deepcopy(x) for x in path]
    all_states.extend(recovered_states)
    by_sig_ns: dict[tuple[str, int], dict[str, Any]] = {}
    for row in all_states:
        by_sig_ns[(str(row.get("signature") or ""), int(row["ns"]))] = row
    all_states = sorted(by_sig_ns.values(), key=lambda x: (x["ns"], x.get("slot", 0)))
    current = copy.deepcopy(all_states[0])
    cursor = 0
    start_ns = int(path[-1]["ns"])
    now = start_ns
    step = 1_000_000
    while now <= target_end_ns and engine.bundles and not engine.errors:
        while cursor + 1 < len(all_states) and int(all_states[cursor + 1]["ns"]) <= now:
            cursor += 1
            current = copy.deepcopy(all_states[cursor])
        engine.tick(now, {mint: current}, now)
        now += step

    if engine.errors:
        raise RuntimeError(f"incident recovery hit a model error: {engine.errors}")
    if engine.bundles:
        raise RuntimeError("incident recovery did not resolve all open/post-exit state")
    if engine.matched_closed() != expected_matched + 1:
        raise RuntimeError(f"recovered cohort did not become matched: {engine.matched_closed()}")
    engine.validate()

    original_status = failed_session.get("status")
    failed_session["original_status"] = original_status
    failed_session["incident_recovery"] = {
        "status": "VERIFIED",
        "scope": "incident-only immutable-chain completion of already-live-entered cohort",
        "original_errors_preserved": True,
        "source_artifact_digest": spec.get("artifact_digest"),
    }

    tools = Path(__file__).resolve().parents[1] / "tools"
    previous_impl = copy.deepcopy(state.get("implementation") or {})
    current_impl = {
        "golden_horizon_engine.py": sha256_path(tools / "golden_horizon_engine.py"),
        "golden_horizon_live.py": sha256_path(tools / "golden_horizon_live.py"),
    }
    state["implementation"] = current_impl
    state["engine"] = engine.persistence()
    incident = {
        "status": "RECOVERED",
        "failed_session_id": failed_session.get("session_id"),
        "failed_session_sha256_before_recovery": session_hash_before,
        "original_errors": errors,
        "source_artifact_run_id": spec.get("run_id"),
        "source_artifact_name": spec.get("artifact_name"),
        "source_artifact_digest": spec.get("artifact_digest"),
        "recovery_method": "CONFIRMED_SOLANA_HISTORY_FOR_ALREADY_LIVE_ENTERED_COHORT_ONLY",
        "new_signal_or_selection_replay": False,
        "historical_market_data_used_for_missing_incident_tail_only": True,
        "interrupted_mint": mint,
        "matched_before": expected_matched,
        "matched_after": engine.matched_closed(),
        "recovered_market_states": len(recovered_states),
        "decoded_recovery_transactions": decoded_txs,
        "queried_signature_rows": len(signature_rows),
        "slot_clock_ns_per_slot": slope,
        "slot_clock_fit_rmse_ms": residual_ns / 1e6,
        "recovery_clock_step_ms": 1.0,
        "timing_uncertainty_ms": timing_uncertainty_ns / 1e6,
        "pnl_critical_transition_ambiguity_count": len(ambiguous),
        "previous_implementation": previous_impl,
        "current_implementation": current_impl,
        "completed_at_ns": time.time_ns(),
        "limitations": [
            "The coin was selected and entered prospectively from the live stream before the infrastructure failure.",
            "Only the missing tail of that already-open cohort was reconstructed from confirmed immutable Solana transaction history.",
            "Recovered transaction timing is mapped from slot to the live receive clock and is not a new measurement of Axiom or validator execution latency.",
        ],
    }
    state.setdefault("recoveries", []).append(incident)
    write_json(args.output, state)
    summary = {"status": "RECOVERED_CONTINUATION_READY", "matched_closed": engine.matched_closed(),
               "mint": mint, "recovered_states": len(recovered_states), "rpc_calls": rpc.calls,
               "incident": incident}
    write_json(args.summary, summary)
    print(json.dumps({"status": summary["status"], "matched": summary["matched_closed"],
                      "recovered_states": summary["recovered_states"]}))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--failed-state", type=Path, required=True)
    p.add_argument("--production-root", type=Path, required=True)
    p.add_argument("--recovery-spec", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--summary", type=Path, required=True)
    return asyncio.run(recover(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
