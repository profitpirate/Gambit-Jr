#!/usr/bin/env python3
"""Infrastructure-only migration bridge for Golden forward-paper campaigns.

This module is intentionally outside the frozen ``golden_top4_*`` and
``golden_loss2s_*`` source sets. It does not alter selection, sizing, timers,
accounting formulas, or any transaction path. It has two jobs:

1. Recover the one preserved checkpoint that was blocked because a Pump bonding
   curve migrated while PAPER inventory was open. The unsupported cohort is
   explicitly excluded rather than assigned a made-up exit. Successful-entry
   debits/rent are rolled back; already-modelled failed-attempt fees are retained
   and attributed as failed-entry fees. The exclusion is permanently audited.
2. For subsequent live windows, observe PumpSwap Buy/Sell reserve events after a
   Pump migration and feed those real reserve observations back into the frozen
   paper engine. During the short route handoff the position is frozen. If no
   supported PumpSwap state appears within the route timeout, only that cohort is
   technically excluded; the whole campaign no longer dies.

There is still no signer, sendTransaction, or funded execution path here.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import copy
import hashlib
import json
import math
import os
import signal
import struct
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Mapping

BRIDGE_VERSION = "golden-migration-route-bridge-1"
PUMP_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMP_AMM_PROGRAM_ID = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
SYSTEM_PROGRAM_ID = "11111111111111111111111111111111"
WSOL_MINT = "So11111111111111111111111111111111111111112"
SOL_QUOTES = {None, "", SYSTEM_PROGRAM_ID, WSOL_MINT}
ROUTE_TIMEOUT_NS = 15_000_000_000
MIGRATION_ERROR = "UNRESOLVED_MIGRATION_NO_ROUTE"
BUY_EVENT_DISC = hashlib.sha256(b"event:BuyEvent").digest()[:8]
SELL_EVENT_DISC = hashlib.sha256(b"event:SellEvent").digest()[:8]


def _migration_errors(rows: list[str] | None) -> list[str]:
    return [x for x in (rows or []) if isinstance(x, str) and x.endswith(":" + MIGRATION_ERROR)]


def _other_errors(rows: list[str] | None) -> list[str]:
    return [x for x in (rows or []) if x not in _migration_errors(rows)]


def _error_mint(text: str) -> str:
    parts = text.split(":")
    if len(parts) < 3 or parts[-1] != MIGRATION_ERROR:
        raise ValueError("not a migration-route error")
    return parts[-2]


def _failed_action_fees(p: Mapping[str, Any]) -> tuple[float, dict[str, float], list[dict[str, Any]]]:
    fees: Counter[str] = Counter()
    acts: list[dict[str, Any]] = []
    for act in p.get("actions", []):
        if act.get("outcome") == "MODELLED_LANDED_FAILURE":
            acts.append(copy.deepcopy(act))
            fees.update({k: float(v) for k, v in (act.get("fees") or {}).items()})
    return sum(fees.values()), dict(fees), acts


def _aborted_failure_copy(p: Mapping[str, Any], failure_fees: dict[str, float], failure_actions: list[dict[str, Any]]) -> dict[str, Any]:
    q = copy.deepcopy(dict(p))
    q.update(
        status="NO_ENTRY",
        abort_reason="TECHNICAL_MIGRATION_ROUTE_EXCLUSION_AFTER_ENTRY",
        original=0.0,
        remaining=0.0,
        rent=0.0,
        buy_cost=0.0,
        curve_sol=0.0,
        sell_gross=0.0,
        intent=None,
        actions=failure_actions,
        fees=failure_fees,
        technical_exclusion=True,
        excluded_from_forward_counts=True,
    )
    q.pop("entry_ns", None)
    q.pop("exit_ns", None)
    q.pop("post", None)
    q.pop("gross_pnl_sol", None)
    q.pop("net_pnl_sol", None)
    q.pop("net_return", None)
    q.pop("hold_ms", None)
    return q


def _exclude_base_state(raw: dict[str, Any], mint: str) -> dict[str, Any]:
    """Drop one unresolved bundle from a serialized frozen base/SingleEngine state."""
    b = (raw.get("bundles") or {}).pop(mint, None)
    result: dict[str, Any] = {"mint": mint, "positions": {}}
    if not b:
        raw["errors"] = _other_errors(raw.get("errors"))
        if "clean" in raw:
            raw["clean"] = not raw.get("bundles") and not raw.get("errors")
        return result

    for arm, p in b.get("positions", {}).items():
        a = raw["accounts"][arm]
        failed_total, failed_fees, failed_actions = _failed_action_fees(p)
        refunded = 0.0
        status = p.get("status")
        if status == "OPEN":
            refunded = float(p.get("buy_cost", 0.0)) + float(p.get("rent", 0.0))
            a["cash"] = float(a["cash"]) + refunded
            a["rent_locked"] = max(0.0, float(a.get("rent_locked", 0.0)) - float(p.get("rent", 0.0)))
        elif status == "PENDING":
            refunded = 0.0
        elif status in ("NO_ENTRY", "CLOSED"):
            raise ValueError(f"migration recovery saw terminal position: {arm}:{status}")
        else:
            raise ValueError(f"migration recovery saw unknown position status: {arm}:{status}")

        if failed_total:
            a["failed_entry_fees"] = float(a.get("failed_entry_fees", 0.0)) + failed_total
            a.setdefault("aborted_entries", []).append(
                _aborted_failure_copy(p, failed_fees, failed_actions)
            )

        result["positions"][arm] = {
            "signal_id": p.get("signal_id"),
            "status_before": status,
            "successful_entry_debit_refunded_sol": refunded,
            "failed_attempt_fees_retained_sol": failed_total,
            "action_count_before": len(p.get("actions", [])),
            "failed_action_count_retained": len(failed_actions),
        }

    raw.get("latest", {}).pop(mint, None)
    raw["errors"] = _other_errors(raw.get("errors"))
    if "clean" in raw:
        raw["clean"] = not raw.get("bundles") and not raw.get("errors")
    return result


def _exclude_single_snapshot(snap: dict[str, Any], mint: str) -> dict[str, Any]:
    result = _exclude_base_state(snap["engine"], mint)
    snap.get("latest", {}).pop(mint, None)
    uncertain = set(snap.get("uncertain_mints", [])); uncertain.add(mint)
    snap["uncertain_mints"] = sorted(uncertain)
    snap["accepting"] = not bool(snap["engine"].get("errors"))
    return result


def _audit_cursor_for_aborted(engine: dict[str, Any], display_arm: str, signal_id: str, retained_actions: int) -> None:
    key = f"{display_arm}:{signal_id}"
    if retained_actions:
        engine.setdefault("action_cursors", {})[key] = retained_actions
    else:
        engine.setdefault("action_cursors", {}).pop(key, None)


def recover_serialized_state(state: dict[str, Any], kind: str) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Recover only the exact known migration-route blocker; refuse everything else."""
    state = copy.deepcopy(state)
    eng = state["engine"]
    errs = _migration_errors(eng.get("errors"))
    if not errs:
        return state, None
    if _other_errors(eng.get("errors")):
        raise ValueError("refusing recovery with unrelated engine errors")
    mints = sorted({_error_mint(x) for x in errs})
    if len(mints) != 1:
        raise ValueError("expected exactly one unresolved migration cohort")
    mint = mints[0]

    audit: dict[str, Any] = {
        "schema": BRIDGE_VERSION,
        "recovered_at_ns": time.time_ns(),
        "kind": kind,
        "mint": mint,
        "original_errors": errs,
        "policy": "exclude unsupported interrupted cohort; refund successful entry debit/rent; retain modelled failed-attempt fees",
        "models": {},
        "forward_counts_unchanged_by_exclusion": True,
        "paper_only": True,
    }

    control_result = _exclude_base_state(eng["controls"], mint)
    for arm, row in control_result["positions"].items():
        audit["models"][arm] = row
        _audit_cursor_for_aborted(eng, arm, row["signal_id"], row["failed_action_count_retained"])
    cu = set(eng.get("control_uncertain", [])); cu.add(mint); eng["control_uncertain"] = sorted(cu)

    v2_candidate = _exclude_single_snapshot(eng["v2"]["candidate"], mint)
    for _, row in v2_candidate["positions"].items():
        audit["models"]["FULL_3S_V2"] = row
        _audit_cursor_for_aborted(eng, "FULL_3S_V2", row["signal_id"], row["failed_action_count_retained"])
    _exclude_single_snapshot(eng["v2"]["observer"], mint)
    eng["v2"]["halt_new_entries"] = False
    eng["v2"]["interrupted"] = False

    if kind == "loss2s":
        loss_control = _exclude_base_state(eng["loss_control"], mint)
        for _, row in loss_control["positions"].items():
            audit["models"]["FULL_3S_2S_LOSS_EXIT"] = row
            _audit_cursor_for_aborted(eng, "FULL_3S_2S_LOSS_EXIT", row["signal_id"], row["failed_action_count_retained"])
        lu = set(eng.get("loss_control_uncertain", [])); lu.add(mint); eng["loss_control_uncertain"] = sorted(lu)
        loss_v2 = _exclude_single_snapshot(eng["loss_v2"], mint)
        for _, row in loss_v2["positions"].items():
            audit["models"]["FULL_3S_V2_2S_LOSS_EXIT"] = row
            _audit_cursor_for_aborted(eng, "FULL_3S_V2_2S_LOSS_EXIT", row["signal_id"], row["failed_action_count_retained"])

    eng.get("bundles", {}).pop(mint, None)
    eng["errors"] = _other_errors(eng.get("errors"))
    eng["clean"] = not eng.get("bundles") and not eng.get("errors")

    events = eng.setdefault("events", [])
    event_id = max((int(x.get("event_id", 0)) for x in events), default=0) + 1
    events.append({
        "kind": "TECHNICAL_MIGRATION_RECOVERY_EXCLUSION",
        "event_id": event_id,
        "reported_ns": audit["recovered_at_ns"],
        "mint": mint,
        "execution": "PAPER_MODEL",
        "actual_axiom_fill": False,
        "actual_profit_sol": None,
        "actual_fees_sol": None,
        "excluded_from_forward_counts": True,
        "recovery": copy.deepcopy(audit),
    })

    recovered_sessions = 0
    for sess in state.get("sessions", []):
        se = _migration_errors(sess.get("errors"))
        other = _other_errors(sess.get("errors"))
        if se:
            if other:
                raise ValueError("refusing recovery with unrelated session errors")
            sess["recovered_errors"] = list(dict.fromkeys((sess.get("recovered_errors") or []) + se))
            sess["errors"] = []
            sess["status"] = "RECOVERED_TECHNICAL_MIGRATION_EXCLUSION"
            sess["recovery_excluded_mints"] = sorted(set(sess.get("recovery_excluded_mints", [])) | {mint})
            sess["active_bundles"] = 0
            recovered_sessions += 1
    if not recovered_sessions:
        raise ValueError("engine had migration blocker but no matching session error")

    state.setdefault("migration_recoveries", []).append(audit)
    state["migration_route_bridge"] = BRIDGE_VERSION
    return state, audit


