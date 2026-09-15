#!/usr/bin/env python3
"""Read-only, freshly arriving Solana-launch collector for 12 exit horizons.

This is LIVE MARKET DATA with MODELLED execution, never an Axiom account bot.
The RPC allowlist intentionally excludes transaction-submission methods.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import importlib.util
import json
import os
import signal
import sys
import time
from collections import Counter, deque
from dataclasses import asdict
from pathlib import Path
from typing import Any

import aiohttp
from golden_horizon_engine import ARMS, Config, Engine, VERSION, quantiles, valid

RPCS = ("https://api.mainnet-beta.solana.com", "https://solana-rpc.publicnode.com")
WSS = ("wss://api.mainnet-beta.solana.com", "wss://solana-rpc.publicnode.com")
READ_METHODS = {"getSlot", "getTransaction", "getSignatureStatuses", "getRecentPrioritizationFees",
                "getMinimumBalanceForRentExemption", "getAccountInfo"}
EXPECTED_BLOBS = {"golden_buyer_reputation_core.py": "5029c64fd7fe099f8834b857bde7b0b2c0ac5b56",
                  "golden_management_v2.py": "e67efeb670c00ccc7fcf2bde806a30e2b88f6208"}
DECODER_BLOB = "1eb2befa601acca68bbf5eef67bef22f46843387"


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, sort_keys=True, allow_nan=False)
        f.write("\n"); f.flush(); os.fsync(f.fileno())
    tmp.replace(path)


def load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None: raise RuntimeError("missing module")
    mod = importlib.util.module_from_spec(spec); sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def blob_hash(path: Path) -> str:
    b = path.read_bytes()
    return hashlib.sha1(b"blob " + str(len(b)).encode() + b"\0" + b).hexdigest()


def verify_sources(root: Path, decoder: Path) -> dict[str, str]:
    result = {}
    for name, expected in EXPECTED_BLOBS.items():
        actual = blob_hash(root / name)
        if actual != expected: raise RuntimeError(f"frozen selection/sizing changed: {name}")
        result[name] = actual
    if blob_hash(decoder) != DECODER_BLOB: raise RuntimeError("unreviewed decoder version")
    result["pumpfun.py"] = DECODER_BLOB
    return result


class Journal:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.f = path.open("a", encoding="utf-8", buffering=1)
        self.last = "0" * 64
        self.n = 0

    def add(self, kind: str, data: Any, durable: bool = False) -> None:
        body = json.dumps({"kind": kind, "data": data}, sort_keys=True, separators=(",", ":"), allow_nan=False)
        digest = hashlib.sha256((self.last + body).encode()).hexdigest()
        self.f.write(json.dumps({"seq": self.n, "prev": self.last, "hash": digest, "body": body}) + "\n")
        self.last = digest; self.n += 1
        if durable: self.f.flush(); os.fsync(self.f.fileno())

    def close(self) -> None:
        self.f.flush(); os.fsync(self.f.fileno()); self.f.close()


class Rpc:
    def __init__(self, session: aiohttp.ClientSession, journal: Journal):
        self.session = session; self.journal = journal; self.seq = 0
        self.latencies: list[float] = []; self.errors: list[str] = []

    async def call(self, method: str, params: list[Any], attempts: int = 6) -> Any:
        if method not in READ_METHODS: raise ValueError("RPC method not permitted: read-only experiment")
        last: Exception | None = None
        for i in range(attempts):
            self.seq += 1; url = RPCS[i % len(RPCS)]; start = time.perf_counter_ns()
            try:
                async with self.session.post(url, json={"jsonrpc": "2.0", "id": self.seq,
                     "method": method, "params": params}, timeout=aiohttp.ClientTimeout(total=15)) as r:
                    r.raise_for_status(); d = await r.json()
                elapsed = (time.perf_counter_ns() - start) / 1e6
                if d.get("error"): raise RuntimeError(str(d["error"]))
                self.latencies.append(elapsed)
                self.journal.add("READ_RPC", {"method": method, "params": params, "result": d.get("result"),
                                             "provider": url, "received_ns": time.time_ns(), "rtt_ms": elapsed})
                return d.get("result")
            except Exception as e:
                last = e; self.errors.append(f"{method}:{type(e).__name__}:{str(e)[:140]}")
                await asyncio.sleep(min(3, .25 * (i + 1)))
        raise RuntimeError(f"{method} unavailable: {last}")


def state_from_item(item: dict[str, Any], recv: int, sig: str, slot: int) -> dict[str, Any] | None:
    q = item.get("quote_mint")
    if q not in (None, "", "11111111111111111111111111111111", "So11111111111111111111111111111111111111112"):
        return None
    if item.get("anchor_event") in ("CompleteEvent", "CompletePumpAmmMigrationEvent"):
        return {"ns": recv, "signature": sig, "slot": slot, "complete": True, "vsol": 0.0, "vtok": 0.0}
    # Decoder fields are raw protocol integers. No magnitude-based unit guessing.
    vs = item.get("virtual_sol_reserves", item.get("virtual_quote_reserves"))
    vt = item.get("virtual_token_reserves")
    if not isinstance(vs, int) or not isinstance(vt, int) or vs <= 0 or vt <= 0: return None
    rt = item.get("real_token_reserves")
    return {"ns": recv, "signature": sig, "slot": slot, "complete": False,
            "vsol": vs / 1e9, "vtok": vt / 1e6,
            "rtok": rt / 1e6 if isinstance(rt, int) else None}


async def socket_worker(url: str, decoder: Any, queue: asyncio.Queue, stop: asyncio.Event,
                        stats: dict[str, Any], errors: list[str]) -> None:
    st = stats.setdefault(url, {"connections": 0, "disconnects": 0, "messages": 0, "last_receive_ns": None})
    async with aiohttp.ClientSession() as session:
        while not stop.is_set():
            try:
                async with session.ws_connect(url, heartbeat=10, max_msg_size=8*1024*1024,
                     timeout=aiohttp.ClientWSTimeout(ws_receive=15, ws_close=5)) as ws:
                    st["connections"] += 1
                    await ws.send_json({"jsonrpc": "2.0", "id": 1, "method": "logsSubscribe",
                                        "params": [{"mentions": [decoder.PUMP_PROGRAM_ID]}, {"commitment": "processed"}]})
                    while not stop.is_set():
                        msg = await ws.receive()
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            received = time.time_ns(); st["messages"] += 1; st["last_receive_ns"] = received
                            d = json.loads(msg.data)
                            if d.get("error"): raise RuntimeError(str(d["error"]))
                            if d.get("method") != "logsNotification": continue
                            try: queue.put_nowait((received, url, d))
                            except asyncio.QueueFull:
                                errors.append("FEED_QUEUE_OVERFLOW_DATA_LOSS"); stop.set(); return
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR): break
            except asyncio.CancelledError:
                raise
            except Exception as e:
                st["last_error"] = f"{type(e).__name__}:{str(e)[:140]}"
            st["disconnects"] += 1
            await asyncio.sleep(.5)


async def verify_creation(rpc: Rpc, decoder: Any, decision: dict[str, Any], session_started: int) -> dict[str, Any]:
    sig = decision["create_signature"]
    tx = None
    for _ in range(8):
        await asyncio.sleep(2)
        tx = await rpc.call("getTransaction", [sig, {"encoding": "json", "commitment": "confirmed", "maxSupportedTransactionVersion": 0}])
        if tx: break
    if not tx or (tx.get("meta") or {}).get("err") is not None:
        raise RuntimeError(f"creation not confirmed: {sig}")
    decoded = decoder.anchor_events_from_logs((tx.get("meta") or {}).get("logMessages") or [], decoder.PUMP_PROGRAM_ID)
    found = any(x.get("anchor_event") == "CreateEvent" and x.get("mint") == decision["mint"] for x in decoded)
    bt = tx.get("blockTime")
    if not found or bt is None or bt < int(session_started / 1e9) - 3:
        raise RuntimeError(f"nonfresh or unverified creation: {decision['mint']}")
    return {"verified": True, "mint": decision["mint"], "signature": sig, "slot": tx["slot"],
            "block_time": bt, "verified_at_ns": time.time_ns(), "confirmation": "confirmed",
            "creation_transaction_actual_fee_sol": tx["meta"]["fee"] / 1e9,
            "fee_attribution": "external creator transaction, NOT a Gambit fee"}


async def run(args: argparse.Namespace) -> int:
    out = args.output; out.mkdir(parents=True, exist_ok=True)
    tools = Path(__file__).resolve().parent
    decoder_path = args.production_root / "src/memecoin_bot/realtime/pumpfun.py"
    frozen = verify_sources(tools, decoder_path)
    decoder = load_module("horizon_pump_decoder", decoder_path)
    core = load_module("horizon_frozen_core", tools / "golden_buyer_reputation_core.py")
    mgmt = load_module("horizon_frozen_sizing", tools / "golden_management_v2.py")
    rep = core.initialise_reputation_from_corpus(args.corpus)
    journal = Journal(out / "observations.jsonl")
    resume = json.loads(args.resume.read_text()) if args.resume and args.resume.exists() else None
    manifest = json.loads(args.manifest.read_text())
    manifest_hash = hashlib.sha256(args.manifest.read_bytes()).hexdigest()
    implementation = {name: hashlib.sha256((tools/name).read_bytes()).hexdigest()
                      for name in ("golden_horizon_engine.py", "golden_horizon_live.py")}
    if resume and resume.get("implementation") != implementation:
        raise RuntimeError("trading implementation changed; do not contaminate an active cohort")
    if resume and any(x.get("errors") for x in resume.get("sessions", [])):
        raise RuntimeError("refusing a technically invalid continuation")
    if resume and resume.get("manifest_hash") != manifest_hash: raise RuntimeError("manifest changed mid-campaign")
    delta = copy.deepcopy(resume.get("reputation_delta", {})) if resume else {}
    for w, pair in delta.items(): rep.appear[w] += pair[0]; rep.wins[w] += pair[1]
    queue: asyncio.Queue = asyncio.Queue(maxsize=20000)
    stop = asyncio.Event(); stats: dict[str, Any] = {}; errors: list[str] = []
    counts: Counter = Counter(); launches: dict[str, dict[str, Any]] = {}; states: dict[str, dict[str, Any]] = {}
    seen: dict[str, int] = {}; tasks: set[asyncio.Task] = set(); proofs: dict[str, Any] = {}
    dequeuing: deque[float] = deque(maxlen=10000); computing: list[float] = []
    started_ns = time.time_ns(); last_feed_ns = started_ns; cutoff = False
    session_id = f"{os.getenv('GITHUB_RUN_ID', 'local')}:{args.window}:{started_ns}"
    campaign_id = resume["campaign_id"] if resume else session_id
    deadline = time.monotonic() + args.duration
    drain_deadline: float | None = None
    last_save = 0.0; last_prune = 0.0

    async with aiohttp.ClientSession() as session:
        rpc = Rpc(session, journal)
        rent_lamports = await rpc.call("getMinimumBalanceForRentExemption", [manifest["assumed_token_account_bytes"]])
        priority_sample = await rpc.call("getRecentPrioritizationFees", [])
        current_slot = await rpc.call("getSlot", [{"commitment": "processed"}])
        fee_observation = {"observed_at_ns": time.time_ns(), "rpc_rent_lamports": rent_lamports,
                           "assumed_account_bytes": manifest["assumed_token_account_bytes"],
                           "recent_priority_fee_samples": priority_sample, "current_slot": current_slot}
        write_json(out / "fee-observation.json", fee_observation)
        cfg_values = dict(manifest["execution_model"])
        # Freeze the quoted rent requirement for the campaign, rather than silently
        # changing cost parameters on a continuation window.
        cfg_values["rent_sol"] = resume["engine"]["config"]["rent_sol"] if resume else int(rent_lamports) / 1e9
        c = Config(**cfg_values)
        engine = Engine(c, resume["engine"] if resume else None)
        initial_matched = engine.matched_closed()
        source_sha = os.getenv("GITHUB_SHA", "LOCAL_UNPUBLISHED")
        journal.add("SESSION", {"id": session_id, "source_sha": source_sha, "manifest_hash": manifest_hash,
                                 "frozen_sources": frozen, "config": asdict(c), "fee_observation": fee_observation,
                                 "data_mode": "LIVE_SOLANA_NEW_CREATE_EVENTS", "execution": "PAPER_ONLY"}, durable=True)

        def task(coro: Any) -> None:
            t = asyncio.create_task(coro); tasks.add(t)
            def finished(t: asyncio.Task) -> None:
                tasks.discard(t)
                if not t.cancelled() and t.exception(): errors.append(repr(t.exception()))
            t.add_done_callback(finished)

        async def proof(decision: dict[str, Any]) -> None:
            p = await verify_creation(rpc, decoder, decision, started_ns)
            proofs[decision["mint"]] = p
            if decision["mint"] in engine.bundles: engine.bundles[decision["mint"]]["creation_proof"] = p
            for h in engine.history:
                if h["mint"] == decision["mint"]: h["creation_proof"] = p
            counts["confirmed_creation_proofs"] += 1

        async def settle(mint: str, buyers: list[str], state: dict[str, Any] | None) -> None:
            await asyncio.sleep(core.HOLD_MS / 1000)
            after = states.get(mint)
            if not valid(state) or not valid(after) or time.time_ns() - last_feed_ns > c.feed_stale_ms * 1e6:
                counts["reputation_updates_skipped"] += 1; return
            tok, _ = core.quote_buy(core.BENCHMARK_STAKE_SOL, state)
            if tok <= 0: counts["reputation_updates_skipped"] += 1; return
            won = core.quote_sell(tok, after) > core.BENCHMARK_STAKE_SOL
            rep.update(buyers, won)
            for w in buyers:
                pair = delta.setdefault(w, [0, 0]); pair[0] += 1; pair[1] += int(won)
            counts["causal_reputation_updates"] += 1

        async def decide(mint: str) -> None:
            launch = launches[mint]
            wait = max(0.0, (launch["create_ns"] + core.DECISION_MS * 1_000_000 - time.time_ns()) / 1e9)
            if wait: await asyncio.sleep(wait)
            t0 = time.perf_counter_ns(); now = time.time_ns()
            buyers = core.unique_early_buyers(launch["events"], launch["creator"], launch["create_ns"], core.DECISION_MS)
            qualifies, n, w, rate = rep.qualifies(buyers)
            decision = {"mint": mint, "name": launch["name"], "symbol": launch["symbol"], "creator": launch["creator"],
                        "create_ns": launch["create_ns"], "create_signature": launch["signature"],
                        "create_slot": launch["slot"], "onchain_create_timestamp": launch["timestamp"],
                        "session_id": session_id, "session_start_ns": started_ns,
                        "decision_ns": now, "buyers": buyers, "buyer_count": len(buyers),
                        "prior_appearances": n, "prior_wins": w, "prior_win_rate": rate, "qualifies": qualifies,
                        "axiom_listing_verified": None}
            conv = mgmt.conviction(decision); decision.update(tier=conv["conviction_tier"], conviction=conv)
            computation = (time.perf_counter_ns() - t0) / 1e6; computing.append(computation)
            decision["completed_decision_compute_ms"] = computation
            decision["decision_ns"] = time.time_ns()  # After all feature and sizing computation.
            counts["decisions"] += 1; counts["qualified"] += int(qualifies)
            s = copy.deepcopy(states.get(mint))
            task(settle(mint, buyers, s))
            journal.add("DECISION", decision, durable=qualifies)
            if qualifies and not cutoff:
                if not s or not valid(s): counts["qualified_no_valid_state"] += 1; return
                if time.time_ns() - launch["create_ns"] > c.max_entry_age_ms * 1e6:
                    counts["decision_too_late"] += 1; return
                if time.time_ns() - last_feed_ns > c.feed_stale_ms * 1e6:
                    counts["stale_feed_rejected"] += 1; return
                if engine.submit(decision, s, time.time_ns()):
                    counts["submitted_cohorts"] += 1; task(proof(decision))

        def checkpoint(terminal: str | None = None) -> dict[str, Any]:
            payload = {"version": VERSION, "campaign_id": campaign_id, "session_id": session_id,
                       "phase": 1 if args.smoke else 2, "source_sha": source_sha, "manifest_hash": manifest_hash,
                       "started_ns": started_ns, "checked_ns": time.time_ns(), "counts": dict(counts),
                       "matched_closed": engine.matched_closed(), "target": args.target,
                       "initial_matched": initial_matched, "active_bundles": len(engine.bundles),
                       "queue_depth": queue.qsize(), "endpoints": stats,
                       "errors": list(dict.fromkeys(errors + engine.errors)),
                       "status": terminal or ("DRAINING" if cutoff else "RUNNING"),
                       "market_data": "LIVE_SOLANA_NEWLY_CREATED_NATIVE_SOL_PUMP_COINS",
                       "historical_replay": False, "paper_only": True, "axiom_execution": False,
                       "actual_profit_sol": None, "actual_transaction_failures": None,
                       "completed_decision_compute_ms": quantiles(computing),
                       "socket_to_processing_ms": quantiles(list(dequeuing)),
                       "read_rpc_rtt_ms": quantiles(rpc.latencies), "arms": engine.summary(),
                       "journal_head": journal.last, "source_fingerprint": frozen,
                       "cost_provenance": manifest["cost_provenance"]}
            write_json(out / "status.json", payload)
            state = {"campaign_id": campaign_id, "manifest_hash": manifest_hash, "source_fingerprint": frozen, "implementation": implementation,
                     "source_sha": source_sha, "reputation_delta": delta, "engine": engine.persistence(),
                     "sessions": (resume.get("sessions", []) if resume else []) + [payload]}
            write_json(out / "state.json", state)
            return payload

        workers = [asyncio.create_task(socket_worker(url, decoder, queue, stop, stats, errors)) for url in WSS]
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try: loop.add_signal_handler(sig, stop.set)
            except NotImplementedError: pass
        checkpoint()
        try:
            while not stop.is_set():
                now = time.time_ns()
                if not cutoff and (time.monotonic() >= deadline or engine.matched_closed() >= args.target):
                    cutoff = True; drain_deadline = time.monotonic() + 90
                if cutoff and not engine.bundles and not tasks: break
                if drain_deadline and time.monotonic() > drain_deadline:
                    errors.append("UNRESOLVED_DRAIN_TIMEOUT"); break
                try: recv, provider, payload = await asyncio.wait_for(queue.get(), timeout=.004)
                except asyncio.TimeoutError: payload = None
                if payload:
                    dequeuing.append((time.time_ns() - recv) / 1e6)
                    result = payload["params"]["result"]; value = result["value"]
                    sig = value.get("signature"); slot = int(result["context"]["slot"])
                    last_feed_ns = max(last_feed_ns, recv)
                    if sig and sig not in seen:
                        seen[sig] = slot
                        if value.get("err") is not None:
                            counts["observed_external_failed_transactions"] += 1
                        else:
                            decoded = decoder.anchor_events_from_logs(value.get("logs") or [], decoder.PUMP_PROGRAM_ID)
                            counts["decoded_events"] += len(decoded)
                            for item in decoded:
                                mint = item.get("mint"); kind = item.get("anchor_event")
                                if not mint: continue
                                s = state_from_item(item, recv, sig, slot)
                                if s:
                                    prior = states.get(mint)
                                    if prior is None or slot >= prior["slot"]:
                                        states[mint] = s
                                    else: counts["out_of_order_slots_ignored"] += 1
                                if kind == "CreateEvent" and not cutoff and mint not in launches:
                                    ts = item.get("timestamp")
                                    if not isinstance(ts, int) or ts < started_ns // 1_000_000_000 - 3 or abs(recv/1e9-ts) > 15:
                                        counts["not_fresh_create_rejected"] += 1; continue
                                    if not valid(s): counts["unsupported_quote_create"] += 1; continue
                                    launches[mint] = {"create_ns": recv, "signature": sig, "slot": slot, "timestamp": ts,
                                                      "creator": item.get("creator") or item.get("user") or "",
                                                      "name": item.get("name") or "", "symbol": item.get("symbol") or "", "events": []}
                                    counts["fresh_launches"] += 1
                                    journal.add("FRESH_CREATE", {"provider": provider, "received_ns": recv,
                                                               "signature": sig, "slot": slot, "decoded": item})
                                    task(decide(mint))
                                    if args.smoke and counts["fresh_launches"] <= 3:
                                        task(proof({"mint": mint, "create_signature": sig}))
                                if mint in launches and kind == "TradeEvent":
                                    launches[mint]["events"].append({"kind": "BUY" if item.get("is_buy") else "SELL",
                                                                    "received_ns": recv, "trader": item.get("user")})
                                if mint in engine.bundles:
                                    journal.add("QUALIFIED_COIN_EVENT", {"received_ns": recv, "provider": provider,
                                                                        "signature": sig, "slot": slot, "decoded": item})
                engine.tick(time.time_ns(), states, last_feed_ns)
                if errors or engine.errors: break
                if time.time_ns() - last_feed_ns > 60_000_000_000:
                    errors.append("ALL_FEEDS_SILENT_60S"); break
                if time.monotonic() - last_save >= 30:
                    last_save = time.monotonic(); p = checkpoint()
                    journal.add("CHECKPOINT", {"matched_closed": p["matched_closed"], "counts": dict(counts)}, durable=True)
                    print(json.dumps({"status": p["status"], "matched": p["matched_closed"], "fresh_launches": counts["fresh_launches"]}), flush=True)
                if time.monotonic() - last_prune >= 60:
                    last_prune = time.monotonic(); keep = set(engine.bundles)
                    for mint, launch in list(launches.items()):
                        if time.time_ns() - launch["create_ns"] > 60_000_000_000 and mint not in keep:
                            launches.pop(mint, None); states.pop(mint, None)
                    # State for noncampaign coins is not needed once their causal label matures.
                    for mint, s in list(states.items()):
                        if mint not in launches and mint not in keep: states.pop(mint, None)
                    if seen:
                        top = max(seen.values()); seen = {k:v for k,v in seen.items() if v >= top - 3000}
            if engine.bundles and not errors and not engine.errors: errors.append("INTERRUPTED_WITH_OPEN_OR_POST_EXIT_STATE")
        except Exception as e:
            errors.append(f"{type(e).__name__}:{e}")
        finally:
            stop.set()
            for w in workers: w.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            for t in list(tasks): t.cancel()
            await asyncio.gather(*list(tasks), return_exceptions=True)
            # All retained failures remain recorded; no losing or unresolved trade is deleted.
            terminal = "BLOCKED" if errors or engine.errors else ("TARGET_REACHED" if engine.matched_closed() >= args.target else "CLEAN_WINDOW_END")
            final = checkpoint(terminal)
            try: engine.validate()
            except Exception as e:
                errors.append(str(e)); terminal = "BLOCKED"; final = checkpoint(terminal)
            if terminal != "BLOCKED":
                for h in engine.history:
                    if not (h.get("creation_proof") or {}).get("verified"):
                        errors.append(f"missing creation proof:{h['mint']}")
                if errors: terminal = "BLOCKED"; final = checkpoint(terminal)
            write_json(out / "final.json", final)
            write_json(out / "creation-proofs.json", proofs)
            journal.close()
        if args.smoke:
            if counts["fresh_launches"] < 1 or counts["decoded_events"] < 1 or counts["confirmed_creation_proofs"] < 1:
                raise RuntimeError("live smoke needs observed AND independently confirmed fresh creation")
            if terminal == "BLOCKED": raise RuntimeError("live smoke failed: " + str(errors + engine.errors))
            write_json(out / "smoke-pass.json", {"status": "PASS", "live_data": True,
                       "fresh_launches": counts["fresh_launches"], "confirmed_creations": counts["confirmed_creation_proofs"],
                       "strategy_profitability_pass": False, "synthetic_data": False,
                       "note": "Connectivity/provenance smoke only; zero trades never certify a thesis"})
        return 2 if terminal == "BLOCKED" else 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--production-root", type=Path, required=True)
    p.add_argument("--corpus", type=Path, required=True)
    p.add_argument("--manifest", type=Path, default=Path("config/golden-horizon.json"))
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--resume", type=Path)
    p.add_argument("--window", default="1")
    p.add_argument("--duration", type=int, default=14400)
    p.add_argument("--target", type=int, default=100)
    p.add_argument("--smoke", action="store_true")
    args = p.parse_args()
    if args.target < 100 and not args.smoke: p.error("campaign target cannot be below 100")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
