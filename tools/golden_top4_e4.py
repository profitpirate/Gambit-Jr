"""Read-only confirmed E4 wallet audit. Never feeds the Golden selector.

Network fees are taken from Solana transaction metadata, including failed TXs.
Wallet cashflow already contains fees: displayed fees must NOT be subtracted
again. Ambiguous swaps and opening inventory have unknown position profit.
"""
from __future__ import annotations
import asyncio
import copy
import json
import os
import time
from collections import Counter
from pathlib import Path
from golden_top4_engine import write_json

WALLET = "E4EzXdwf7NNdqM2XGswWaWHfxgucVCo24PTCcrimTKBz"
WSOL = "So11111111111111111111111111111111111111112"

def parse_transaction(tx, signature, decoder):
    meta = tx.get("meta") or {}
    keys = tx["transaction"]["message"]["accountKeys"]
    keys = [k.get("pubkey") if isinstance(k, dict) else k for k in keys]
    loaded = meta.get("loadedAddresses") or {}
    keys += loaded.get("writable", []) + loaded.get("readonly", [])
    if WALLET not in keys:
        raise ValueError("E4 not in transaction account keys")
    idx = keys.index(WALLET)
    balances = {}
    for phase in ("pre", "post"):
        vals = Counter(); decimals = {}
        for b in meta.get(phase+"TokenBalances", []):
            if b.get("owner") != WALLET:
                continue
            mint = b["mint"]; a = b["uiTokenAmount"]
            vals[mint] += int(a["amount"]); decimals[mint] = int(a["decimals"])
        balances[phase] = (vals, decimals)
    pre, post = balances["pre"][0], balances["post"][0]
    decs = {**balances["pre"][1], **balances["post"][1]}
    deltas = {m: {"pre_raw": pre[m], "post_raw": post[m], "delta_raw": post[m]-pre[m],
                  "decimals": decs[m]} for m in set(pre)|set(post) if post[m] != pre[m]}
    native = (meta["postBalances"][idx]-meta["preBalances"][idx])/1e9
    wrapped = deltas.get(WSOL, {}).get("delta_raw", 0)/1e9
    failed = meta.get("err") is not None
    events = decoder.anchor_events_from_logs(meta.get("logMessages") or [], decoder.PUMP_PROGRAM_ID)
    pump = [x for x in events if x.get("anchor_event") == "TradeEvent" and x.get("user") == WALLET]
    assets = {m:d for m,d in deltas.items() if m != WSOL}
    kind = "FAILED_TRANSACTION" if failed else "UNCLASSIFIED_WALLET_ACTIVITY"
    mint = None
    if not failed and len(assets) == 1:
        mint, change = next(iter(assets.items()))
        relevant = [x for x in pump if x.get("mint") == mint]
        if relevant:
            kind = "TRADE_BUY" if change["delta_raw"]>0 else "TRADE_SELL"
    return {"signature": signature, "slot": tx["slot"], "block_time": tx.get("blockTime"),
        "kind": kind, "mint": mint, "chain_error": meta.get("err"), "confirmed": True,
        "observed_at_ns": time.time_ns(), "wallet": WALLET,
        "wallet_native_delta_sol": native, "wallet_wrapped_delta_sol": wrapped,
        "wallet_liquid_cashflow_sol": native+wrapped,
        "actual_chain_fee_paid_by_e4_sol": meta.get("fee", 0)/1e9 if keys[0] == WALLET else 0.0,
        "actual_transaction_chain_fee_sol": meta.get("fee", 0)/1e9,
        "fee_payer": keys[0], "token_deltas": deltas,
        "fees_already_in_wallet_delta": True,
        "actual_tip_and_platform_fee_breakdown": None,
        "position_profit_sol": None,
        "note": "Cashflow is observed; transfers, opening inventory and rent prevent equating all cashflow to profit"}


def cycle_audit(rows):
    """Conservative single-native-Pump-asset cycles; never whole-wallet WR."""
    opened = {}; closed = []; unresolved = []
    for row in rows:
        if row['kind'] not in ('TRADE_BUY', 'TRADE_SELL'):
            continue
        mint = row['mint']; change = row['token_deltas'][mint]
        p = opened.get(mint)
        if p is None:
            p = {'mint': mint, 'basis_known': row['kind']=='TRADE_BUY' and change['pre_raw']==0 and not row.get('start_boundary_second_ambiguous'),
                 'cashflow_sol':0.0, 'chain_fees_sol':0.0, 'tokens_raw':change['pre_raw'],
                 'signatures':[], 'buys':0, 'sells':0}
            opened[mint] = p
        if p['tokens_raw'] != change['pre_raw']:
            p['basis_known'] = False
        p['cashflow_sol'] += row['wallet_liquid_cashflow_sol']
        p['chain_fees_sol'] += row['actual_chain_fee_paid_by_e4_sol']
        p['tokens_raw'] = change['post_raw']; p['signatures'].append(row['signature'])
        p['buys' if row['kind']=='TRADE_BUY' else 'sells'] += 1
        if change['post_raw']==0:
            (closed if p['basis_known'] else unresolved).append(p)
            del opened[mint]
    wins=sum(p['cashflow_sol']>0 for p in closed)
    losses=sum(p['cashflow_sol']<0 for p in closed)
    failed=sum(r['actual_chain_fee_paid_by_e4_sol'] for r in rows if r['kind']=='FAILED_TRANSACTION')
    return {'scope':'COMPLETE_IN_WINDOW_SINGLE_NATIVE_PUMP_ASSET_CYCLES_ONLY',
            'completed_cycles':closed, 'closed_cycle_count':len(closed), 'wins':wins,'losses':losses,
            'closed_cycle_win_rate':wins/len(closed) if closed else None,
            'closed_cycle_net_cashflow_sol':sum(p['cashflow_sol'] for p in closed),
            'separate_failed_transaction_fees_sol':failed,
            'closed_cycles_minus_all_failed_fees_sol':sum(p['cashflow_sol'] for p in closed)-failed,
            'open_cycles':list(opened.values()), 'unknown_basis_closed_cycles':unresolved,
            'not_complete_whole_wallet_trading_pnl':True,
            'note':'Successful-cycle cashflows already contain fees and any same-TX rent. Separate failed fees are deducted once; ambiguous/open/non-Pump activity remains outside this subset.'}

