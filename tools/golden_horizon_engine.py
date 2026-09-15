#!/usr/bin/env python3
"""Deterministic forward-paper accounting; never constructs blockchain orders.

All ticks must be supplied by the live collector. Fixtures belong only in tests.
Live coin prices are observations. Fills, inclusion delays and costs are models.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import statistics
from dataclasses import asdict, dataclass
from typing import Any, Mapping

VERSION = "golden-horizon-cost-v1"
HORIZONS = (2, 3, 4, 5, 7, 10)
ARMS = tuple(f"{family}_{s}S" for family in ("FULL", "PARTIALS") for s in HORIZONS)
TIERS = {"BASE": (0.05, 0.30, 350), "MEDIUM": (0.08, 0.30, 350),
         "HIGH": (0.15, 0.20, 325), "EXCEPTIONAL": (0.25, 0.20, 300)}


@dataclass(frozen=True)
class Config:
    starting_sol: float = 3.0
    protocol_bps: float = 125.0
    platform_bps: float = 100.0
    base_fee_sol: float = 0.000005
    priority_sol: float = 0.001
    tip_sol: float = 0.001
    rent_sol: float = 0.00228288  # Replaced by RPC quote for configured account byte size.
    inclusion_delay_ms: int = 250  # Explicit sensitivity assumption, NOT measured Axiom latency.
    max_slippage_bps: float = 2000.0
    max_attempts: int = 5
    retry_ms: int = 250
    reserve_sol: float = 0.03
    post_seconds: int = 20
    max_concurrent: int = 2
    feed_stale_ms: int = 3000
    max_entry_age_ms: int = 3000

    def __post_init__(self) -> None:
        for k, v in asdict(self).items():
            if not isinstance(v, (float, int)) or not math.isfinite(v) or v < 0:
                raise ValueError(f"invalid config: {k}")
        if self.starting_sol != 3 or self.max_attempts < 1 or self.max_concurrent < 1:
            raise ValueError("invalid bankroll/attempt/concurrency configuration")
        if self.protocol_bps + self.platform_bps >= 10000 or self.max_slippage_bps >= 10000:
            raise ValueError("invalid cost or slippage bounds")

    @property
    def fixed(self) -> float:
        return self.base_fee_sol + self.priority_sol + self.tip_sol

    @property
    def percent(self) -> float:
        return (self.protocol_bps + self.platform_bps) / 10000

    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()


def quantiles(xs: list[float]) -> dict[str, Any]:
    ys = sorted(xs)
    def q(p: float) -> float | None:
        if not ys:
            return None
        x = p * (len(ys) - 1); i = int(x)
        return ys[i] + (ys[min(i + 1, len(ys) - 1)] - ys[i]) * (x - i)
    return {"count": len(ys), "median": q(.5), "p95": q(.95), "p99": q(.99),
            "maximum": max(ys) if ys else None}


def valid(s: Mapping[str, Any] | None) -> bool:
    if not s or s.get("complete"):
        return False
    return all(isinstance(s.get(k), (int, float)) and math.isfinite(s[k]) and s[k] > 0 for k in ("vsol", "vtok"))


def buy_quote(sol: float, s: Mapping[str, Any], ds: float = 0.0, dt: float = 0.0) -> float:
    if not valid(s) or sol <= 0:
        raise ValueError("invalid buy state")
    x, y = s["vsol"] + ds, s["vtok"] + dt
    if x <= 0 or y <= 0:
        raise ValueError("invalid counterfactual reserves")
    return math.floor((sol * y / (x + sol)) * 1e6) / 1e6


def sell_quote(tok: float, s: Mapping[str, Any], ds: float = 0.0, dt: float = 0.0) -> float:
    if not valid(s) or tok < 0:
        raise ValueError("invalid sell state")
    x, y = s["vsol"] + ds, s["vtok"] + dt
    if x <= 0 or y <= 0:
        raise ValueError("invalid counterfactual reserves")
    return math.floor((tok * x / (y + tok)) * 1e9) / 1e9


def fee_breakdown(c: Config, notional: float, failed: bool = False) -> dict[str, float]:
    # Failure class is an ASSUMED on-chain failed transaction. The tip instruction
    # and trade do not commit. Dropped/not-submitted attempts are a separate class.
    return {"network_base_sol": c.base_fee_sol, "priority_sol": c.priority_sol,
            "tip_sol": 0.0 if failed else c.tip_sol,
            "protocol_sol": 0.0 if failed else notional * c.protocol_bps / 10000,
            "platform_sol": 0.0 if failed else notional * c.platform_bps / 10000}


class Engine:
    def __init__(self, config: Config, state: Mapping[str, Any] | None = None):
        self.c = config
        self.accounts = {a: {"cash": config.starting_sol, "rent_locked": 0.0,
                           "peak": config.starting_sol, "max_dd": 0.0,
                           "ledger": [], "failed_entry_fees": 0.0, "aborted_entries": []} for a in ARMS}
        self.bundles: dict[str, dict[str, Any]] = {}
        self.history: list[dict[str, Any]] = []
        self.rejections: list[dict[str, Any]] = []
        self.errors: list[str] = []
        self.latest: dict[str, dict[str, Any]] = {}
        self.last_feed_ns = 0
        self.sequence = 0
        if state:
            if state.get("version") != VERSION or state.get("config_hash") != config.fingerprint():
                raise ValueError("resume version/config mismatch")
            if not state.get("clean") or state.get("bundles"):
                raise ValueError("refusing to erase unresolved inventory on resume")
            self.accounts = copy.deepcopy(state["accounts"])
            self.history = copy.deepcopy(state["history"])
            self.sequence = int(state["sequence"])
            self.rejections = copy.deepcopy(state.get("rejections", []))
            self.errors = list(state.get("errors", []))
            if self.errors:
                raise ValueError("resume contains unresolved errors")
            self.validate()

    def equity(self, arm: str) -> float:
        a = self.accounts[arm]
        value = a["cash"] + a["rent_locked"]
        for b in self.bundles.values():
            p = b["positions"][arm]
            if p["status"] == "OPEN":
                s = self.latest.get(b["mint"])
                if valid(s):
                    value += max(0.0, sell_quote(p["remaining"], s, p["ds"], p["dt"]) * (1 - self.c.percent) - self.c.fixed)
                else:
                    # No fictitious liquidation: conservatively mark inventory zero,
                    # retain tokens and block completion until state is resolved.
                    value += 0.0
        return value

    def update_drawdowns(self) -> None:
        for arm, a in self.accounts.items():
            eq = self.equity(arm)
            a["peak"] = max(a["peak"], eq)
            if a["peak"]:
                a["max_dd"] = max(a["max_dd"], (a["peak"] - eq) / a["peak"])

    def matched_closed(self) -> int:
        sets = [{r["signal_id"] for r in a["ledger"] if r["status"] == "CLOSED"} for a in self.accounts.values()]
        return len(set.intersection(*sets)) if sets else 0

    def accepting_slots(self) -> int:
        return sum(any(p["status"] in ("PENDING", "OPEN") for p in b["positions"].values()) for b in self.bundles.values())

    def submit(self, decision: dict[str, Any], state: dict[str, Any], now: int) -> bool:
        mint = decision["mint"]
        if mint in self.bundles or any(h["mint"] == mint for h in self.history):
            self.rejections.append({"mint": mint, "reason": "DUPLICATE", "ns": now}); return False
        if self.accepting_slots() >= self.c.max_concurrent:
            self.rejections.append({"mint": mint, "reason": "CONCURRENCY", "ns": now}); return False
        if not valid(state) or int(state.get("ns", 0)) > now:
            self.rejections.append({"mint": mint, "reason": "INVALID_OR_FUTURE_STATE", "ns": now}); return False
        if not decision.get("create_signature") or not decision.get("create_ns"):
            raise ValueError("missing real launch provenance")
        if decision["create_ns"] > decision["decision_ns"] or decision["decision_ns"] > now:
            raise ValueError("noncausal decision")
        tier = decision["tier"]; fraction, initial, initial_ms = TIERS[tier]
        plan: dict[str, Any] = {}
        # Pre-submission cash checks are atomic; no transaction or fee is claimed.
        for arm in ARMS:
            a = self.accounts[arm]
            budget = min(self.equity(arm) * fraction, a["cash"] - self.c.reserve_sol)
            curve = (budget - self.c.fixed - self.c.rent_sol) / (1 + self.c.percent)
            if curve <= .00001:
                self.rejections.append({"mint": mint, "reason": "BANKROLL_TOO_LOW", "arm": arm, "ns": now})
                return False
            quoted = buy_quote(curve, state)
            if quoted <= 0 or (state.get("rtok") is not None and quoted > state["rtok"]):
                self.rejections.append({"mint": mint, "reason": "NO_FULL_CURVE_FILL", "ns": now}); return False
            plan[arm] = {"arm": arm, "mint": mint, "status": "PENDING", "tier": tier,
                         "position_fraction": fraction, "horizon_ms": int(arm.split("_")[-1][:-1]) * 1000,
                         "initial_fraction": initial if arm.startswith("PARTIALS") else 0.0,
                         "initial_ms": initial_ms, "initial_done": False, "budget": budget,
                         "curve_sol": curve, "original": 0.0, "remaining": 0.0, "rent": 0.0,
                         "ds": 0.0, "dt": 0.0, "actions": [], "fees": {}, "sell_gross": 0.0,
                         "buy_cost": 0.0, "entry_attempts": 0, "intent": None,
                         "mfe": None, "mae": None, "path": [], "post": None,
                         "equity_before": self.equity(arm),
                         "entry_intent": {"requested_ns": now, "due_ns": now + self.c.inclusion_delay_ms * 1_000_000,
                                          "quoted_output": quoted, "quote_state": dict(state)}}
        self.sequence += 1
        sid = f"{self.sequence}:{mint}"
        for p in plan.values(): p["signal_id"] = sid
        self.latest[mint] = dict(state)
        self.bundles[mint] = {"mint": mint, "signal_id": sid, "decision": copy.deepcopy(decision),
                              "positions": plan, "market_path": [], "creation_proof": None}
        return True

    def _fees(self, p: dict[str, Any], parts: dict[str, float]) -> None:
        for k, v in parts.items(): p["fees"][k] = p["fees"].get(k, 0.0) + v

    def _failure(self, p: dict[str, Any], intent: dict[str, Any], state: dict[str, Any], now: int, side: str) -> None:
        fees = fee_breakdown(self.c, 0, failed=True)
        total = sum(fees.values())
        self.accounts[p["arm"]]["cash"] -= total
        self._fees(p, fees)
        p["actions"].append({"side": side, "outcome": "MODELLED_LANDED_FAILURE", "reason": "SLIPPAGE_LIMIT",
                             "ns": now, "intent_ns": intent["requested_ns"], "fees": fees,
                             "state": dict(state), "actual_transaction_signature": None,
                             "failure_class_assumption": "landed failure; tip instruction rolls back"})

    def _enter(self, p: dict[str, Any], b: dict[str, Any], s: dict[str, Any], now: int) -> None:
        intent = p["entry_intent"]
        if now < intent["due_ns"]: return
        p["entry_attempts"] += 1
        tok = buy_quote(p["curve_sol"], s)
        slip = 1 - tok / intent["quoted_output"]
        if slip > self.c.max_slippage_bps / 10000:
            self._failure(p, intent, s, now, "BUY")
            if p["entry_attempts"] >= self.c.max_attempts or now - b["decision"]["create_ns"] > self.c.max_entry_age_ms * 1e6:
                p["status"] = "NO_ENTRY"
                a = self.accounts[p["arm"]]
                a["failed_entry_fees"] += sum(p["fees"].values())
                a["aborted_entries"].append(copy.deepcopy(p))
                return
            intent.update(requested_ns=now, due_ns=now + self.c.retry_ms * 1_000_000,
                          quoted_output=tok, quote_state=dict(s))
            return
        if s.get("rtok") is not None and tok > s["rtok"]:
            self.errors.append(f"{p['signal_id']}:curve inventory insufficient at fill"); return
        fees = fee_breakdown(self.c, p["curve_sol"])
        cost = p["curve_sol"] + sum(fees.values())
        a = self.accounts[p["arm"]]
        if a["cash"] < cost + self.c.rent_sol:
            self.errors.append(f"{p['signal_id']}:insufficient cash at fill"); return
        a["cash"] -= cost + self.c.rent_sol; a["rent_locked"] += self.c.rent_sol
        p.update(status="OPEN", entry_ns=now, original=tok, remaining=tok, rent=self.c.rent_sol,
                 buy_cost=cost, ds=p["curve_sol"], dt=-tok)
        self._fees(p, fees)
        p["actions"].append({"side": "BUY", "outcome": "MODELLED_FILL", "reason": "FROZEN_GOLDEN_ENTRY",
                             "ns": now, "intent_ns": intent["requested_ns"], "quote_state": intent["quote_state"],
                             "state": dict(s), "tokens": tok, "curve_sol": p["curve_sol"], "fees": fees,
                             "rent_locked_sol": p["rent"], "actual_transaction_signature": None,
                             "adverse_quote_drift_bps": slip * 10000,
                             "price_impact_sol_equivalent": max(0.0, p["curve_sol"] - tok * s["vsol"] / s["vtok"]),
                             "decision_to_modelled_fill_ms": (now - b["decision"]["decision_ns"]) / 1e6})

    def _sell(self, p: dict[str, Any], s: dict[str, Any], now: int) -> None:
        intent = p["intent"]
        if not intent or now < intent["due_ns"]: return
        amount = min(p["remaining"], intent["tokens"])
        gross = sell_quote(amount, s, p["ds"], p["dt"])
        slip = 1 - gross / intent["quoted_output"] if intent["quoted_output"] > 0 else 0.0
        intent["attempts"] += 1
        if slip > self.c.max_slippage_bps / 10000:
            self._failure(p, intent, s, now, "SELL")
            if intent["attempts"] >= self.c.max_attempts:
                self.errors.append(f"{p['signal_id']}:{p['arm']}:UNRESOLVED_EXIT_SLIPPAGE")
                return
            intent.update(requested_ns=now, due_ns=now + self.c.retry_ms * 1_000_000,
                          quoted_output=gross, quote_state=dict(s))
            return
        fees = fee_breakdown(self.c, gross)
        self.accounts[p["arm"]]["cash"] += gross - sum(fees.values())
        self._fees(p, fees); p["sell_gross"] += gross
        # Keep a per-arm reserve overlay so repeated sells cannot reuse pre-sell reserves.
        p["remaining"] = max(0.0, p["remaining"] - amount); p["ds"] -= gross; p["dt"] += amount
        p["actions"].append({"side": "SELL", "outcome": "MODELLED_FILL", "reason": intent["reason"],
                             "ns": now, "intent_ns": intent["requested_ns"], "scheduled_ns": intent["scheduled_ns"],
                             "quote_state": intent["quote_state"], "state": dict(s), "tokens": amount,
                             "fraction_of_original": amount / p["original"], "gross_sol": gross,
                             "net_proceeds_sol": gross - sum(fees.values()), "fees": fees,
                             "remaining_tokens": p["remaining"], "adverse_quote_drift_bps": slip * 10000,
                             "actual_transaction_signature": None,
                             "intent_to_modelled_fill_ms": (now - intent["requested_ns"]) / 1e6,
                             "price_impact_sol_equivalent": max(0.0, amount * (s["vsol"] + p["ds"] + gross) / (s["vtok"] + p["dt"] - amount) - gross)})
        if intent["reason"] == "STRUCTURAL_INITIAL": p["initial_done"] = True
        p["intent"] = None
        if p["remaining"] <= 1e-7:
            p["remaining"] = 0.0
            a = self.accounts[p["arm"]]
            a["cash"] += p["rent"]; a["rent_locked"] -= p["rent"]
            p.update(status="CLOSED", exit_ns=now, exit_reason=intent["reason"],
                     hold_ms=(now - p["entry_ns"]) / 1e6,
                     gross_pnl_sol=p["sell_gross"] - p["curve_sol"])
            p["net_pnl_sol"] = p["gross_pnl_sol"] - sum(p["fees"].values())
            p["net_return"] = p["net_pnl_sol"] / p["buy_cost"]
            p["post"] = {"start_ns": now, "end_ns": now + self.c.post_seconds * 1_000_000_000,
                         "exit_spot": s["vsol"] / s["vtok"], "samples": [], "complete": False,
                         "coverage": "PENDING", "min_spot": None, "max_spot": None}
            a["ledger"].append(p)

    def _mark(self, p: dict[str, Any], s: dict[str, Any], now: int) -> None:
        value = sell_quote(p["remaining"], s, p["ds"], p["dt"])
        net = p["sell_gross"] + value * (1 - self.c.percent) - self.c.fixed - p["curve_sol"] - sum(p["fees"].values())
        ret = net / p["buy_cost"]
        p["mfe"] = ret if p["mfe"] is None else max(p["mfe"], ret)
        p["mae"] = ret if p["mae"] is None else min(p["mae"], ret)
        if not p["path"] or p["path"][-1]["state_ns"] != s["ns"]:
            p["path"].append({"ns": now, "state_ns": s["ns"], "mark_net_return": ret,
                              "remaining_fraction": p["remaining"] / p["original"]})
        if p["intent"]: return
        elapsed_ms = (now - p["entry_ns"]) / 1e6
        if elapsed_ms >= p["horizon_ms"]:
            reason = "HORIZON_FLATTEN"; amount = p["remaining"]
            scheduled = p["entry_ns"] + p["horizon_ms"] * 1_000_000
        elif p["initial_fraction"] and not p["initial_done"] and elapsed_ms >= p["initial_ms"]:
            reason = "STRUCTURAL_INITIAL"; amount = math.floor(p["original"] * p["initial_fraction"] * 1e6) / 1e6
            scheduled = p["entry_ns"] + p["initial_ms"] * 1_000_000
        else: return
        if amount <= 0: raise ValueError("zero action amount")
        p["intent"] = {"tokens": amount, "reason": reason, "attempts": 0, "requested_ns": now,
                       "scheduled_ns": scheduled, "due_ns": now + self.c.inclusion_delay_ms * 1_000_000,
                       "quoted_output": sell_quote(amount, s, p["ds"], p["dt"]), "quote_state": dict(s)}

    def tick(self, now: int, states: Mapping[str, dict[str, Any]], last_feed_ns: int) -> None:
        self.last_feed_ns = last_feed_ns
        healthy = 0 <= now - last_feed_ns <= self.c.feed_stale_ms * 1_000_000
        self.latest = {mint: states.get(mint, self.latest.get(mint)) for mint in self.bundles}
        for mint, b in list(self.bundles.items()):
            s = self.latest.get(mint)
            if not valid(s) or s["ns"] > now:
                if any(p["status"] == "OPEN" for p in b["positions"].values()):
                    if s and s.get("complete"):
                        self.errors.append(f"{b['signal_id']}:UNRESOLVED_MIGRATION_NO_ROUTE")
                if s and s.get("complete") and all(p["status"] in ("CLOSED", "NO_ENTRY") for p in b["positions"].values()):
                    for p in b["positions"].values():
                        if p["status"] == "CLOSED":
                            p["post"]["coverage"] = "MIGRATION_NO_POST_ROUTE_DATA"
                            if now >= p["post"]["end_ns"]:
                                p["post"].update(complete=True, went_higher=None, went_lower=None)
                    if all(p["status"] == "NO_ENTRY" or p["post"]["complete"] for p in b["positions"].values()):
                        self.history.append({"mint": mint, "signal_id": b["signal_id"], "decision": b["decision"],
                                             "creation_proof": b["creation_proof"], "market_path": b["market_path"],
                                             "all_arms_closed": all(p["status"] == "CLOSED" for p in b["positions"].values())})
                        del self.bundles[mint]
                continue
            if not b["market_path"] or b["market_path"][-1]["ns"] != s["ns"]:
                b["market_path"].append(dict(s))
            for p in b["positions"].values():
                if p["status"] == "PENDING" and healthy: self._enter(p, b, s, now)
                if p["status"] == "OPEN" and healthy:
                    self._sell(p, s, now)
                    if p["status"] == "OPEN": self._mark(p, s, now)
                if p["status"] == "CLOSED" and not p["post"]["complete"]:
                    post = p["post"]
                    if not healthy: post["coverage"] = "FEED_GAP"
                    if post["start_ns"] < s["ns"] <= post["end_ns"]:
                        if not post["samples"] or post["samples"][-1]["ns"] != s["ns"]:
                            price = s["vsol"] / s["vtok"]
                            post["samples"].append({"ns": s["ns"], "spot": price, "signature": s.get("signature")})
                            post["min_spot"] = price if post["min_spot"] is None else min(post["min_spot"], price)
                            post["max_spot"] = price if post["max_spot"] is None else max(post["max_spot"], price)
                    if now >= post["end_ns"]:
                        post["complete"] = True
                        if post["coverage"] != "FEED_GAP":
                            post["coverage"] = "OBSERVED" if post["samples"] else "NO_FURTHER_TICKS_OBSERVED"
                        post["went_higher"] = post["max_spot"] > post["exit_spot"] if post["max_spot"] is not None else None
                        post["went_lower"] = post["min_spot"] < post["exit_spot"] if post["min_spot"] is not None else None
            if all(p["status"] == "NO_ENTRY" or (p["status"] == "CLOSED" and p["post"]["complete"]) for p in b["positions"].values()):
                self.history.append({"mint": mint, "signal_id": b["signal_id"], "decision": b["decision"],
                                     "creation_proof": b["creation_proof"], "market_path": b["market_path"],
                                     "all_arms_closed": all(p["status"] == "CLOSED" for p in b["positions"].values())})
                del self.bundles[mint]
        self.update_drawdowns()
        if any(a["cash"] < -1e-8 for a in self.accounts.values()):
            self.errors.append("INSOLVENT_MODELLED_ACCOUNT")
        self.errors = list(dict.fromkeys(self.errors))

    def persistence(self) -> dict[str, Any]:
        return {"version": VERSION, "config": asdict(self.c), "config_hash": self.c.fingerprint(),
                "paper_only": True, "real_execution": False, "actual_profit_sol": None,
                "actual_failed_transactions": None, "clean": not self.bundles and not self.errors,
                "sequence": self.sequence, "accounts": self.accounts, "history": self.history,
                "bundles": self.bundles, "errors": self.errors, "rejections": self.rejections}

    def validate(self) -> None:
        if set(self.accounts) != set(ARMS): raise ValueError("missing/extra arms")
        for arm, a in self.accounts.items():
            ids = [p["signal_id"] for p in a["ledger"]]
            if len(ids) != len(set(ids)): raise ValueError("duplicate ledger entry")
            for p in a["ledger"]:
                if p["status"] != "CLOSED" or p["remaining"] != 0: raise ValueError("unclosed ledger")
                actual = p["sell_gross"] - p["curve_sol"] - sum(p["fees"].values())
                if not math.isclose(actual, p["net_pnl_sol"], abs_tol=1e-8): raise ValueError("trade PnL mismatch")
            if not self.bundles:
                expected = self.c.starting_sol + sum(p["net_pnl_sol"] for p in a["ledger"]) - a["failed_entry_fees"]
                if not math.isclose(a["cash"], expected, abs_tol=1e-7): raise ValueError(f"cash reconciliation:{arm}")
                if abs(a["rent_locked"]) > 1e-7: raise ValueError("unresolved rent")

    def summary(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for arm, a in self.accounts.items():
            rows = a["ledger"]; xs = [p["net_pnl_sol"] for p in rows]
            w = [x for x in xs if x > 0]; l = [x for x in xs if x < 0]
            fees: dict[str, float] = {}
            for p in rows + a["aborted_entries"]:
                for k, v in p["fees"].items(): fees[k] = fees.get(k, 0) + v
            actions = [act for p in rows + a["aborted_entries"] for act in p["actions"]]
            out[arm] = {"closed_trades": len(xs), "wins": len(w), "losses": len(l), "breakeven": len(xs)-len(w)-len(l),
                        "win_rate": len(w)/len(xs) if xs else None,
                        "gross_pnl_sol": sum(p["gross_pnl_sol"] for p in rows), "total_modelled_fees_sol": sum(fees.values()),
                        "fees_by_category": fees, "net_pnl_sol": sum(xs)-a["failed_entry_fees"],
                        "cash_sol": a["cash"], "equity_sol": self.equity(arm), "rent_locked_sol": a["rent_locked"],
                        "profit_factor_closed_trades": sum(w)/-sum(l) if l else None,
                        "profit_factor_including_aborted_entry_fees": sum(w)/(-sum(l)+a["failed_entry_fees"]) if -sum(l)+a["failed_entry_fees"] > 0 else None,
                        "maximum_mark_to_market_drawdown": a["max_dd"],
                        "average_winner_sol": statistics.mean(w) if w else None,
                        "average_loser_sol": statistics.mean(l) if l else None,
                        "largest_winner_share": max(w)/sum(w) if w else None,
                        "aborted_entries": len(a["aborted_entries"]),
                        "modelled_landed_failures": sum(x["outcome"] == "MODELLED_LANDED_FAILURE" for x in actions),
                        "actual_failed_transactions": None, "actual_fees_sol": None, "actual_profit_sol": None,
                        "entry_timing_ms": quantiles([x["decision_to_modelled_fill_ms"] for x in actions if "decision_to_modelled_fill_ms" in x]),
                        "exit_timing_ms": quantiles([x["intent_to_modelled_fill_ms"] for x in actions if "intent_to_modelled_fill_ms" in x])}
        return out