def _write_json(path: Path, obj: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, sort_keys=True, allow_nan=False) + "\n")
    tmp.replace(path)


def recover_repo_checkpoint(kind: str) -> dict[str, Any] | None:
    prefix = "docs/research/golden-top4-" if kind == "top4" else "docs/research/golden-loss2s-"
    path = Path(prefix + "state.json")
    if not path.exists() or path.stat().st_size == 0:
        return None
    before = json.loads(path.read_text())
    after, audit = recover_serialized_state(before, kind)
    if audit:
        _write_json(path, after)
        audit_path = Path(prefix + "migration-recovery.json")
        _write_json(audit_path, audit)
        print("MIGRATION_RECOVERY_APPLIED " + json.dumps({"kind": kind, "mint": audit["mint"], "models": sorted(audit["models"])}), flush=True)
    return audit


class MigrationRouter:
    def __init__(self):
        self.pool_to_route: dict[str, dict[str, Any]] = {}
        self.stats: Counter[str] = Counter()

    @staticmethod
    def _program_data(logs: list[Any], program_id: str):
        stack: list[str] = []
        for line in logs:
            text = str(line)
            if text.startswith("Program ") and " invoke [" in text:
                stack.append(text.split(" ", 2)[1]); continue
            if text.startswith("Program ") and (text.endswith(" success") or " failed: " in text):
                completed = text.split(" ", 2)[1]
                if completed in stack:
                    while stack:
                        if stack.pop() == completed: break
                continue
            if "Program data: " not in text or not stack or stack[-1] != program_id:
                continue
            try:
                yield base64.b64decode(text.split("Program data: ", 1)[1].strip(), validate=True)
            except Exception:
                continue

    def decode_amm(self, decoder: Any, logs: list[Any]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for raw in self._program_data(logs, PUMP_AMM_PROGRAM_ID):
            if len(raw) < 152 or raw[:8] not in (BUY_EVENT_DISC, SELL_EVENT_DISC):
                continue
            values = struct.unpack_from("<q" + "Q" * 13, raw, 8)
            pool_base = int(values[5]); pool_quote = int(values[6])
            pool_raw = raw[8 + 14 * 8: 8 + 14 * 8 + 32]
            pool = decoder.b58encode(pool_raw)
            route = self.pool_to_route.get(pool)
            if not route or route.get("quote_mint") not in SOL_QUOTES:
                self.stats["amm_event_without_supported_route"] += 1
                continue
            out.append({
                "anchor_event": "PumpSwapBuyEvent" if raw[:8] == BUY_EVENT_DISC else "PumpSwapSellEvent",
                "mint": route["mint"],
                "pool": pool,
                "quote_mint": route.get("quote_mint"),
                "pool_base_token_reserves": pool_base,
                "pool_quote_token_reserves": pool_quote,
                "virtual_quote_reserves": 0,
                "route": "PUMPSWAP_EVENT",
            })
            self.stats["amm_route_events"] += 1
        return out

    def wrap_decoder(self, decoder: Any):
        router = self
        class Wrapped:
            def __getattr__(self, name): return getattr(decoder, name)
            def anchor_events_from_logs(self, logs, program_id=None):
                pump = decoder.anchor_events_from_logs(logs, decoder.PUMP_PROGRAM_ID)
                for item in pump:
                    if item.get("anchor_event") == "CompletePumpAmmMigrationEvent" and item.get("pool") and item.get("mint"):
                        router.pool_to_route[item["pool"]] = {
                            "mint": item["mint"], "quote_mint": item.get("quote_mint"),
                            "migration_timestamp": item.get("timestamp")}
                        router.stats["migration_routes"] += 1
                return pump + router.decode_amm(decoder, logs)
        return Wrapped()

    def state_from_item(self, original, item: dict[str, Any], recv: int, sig: str, slot: int):
        kind = item.get("anchor_event")
        if kind in ("CompleteEvent", "CompletePumpAmmMigrationEvent"):
            self.stats["migration_pending_states"] += 1
            return {"ns": recv, "signature": sig, "slot": slot, "complete": False,
                    "vsol": 0.0, "vtok": 0.0, "migration_pending": True,
                    "migration_event": kind, "pool": item.get("pool"), "route": "PUMP_TO_PUMPSWAP_HANDOFF"}
        if kind in ("PumpSwapBuyEvent", "PumpSwapSellEvent"):
            base_reserve = item.get("pool_base_token_reserves")
            quote_reserve = item.get("pool_quote_token_reserves")
            virtual_quote = item.get("virtual_quote_reserves", 0)
            if not all(isinstance(x, int) and x >= 0 for x in (base_reserve, quote_reserve, virtual_quote)):
                return None
            if base_reserve <= 0 or quote_reserve + virtual_quote <= 0:
                return None
            self.stats["pumpswap_states"] += 1
            return {"ns": recv, "signature": sig, "slot": slot, "complete": False,
                    "vsol": (quote_reserve + virtual_quote) / 1e9,
                    "vtok": base_reserve / 1e6, "rtok": base_reserve / 1e6,
                    "route": "PUMPSWAP_EVENT", "pool": item.get("pool"),
                    "virtual_quote_reserves_raw": virtual_quote}
        return original(item, recv, sig, slot)

    async def socket_worker(self, original_decoder: Any, url: str, decoder: Any, queue: asyncio.Queue,
                            stop: asyncio.Event, stats: dict[str, Any], errors: list[str]) -> None:
        import aiohttp
        st = stats.setdefault(url, {"connections": 0, "disconnects": 0, "messages": 0, "last_receive_ns": None,
                                    "pump_and_pumpswap_subscriptions": True})
        async with aiohttp.ClientSession() as session:
            while not stop.is_set():
                try:
                    async with session.ws_connect(url, heartbeat=10, max_msg_size=8*1024*1024,
                         timeout=aiohttp.ClientWSTimeout(ws_receive=15, ws_close=5)) as ws:
                        st["connections"] += 1
                        await ws.send_json({"jsonrpc":"2.0","id":1,"method":"logsSubscribe",
                            "params":[{"mentions":[PUMP_PROGRAM_ID]},{"commitment":"processed"}]})
                        await ws.send_json({"jsonrpc":"2.0","id":2,"method":"logsSubscribe",
                            "params":[{"mentions":[PUMP_AMM_PROGRAM_ID]},{"commitment":"processed"}]})
                        while not stop.is_set():
                            msg = await ws.receive()
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                received = time.time_ns(); d = json.loads(msg.data)
                                if d.get("error"): raise RuntimeError(str(d["error"]))
                                if d.get("method") != "logsNotification": continue
                                st["messages"] += 1; st["last_receive_ns"] = received
                                try: queue.put_nowait((received, url, d))
                                except asyncio.QueueFull:
                                    errors.append("FEED_QUEUE_OVERFLOW_DATA_LOSS"); stop.set(); return
                            elif msg.type in (aiohttp.WSMsgType.CLOSED,aiohttp.WSMsgType.CLOSE,aiohttp.WSMsgType.ERROR):
                                break
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    st["last_error"] = f"{type(exc).__name__}:{str(exc)[:140]}"
                st["disconnects"] += 1
                await asyncio.sleep(.5)


def _failure_fees_object(p: Mapping[str, Any]):
    total, by_cat, acts = _failed_action_fees(p)
    return total, by_cat, acts


def _exclude_base_object(eng: Any, mint: str) -> dict[str, Any]:
    b = eng.bundles.pop(mint, None)
    out: dict[str, Any] = {}
    if not b:
        eng.errors = _other_errors(eng.errors)
        return out
    for arm, p in b["positions"].items():
        a = eng.accounts[arm]
        failed_total, failed_fees, failed_actions = _failure_fees_object(p)
        refunded = 0.0
        if p["status"] == "OPEN":
            refunded = float(p.get("buy_cost", 0.0)) + float(p.get("rent", 0.0))
            a["cash"] += refunded; a["rent_locked"] = max(0.0, a["rent_locked"] - p.get("rent", 0.0))
        elif p["status"] != "PENDING":
            raise RuntimeError("unexpected terminal migration-exclusion state")
        if failed_total:
            a["failed_entry_fees"] += failed_total
            a["aborted_entries"].append(_aborted_failure_copy(p, failed_fees, failed_actions))
        out[arm] = {"signal_id":p["signal_id"],"refunded_sol":refunded,
                    "failed_attempt_fees_retained_sol":failed_total,"retained_failed_actions":len(failed_actions)}
    eng.latest.pop(mint, None)
    eng.errors = _other_errors(eng.errors)
    return out


def _exclude_single_object(eng: Any, mint: str) -> dict[str, Any]:
    out = _exclude_base_object(eng, mint)
    if hasattr(eng, "_view"): eng._view.pop(mint, None)
    if hasattr(eng, "uncertain_mints"): eng.uncertain_mints.add(mint)
    if hasattr(eng, "accepting"): eng.accepting = not bool(eng.errors)
    return out


def _campaign_exclude(self: Any, mint: str, marker: Mapping[str, Any], kind: str) -> None:
    if mint not in self.bundles:
        return
    models: dict[str, Any] = {}
    parent = _exclude_base_object(self.controls, mint)
    for arm,row in parent.items(): models[arm]=row
    self.control_uncertain.add(mint)
    cand = _exclude_single_object(self.runtime.candidate, mint)
    for _,row in cand.items(): models["FULL_3S_V2"]=row
    _exclude_single_object(self.runtime.observer, mint)
    self.runtime.halt_new_entries = False; self.runtime.interrupted = False

    if kind == "loss2s":
        lc = _exclude_base_object(self.loss_control, mint)
        for _,row in lc.items(): models["FULL_3S_2S_LOSS_EXIT"]=row
        self.loss_control_uncertain.add(mint)
        lv = _exclude_single_object(self.loss_v2, mint)
        for _,row in lv.items(): models["FULL_3S_V2_2S_LOSS_EXIT"]=row

    for arm,row in models.items():
        key=f"{arm}:{row['signal_id']}"
        if row["retained_failed_actions"]: self.action_cursors[key]=row["retained_failed_actions"]
        else: self.action_cursors.pop(key,None)
    self._event({"kind":"TECHNICAL_MIGRATION_ROUTE_EXCLUSION","mint":mint,
                 "reason":"NO_SUPPORTED_PUMPSWAP_STATE_WITHIN_ROUTE_TIMEOUT",
                 "marker":dict(marker),"route_timeout_ms":ROUTE_TIMEOUT_NS/1e6,
                 "models":models,"excluded_from_forward_counts":True,
                 "bridge_version":BRIDGE_VERSION})
    self.validate()


def patch_campaign_tick(cls: type, kind: str) -> None:
    if getattr(cls, "_migration_bridge_patched", False): return
    original = cls.tick
    def tick(self, now, states, last_feed_ns):
        pending = {m:s for m,s in states.items() if isinstance(s,dict) and s.get("migration_pending") and m in self.bundles}
        for mint, marker in list(pending.items()):
            if now - int(marker["ns"]) >= ROUTE_TIMEOUT_NS:
                _campaign_exclude(self, mint, marker, kind)
                pending.pop(mint, None)
        if not pending:
            return original(self, now, states, last_feed_ns)
        filtered = dict(states)
        healthy = 0 <= now-last_feed_ns <= self.c.feed_stale_ms*1_000_000
        cand_before={m:(m in self.runtime.candidate.uncertain_mints) for m in pending}
        obs_before={m:(m in self.runtime.observer.uncertain_mints) for m in pending}
        lv_before={}
        if kind=="loss2s": lv_before={m:(m in self.loss_v2.uncertain_mints) for m in pending}
        for m in pending: filtered[m]=None
        result=original(self, now, filtered, last_feed_ns)
        if healthy:
            for m,was in cand_before.items():
                if not was:self.runtime.candidate.uncertain_mints.discard(m)
            for m,was in obs_before.items():
                if not was:self.runtime.observer.uncertain_mints.discard(m)
            if kind=="loss2s":
                for m,was in lv_before.items():
                    if not was:self.loss_v2.uncertain_mints.discard(m)
        return result
    cls.tick=tick
    cls._migration_bridge_patched=True


async def run_collector(kind: str, args: argparse.Namespace) -> int:
    import aiohttp
    if kind == "loss2s":
        import golden_loss2s_solana as loss_adapter
        adapter = loss_adapter.base
    else:
        import golden_top4_solana as adapter

    m=adapter.verify_manifest(args.manifest); args.output.mkdir(parents=True,exist_ok=True)
    if args.resume:
        previous=json.loads(args.resume.read_text())
        if previous.get("top4_campaign")!=m["campaign_name"]: raise ValueError("different campaign state")
    for cls in (adapter.OriginalEngine,adapter.CampaignEngine):
        cls.output=args.output;cls.smoke=args.smoke;cls.require_reporter=not args.smoke

    collector=adapter.collector
    src=Path(collector.__file__).read_text()
    old='time.monotonic() >= deadline or engine.matched_closed() >= args.target'
    assert src.count(old)==1
    src=src.replace(old,old+" or (out / 'stop-request').exists()")
    src=src.replace('last_save >= 30','last_save >= 5')
    exec(compile(src,collector.__file__,'exec'),collector.__dict__)
    collector.Engine=adapter.CampaignEngine;collector.ARMS=adapter.ARMS;collector.VERSION=adapter.VERSION
    collector.verify_creation=adapter.verify_creation;collector.READ_METHODS.add('getSignaturesForAddress')

    router=MigrationRouter()
    original_state_from_item=collector.state_from_item
    collector.state_from_item=lambda item,recv,sig,slot: router.state_from_item(original_state_from_item,item,recv,sig,slot)
    original_load=collector.load_module
    def bridge_load(name,path):
        mod=original_load(name,path)
        return router.wrap_decoder(mod) if name=='horizon_pump_decoder' else mod
    collector.load_module=bridge_load
    collector.socket_worker=lambda url,decoder,queue,stop,stats,errors: router.socket_worker(decoder,url,decoder,queue,stop,stats,errors)
    patch_campaign_tick(adapter.CampaignEngine,kind)

    old_journal=collector.Journal;old_write=collector.write_json
    observer=None;task=None;stop=asyncio.Event()
    ej=old_journal(args.output/'e4-rpc-observations.jsonl')
    decoder=collector.load_module('top4_decoder',args.production_root/'src/memecoin_bot/realtime/pumpfun.py')
    prior=json.loads(args.e4_resume.read_text()) if args.e4_resume and args.e4_resume.exists() else None
    async with aiohttp.ClientSession() as session:
        rpc=collector.Rpc(session,ej)
        class SessionJournal(old_journal):
            def add(self,kind_name,data,durable=False):
                nonlocal observer,task
                super().add(kind_name,data,durable)
                if kind_name=='SESSION' and observer is None:
                    observer=adapter.Observer(rpc,decoder,args.output,int(data['id'].rsplit(':',1)[1]),prior)
                    task=asyncio.create_task(observer.run(stop))
        def publish_local(path,data):
            if path.name in ('status.json','final.json') and isinstance(data,dict):
                e=adapter.OriginalEngine.last_instance
                if e is None:e=adapter.CampaignEngine.last_instance
                if e is not None:
                    data=dict(data)
                    data.update(models=list(adapter.ARMS),collector_active=data.get('status') in ('RUNNING','DRAINING'),
                        min_verified_closed_per_model=e.matched_closed(),verified_forward_counts=e.forward_counts(),
                        count_semantics='minimum verified per-model count; v2 selections may differ',
                        collector_scope=adapter.SOURCE,axiom_listing_verified=None,
                        local_four_model_tick_ms=collector.quantiles(e.local_ticks),
                        local_four_model_selection_ms=collector.quantiles(e.local_selection),
                        total_report_events=len(e.events),per_action_reporting_file='activity.json',
                        operator_stop_requested=(args.output/'stop-request').exists(),live_testing_started=not args.smoke,
                        migration_route_bridge=BRIDGE_VERSION,migration_route_stats=dict(router.stats),
                        pumpswap_program_id=PUMP_AMM_PROGRAM_ID,
                        e4_monitor={'wallet':adapter.WALLET,'checked_ns':observer.checked_ns if observer else None,
                                    'coverage_complete':observer.complete_pages if observer else False,'used_in_selection':False})
            if path.name=='state.json':
                data=dict(data);data['top4_campaign']=m['campaign_name'];data['market_source']=adapter.SOURCE
                data['migration_route_bridge']=BRIDGE_VERSION;data['migration_route_stats']=dict(router.stats)
            old_write(path,data)
        collector.Journal=SessionJournal;collector.write_json=publish_local
        try:
            rc=await collector.run(args)
        finally:
            ending=time.time_ns();stop.set()
            if task:
                try:
                    await asyncio.wait_for(task,timeout=30)
                    await asyncio.wait_for(observer.poll(ending),timeout=90)
                except Exception as exc:
                    task.cancel();await asyncio.gather(task,return_exceptions=True)
                    observer.errors.append({'error':'FINAL_DRAIN:'+type(exc).__name__});observer.complete_pages=False
                observer.ended_ns=ending
                final=observer.save();final['market_session_end_ns']=ending
                adapter.write_json(args.output/'e4-status.json',final)
            ej.close();collector.Journal=old_journal;collector.write_json=old_write;collector.load_module=original_load
    if args.smoke:
        if not observer or not observer.checked_ns or not observer.complete_pages:
            raise RuntimeError('E4 preflight could not establish read-only observation')
        adapter.write_json(args.output/'readiness.json',{'status':'SOLANA_AND_E4_PREFLIGHT_PASS','source':adapter.SOURCE,
            'live_testing_started':False,'smoke_trades':0,'synthetic_data':False,'axiom_feed_verified':False,
            'e4_checked_ns':observer.checked_ns,'migration_route_bridge':BRIDGE_VERSION,
            'market_smoke':json.loads((args.output/'smoke-pass.json').read_text())})
    return rc


def run_campaign(kind: str, corpus: str, duration: int) -> int:
    recover_repo_checkpoint(kind)
    if kind == "loss2s":
        import golden_loss2s_campaign as campaign
    else:
        import golden_top4_campaign as campaign
    original_command=campaign.command
    bridge_path=str(Path(__file__).resolve())
    def command(argv,log):
        if len(argv)>1 and (str(argv[1]).endswith('golden_top4_solana.py') or str(argv[1]).endswith('golden_loss2s_solana.py')):
            new=[sys.executable,bridge_path,'collect','--kind',kind]+list(argv[2:])
            with log.open('w') as f:return subprocess.call(new,stdout=f,stderr=subprocess.STDOUT)
        return original_command(argv,log)
    campaign.command=command
    a=argparse.Namespace(corpus=corpus,duration=duration)
    return campaign.launch(a)


def self_test() -> None:
    assert BUY_EVENT_DISC == bytes([103,244,82,31,44,245,119,119])
    assert SELL_EVENT_DISC == bytes([62,47,55,10,165,3,220,42])
    p={"status":"OPEN","signal_id":"1:M","buy_cost":.2,"rent":.01,"actions":[
        {"outcome":"MODELLED_LANDED_FAILURE","fees":{"network_base_sol":.001}},
        {"outcome":"MODELLED_FILL","fees":{"platform_sol":.002}}],"fees":{"network_base_sol":.001,"platform_sol":.002},
        "remaining":1.0,"original":1.0,"curve_sol":.19,"sell_gross":0.0,"intent":None}
    raw={"accounts":{"FULL_3S":{"cash":.789,"rent_locked":.01,"failed_entry_fees":0.0,"aborted_entries":[]}},
         "bundles":{"M":{"positions":{"FULL_3S":p}},},"latest":{},"errors":["1:M:"+MIGRATION_ERROR],"clean":False}
    r=_exclude_base_state(raw,"M")
    assert math.isclose(raw["accounts"]["FULL_3S"]["cash"],.999,abs_tol=1e-12)
    assert math.isclose(raw["accounts"]["FULL_3S"]["failed_entry_fees"],.001,abs_tol=1e-12)
    assert not raw["bundles"] and not raw["errors"] and raw["clean"]
    assert r["positions"]["FULL_3S"]["failed_action_count_retained"]==1
    print("MIGRATION_BRIDGE_SELF_TEST_PASS",BRIDGE_VERSION)


def main() -> int:
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='cmd',required=True)
    sub.add_parser('self-test')
    r=sub.add_parser('recover-file');r.add_argument('--kind',choices=['top4','loss2s'],required=True);r.add_argument('--input',type=Path,required=True);r.add_argument('--output',type=Path,required=True)
    c=sub.add_parser('campaign');c.add_argument('--kind',choices=['top4','loss2s'],required=True);c.add_argument('--corpus',required=True);c.add_argument('--duration',type=int,default=14400)
    x=sub.add_parser('collect');x.add_argument('--kind',choices=['top4','loss2s'],required=True);x.add_argument('--production-root',type=Path,required=True);x.add_argument('--corpus',type=Path,required=True);x.add_argument('--manifest',type=Path,required=True);x.add_argument('--output',type=Path,required=True);x.add_argument('--resume',type=Path);x.add_argument('--e4-resume',type=Path);x.add_argument('--window',default='1');x.add_argument('--duration',type=int,default=14400);x.add_argument('--target',type=int,default=100);x.add_argument('--smoke',action='store_true')
    a=p.parse_args()
    if a.cmd=='self-test':self_test();return 0
    if a.cmd=='recover-file':
        d=json.loads(a.input.read_text());out,audit=recover_serialized_state(d,a.kind);_write_json(a.output,out);print(json.dumps(audit,indent=2));return 0
    if a.cmd=='campaign':return run_campaign(a.kind,a.corpus,a.duration)
    if a.cmd=='collect':
        if a.target!=100:p.error('100 verified closed trades per model is the frozen minimum')
        return asyncio.run(run_collector(a.kind,a))
    raise AssertionError(a.cmd)

if __name__=='__main__':raise SystemExit(main())