class Observer:
    def __init__(self, rpc, decoder, out: Path, started_ns: int, previous=None):
        self.rpc = rpc; self.decoder = decoder; self.out = out
        self.started_ns = started_ns; self.cursor = None
        self.rows = []; self.missing = {}; self.errors = []
        self.prior_missing = copy.deepcopy(previous.get("missing_transactions", {})) if previous else {}
        self.checked_ns = 0; self.complete_pages = False
        self.closed_cycles = []; self.open_cycles = {}; self.unattributed = []
        self.positions_seen = set()
        self.sessions = [] if previous is None else copy.deepcopy(previous.get("sessions", []))
        self.prior_rows = [] if previous is None else copy.deepcopy(previous.get("rows", []))
        self.seen = {r["signature"] for r in self.prior_rows}

    async def poll(self, ending_ns=None):
        page = []; before = None; reached = False
        for _ in range(100):
            opts = {"limit": 100, "commitment": "confirmed"}
            if before: opts["before"] = before
            rows = await self.rpc.call("getSignaturesForAddress", [WALLET, opts])
            if not rows:
                reached = True; break
            for row in rows:
                if row["signature"] == self.cursor:
                    reached = True; break
                bt = row.get("blockTime")
                if bt is not None and bt < self.started_ns//1_000_000_000:
                    reached = True; break
                if ending_ns is None or bt is None or bt <= ending_ns//1_000_000_000:
                    page.append(row)
            if reached: break
            before = rows[-1]["signature"]
        if not reached:
            raise RuntimeError("E4 pagination cap; coverage cannot be called complete")
        for sigrow in reversed(page):
            sig = sigrow["signature"]
            if sig in self.seen: continue
            tx = await self.rpc.call("getTransaction", [sig, {"encoding":"json", "commitment":"confirmed", "maxSupportedTransactionVersion":1}])
            if not tx:
                self.missing[sig] = "CONFIRMED_TRANSACTION_NOT_YET_AVAILABLE"
                continue
            self.missing.pop(sig, None)
            row = parse_transaction(tx, sig, self.decoder)
            row["shared_market_session_start_ns"] = self.started_ns
            row["start_boundary_second_ambiguous"] = tx.get("blockTime") == self.started_ns//1_000_000_000
            self.rows.append(row); self.seen.add(sig)
            with (self.out/"e4-raw-transactions.jsonl").open("a") as f:
                f.write(json.dumps({"signature": sig, "transaction": tx}, allow_nan=False)+"\n")
                f.flush(); os.fsync(f.fileno())
            print("E4_OBSERVED " + json.dumps(row, allow_nan=False), flush=True)
            self.save()
        if page and not self.missing:
            self.cursor = page[0]["signature"]
        self.checked_ns = time.time_ns(); self.complete_pages = not self.missing
        self.save()

    def save(self):
        all_rows = self.prior_rows + self.rows
        counts = Counter(x["kind"] for x in all_rows)
        fees = sum(x["actual_chain_fee_paid_by_e4_sol"] for x in all_rows)
        failed_fees = sum(x["actual_chain_fee_paid_by_e4_sol"] for x in all_rows if x["kind"] == "FAILED_TRANSACTION")
        self.out.mkdir(parents=True, exist_ok=True)
        data = {"wallet": WALLET, "execution": "EXTERNAL_OBSERVED_ONCHAIN_NOT_GAMBIT", "checked_ns": self.checked_ns,
            "session_start_ns": self.started_ns, "rows": all_rows, "counts": dict(counts),
            "actual_chain_fees_paid_sol": fees, "actual_failed_transaction_fees_sol": failed_fees,
            "observed_wallet_liquid_cashflow_sol": sum(x["wallet_liquid_cashflow_sol"] for x in all_rows),
            "audited_position_win_rate": None, "audited_realized_trading_profit_sol": None,
            "single_asset_pump_cycle_audit": cycle_audit(all_rows),
            "previous_window_unresolved_transactions": self.prior_missing,
            "missing_transactions": self.missing, "current_pagination_complete": self.complete_pages,
            "errors": self.errors, "sessions": self.sessions + [{"start_ns":self.started_ns, "checked_ns":self.checked_ns}],
            "selection_input": False,
            "limitations": ["Whole-wallet cashflow is not automatically trading profit.",
                "Final cycle/cost-basis reconciliation is required before claiming whole-wallet E4 win rate or realized trading PnL.",
                "Confirmed-poll latency is not E4 decision or execution latency.",
                "Token symbols and Axiom listings are not verified by this monitor.",
                "Observed network fee includes base and priority; unavailable tip/platform breakdown is not invented."]}
        write_json(self.out/"e4-status.json", data)
        return data

    async def run(self, stop: asyncio.Event):
        self.out.mkdir(parents=True, exist_ok=True)
        while not stop.is_set():
            try:
                await self.poll()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.errors.append({"at_ns":time.time_ns(), "error":type(exc).__name__+":"+str(exc)[:200]})
                self.complete_pages = False; self.save()
            try: await asyncio.wait_for(stop.wait(), timeout=5)
            except asyncio.TimeoutError: pass
        return self.save()
