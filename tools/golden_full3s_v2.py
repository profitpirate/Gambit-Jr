"""FULL_3S_V2 offline-build runtime. No connector, signer or broadcast methods.

The original twelve-arm engine file is byte-for-byte preserved. A private module
instance exposes its unchanged accounting to ONE arm, not twelve. Both the v2
account and its learning observer use identical costs, 3s exit and 250ms modeled
inclusion delay. Local processing latency is never called chain execution speed.
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import math
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Mapping

from golden_full3s_policy import EvidenceIndex, Outcome, Selector, PolicyConfig, clean_buyers, finite

BASELINE_GIT_BLOB = "475087142929dd4cbb404f5c4ca2898944435f81"
BASELINE_COMMIT = "03dd2d24cc18e6152f76db6b7617a9bc821f061c"
MODEL = "FULL_3S_V2"
ARM = "FULL_3S"


def _load_base():
    path = Path(__file__).with_name("golden_horizon_engine.py")
    raw = path.read_bytes()
    blob = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
    if blob != BASELINE_GIT_BLOB:
        raise RuntimeError("unfrozen baseline accounting source")
    name = "_full3s_private_accounting"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    # This module object is private. golden_horizon_engine.ARMS remains unchanged.
    module.ARMS = (ARM,)
    return module


base = _load_base()
Config = base.Config
IMPLEMENTATION_DIGEST = hashlib.sha256(b"".join(
    name.encode() + hashlib.sha256(Path(__file__).with_name(name).read_bytes()).digest()
    for name in ("golden_full3s_v2.py", "golden_full3s_policy.py", "golden_full3s_store.py",
                 "golden_full3s_service.py", "golden_full3s_diagnostics.py", "golden_horizon_engine.py")
)).hexdigest()


class MonotonicClock:
    """Epoch-shaped event clock anchored once to a monotonic clock.

    On machine reboot the adapter must start a new epoch and mark open research
    positions interrupted; it may not reconstruct missing prices as live data.
    """
    def __init__(self):
        self.wall = time.time_ns(); self.mono = time.monotonic_ns()

    def now_ns(self) -> int:
        return self.wall + time.monotonic_ns() - self.mono


class SingleEngine(base.Engine):
    def __init__(self, config: Config | None = None, state: Mapping[str, Any] | None = None, *, quote_age_ms: int = 500):
        config = config or Config()
        super().__init__(config)
        self.quote_age_ms = quote_age_ms
        self.last_now = 0
        self.seen_mints: set[str] = set()
        self.uncertain_mints: set[str] = set()
        self.input_rejections = 0
        self.accepting = True
        self._view: dict[str, dict[str, Any]] = {}
        if state:
            if state["config_hash"] != config.fingerprint(): raise ValueError("changed cost model on restart")
            raw = state["engine"]
            self.accounts = copy.deepcopy(raw["accounts"])
            self.bundles = copy.deepcopy(raw["bundles"])
            # JSON cannot preserve object aliases. A closed position may still
            # be in the post-exit bundle AND the ledger; reconnect both views.
            ledger_index = {p["signal_id"]: p for p in self.accounts[ARM]["ledger"]}
            for bundle in self.bundles.values():
                p = bundle["positions"][ARM]
                if p["status"] == "CLOSED":
                    if p["signal_id"] not in ledger_index:
                        raise ValueError("closed bundle missing ledger position")
                    bundle["positions"][ARM] = ledger_index[p["signal_id"]]
            self.history = copy.deepcopy(raw["history"])
            self.errors = list(raw["errors"]); self.rejections = copy.deepcopy(raw["rejections"])
            self.sequence = raw["sequence"]
            self.latest = copy.deepcopy(state["latest"])
            self.last_now = state["last_now"]
            self.seen_mints = set(state["seen_mints"])
            self.uncertain_mints = set(state["uncertain_mints"])
            self.accepting = state["accepting"]
            self.input_rejections = state["input_rejections"]
            self._view = dict(self.latest)
            self.validate()

    def matched_closed(self) -> int:
        return len(self.accounts[ARM]["ledger"])

    def forward_count(self) -> int:
        return sum(p["mint"] not in self.uncertain_mints and
                   bool(p.get("post", {}).get("complete")) and p.get("post", {}).get("coverage") == "OBSERVED"
                   for p in self.accounts[ARM]["ledger"])

    def reject(self, mint: str, why: str, now: int) -> bool:
        self.rejections.append({"mint": mint, "reason": why, "ns": now})
        return False

    def submit(self, decision: dict[str, Any], state: dict[str, Any], now: int) -> bool:
        mint = decision["mint"]
        if not self.accepting or self.errors: return self.reject(mint, "ENTRY_GATE_CLOSED", now)
        if mint in self.seen_mints: return self.reject(mint, "DUPLICATE", now)
        if not isinstance(now, int) or now < self.last_now: return self.reject(mint, "CLOCK_REGRESSION", now)
        if not valid_quote(state, now, self.quote_age_ms): return self.reject(mint, "INVALID_OR_STALE_COIN_QUOTE", now)
        if now - decision["create_ns"] > self.c.max_entry_age_ms * 1_000_000:
            return self.reject(mint, "ENTRY_TOO_OLD", now)
        if decision.get("tier") not in base.TIERS: return self.reject(mint, "INVALID_TIER", now)
        if self.accepting_slots() >= self.c.max_concurrent: return self.reject(mint, "CONCURRENCY", now)
        a = self.accounts[ARM]
        # Reserve other pending budgets, not merely already-debited cash.
        reserved = sum(b["positions"][ARM]["budget"] for b in self.bundles.values() if b["positions"][ARM]["status"] == "PENDING")
        prospective = min(self.equity(ARM)*base.TIERS[decision["tier"]][0], a["cash"]-self.c.reserve_sol)
        fee_headroom = self.c.max_attempts * (self.c.base_fee_sol + self.c.priority_sol)
        if a["cash"] - reserved - prospective < self.c.reserve_sol + fee_headroom:
            return self.reject(mint, "PENDING_BUDGET_OR_RETRY_RESERVE", now)
        ok = super().submit(decision, state, now)
        if ok:
            self.seen_mints.add(mint); self._view[mint] = dict(state)
        return ok

    def tick(self, now: int, states: Mapping[str, dict[str, Any]], last_feed_ns: int) -> None:
        if not isinstance(now, int) or now < self.last_now:
            self.accepting = False
            self.errors = list(dict.fromkeys(self.errors + ["CLOCK_REGRESSION"]))
            return
        self.last_now = now
        effective: dict[str, Any] = {}
        for mint, bundle in list(self.bundles.items()):
            previous = self._view.get(mint)
            candidate = states.get(mint, previous)
            if candidate is not None:
                if not isinstance(candidate.get("ns"), int) or candidate["ns"] > now:
                    self.input_rejections += 1; candidate = previous
                elif previous and (candidate["ns"] < previous["ns"] or candidate.get("slot", 0) < previous.get("slot", 0)):
                    self.input_rejections += 1; candidate = previous
                elif not candidate.get("complete") and not base.valid(candidate):
                    self.input_rejections += 1; candidate = previous
            if candidate is not None: self._view[mint] = dict(candidate)
            p = bundle["positions"][ARM]
            alive = p["status"] in ("OPEN", "PENDING")
            fresh = valid_quote(candidate, now, self.quote_age_ms)
            healthy = 0 <= now-last_feed_ns <= self.c.feed_stale_ms*1_000_000
            if alive and (not fresh or not healthy) and not (candidate and candidate.get("complete")):
                self.uncertain_mints.add(mint)
                if p["status"] == "PENDING" and now-bundle["decision"]["create_ns"] > self.c.max_entry_age_ms*1_000_000:
                    p["status"] = "NO_ENTRY"
                    p["abort_reason"] = "NO_FRESH_EXECUTABLE_STATE_BEFORE_ENTRY_EXPIRY"
                    a = self.accounts[ARM]
                    a["failed_entry_fees"] += sum(p["fees"].values())
                    a["aborted_entries"].append(copy.deepcopy(p))
                    self.history.append({"mint": mint, "signal_id": p["signal_id"], "decision": bundle["decision"], "market_path": bundle["market_path"], "all_arms_closed": False, "creation_proof": bundle.get("creation_proof")})
                    del self.bundles[mint]; self._view.pop(mint, None)
                    continue
                effective[mint] = None
            else:
                effective[mint] = candidate
        super().tick(now, effective, last_feed_ns)
        for mint in list(self._view):
            if mint not in self.bundles: self._view.pop(mint, None)
        if self.errors: self.accepting = False

    def mark_interrupted(self) -> None:
        if self.bundles:
            self.uncertain_mints.update(self.bundles)
            self.accepting = False

    def snapshot(self) -> dict[str, Any]:
        return {"config_hash": self.c.fingerprint(), "engine": super().persistence(),
                "latest": self._view, "last_now": self.last_now, "seen_mints": sorted(self.seen_mints),
                "uncertain_mints": sorted(self.uncertain_mints), "input_rejections": self.input_rejections,
                "accepting": self.accepting}


def valid_quote(s: Mapping[str, Any] | None, now: int, age_ms: int) -> bool:
    return bool(s and base.valid(s) and isinstance(s.get("ns"), int) and not isinstance(s["ns"], bool) and
                0 <= now-s["ns"] <= age_ms*1_000_000 and
                all(finite(s[k]) for k in ("vsol", "vtok")) and
                (s.get("rtok") is None or finite(s["rtok"]) and s["rtok"] >= 0))


def quote_metrics(c: Config, state: dict[str, Any], budget: float) -> dict[str, Any]:
    curve = (budget-c.fixed-c.rent_sol)/(1+c.percent)
    if not base.valid(state) or curve <= 0:
        return {"supported": False, "ns": state.get("ns", 0)}
    tok = base.buy_quote(curve, state)
    spot_value = tok*state["vsol"]/state["vtok"]
    gross = base.sell_quote(tok, state, curve, -tok)
    drag = 1 - (gross*(1-c.percent)-c.fixed)/(curve*(1+c.percent)+c.fixed)
    return {"supported": tok > 0 and (state.get("rtok") is None or tok <= state["rtok"]),
            "ns": state["ns"], "impact_bps": max(0.0, (1-spot_value/curve)*10000),
            "roundtrip_drag_bps": max(0.0, drag*10000)}


class Runtime:
    """Two isolated paper engines: candidate and unfiltered label observer.

    There is no run-live command. Callers must explicitly supply data. The
    observer labels rejected as well as accepted Golden signals when capacity
    permits; missing counterfactual outcomes are UNKNOWN, never assumed wins.
    """
    def __init__(self, config: Config | None = None, policy: PolicyConfig | None = None, *, fixture_mode: bool = False):
        self.c = config or Config(); self.pc = policy or PolicyConfig()
        self.evidence = EvidenceIndex(self.c.fingerprint(), self.pc, fixture_mode=fixture_mode)
        self.selector = Selector(self.evidence)
        self.candidate = SingleEngine(self.c, quote_age_ms=self.pc.max_quote_age_ms)
        self.observer = SingleEngine(self.c, quote_age_ms=self.pc.max_quote_age_ms)
        self.decisions: dict[str, Any] = {}
        self.evaluations: dict[str, Any] = {}
        self.labelled: set[str] = set()
        self.label_cursor = 0
        self.next_prune_ns = 0
        self.last_ns = 0
        self.fixture_mode = fixture_mode
        self.halt_new_entries = False
        self.interrupted = False

    def submit(self, d: dict[str, Any], s: dict[str, Any], now: int, *, queue_depth: int = 0) -> dict[str, Any]:
        mint = d["mint"]
        if mint in self.decisions:
            return {"accept": False, "vetoes": ["DUPLICATE_DECISION"]}
        if now < self.last_ns or queue_depth > 1024 or self.halt_new_entries or self.interrupted:
            self.halt_new_entries = True
            return {"accept": False, "vetoes": ["OPERATIONS_ENTRY_HALT"]}
        fraction = base.TIERS.get(d.get("tier"), (0,))[0]
        budget = min(self.candidate.equity(ARM)*fraction, self.candidate.accounts[ARM]["cash"]-self.c.reserve_sol)
        metrics = quote_metrics(self.c, s, budget)
        result = self.selector.evaluate(d, metrics, budget, now)
        self.decisions[mint] = copy.deepcopy(d)
        self.evaluations[mint] = result
        if result["baseline_accept"]:
            result["observer_submitted"] = self.observer.submit(d, s, now)
        else: result["observer_submitted"] = False
        result["candidate_submitted"] = self.candidate.submit(d, s, now) if result["accept"] else False
        if result["accept"] and not result["candidate_submitted"]:
            result["operations_rejection"] = self.candidate.rejections[-1]["reason"]
        return result

    def tick(self, now: int, states: Mapping[str, dict[str, Any]], last_feed_ns: int) -> None:
        if now < self.last_ns:
            self.halt_new_entries = True
            raise ValueError("runtime clock regression")
        self.last_ns = now
        # Existing inventory is managed before learning/index work; vetoes never
        # stop the exit engine. Selection does not inspect future post-exit data.
        self.candidate.tick(now, states, last_feed_ns)
        self.observer.tick(now, states, last_feed_ns)
        rows = self.observer.accounts[ARM]["ledger"]
        fresh_rows = rows[self.label_cursor:]
        self.label_cursor = len(rows)
        for p in fresh_rows:
            if p["mint"] in self.labelled: continue
            self.labelled.add(p["mint"])
            if p["mint"] in self.observer.uncertain_mints: continue
            d = self.decisions[p["mint"]]
            buyers = clean_buyers(d)
            if not buyers: continue
            self.evidence.add(Outcome(mint=p["mint"], buyers=buyers, creator=str(d.get("creator", "")),
                decision_ns=d["decision_ns"], closed_ns=p["exit_ns"], available_ns=now,
                budget_sol=p["budget"], net_pnl_sol=p["net_pnl_sol"], gross_pnl_sol=p["gross_pnl_sol"],
                cost_hash=self.c.fingerprint(), source="SYNTHETIC_FIXTURE" if self.fixture_mode else "FORWARD_OBSERVED_PAPER"))
        if now >= self.next_prune_ns:
            self.evidence.prune(now)
            self.next_prune_ns = now + 3_600_000_000_000
        if self.candidate.errors or self.observer.errors: self.halt_new_entries = True

    def summary(self) -> dict[str, Any]:
        return {"model": MODEL, "execution": "PAPER_ONLY", "live_test_started": False,
                "fixture_mode": self.fixture_mode, "actual_profit_sol": None, "actual_failed_transactions": None,
                "candidate": self.candidate.summary()[ARM], "observer": self.observer.summary()[ARM],
                "candidate_fully_observed_closed": 0 if self.fixture_mode else self.candidate.forward_count(),
                "completed_real_axiom_trades": 0, "interrupted": self.interrupted,
                "halt_new_entries": self.halt_new_entries, "cost_hash": self.c.fingerprint(),
                "policy_hash": self.pc.digest(), "live_profitability_proven": False}

    def snapshot(self) -> dict[str, Any]:
        return copy.deepcopy({"model": MODEL, "implementation_digest": IMPLEMENTATION_DIGEST, "config": asdict(self.c), "policy": asdict(self.pc),
            "fixture_mode": self.fixture_mode, "candidate": self.candidate.snapshot(), "observer": self.observer.snapshot(),
            "evidence": self.evidence.snapshot(), "decisions": self.decisions, "evaluations": self.evaluations,
            "labelled": sorted(self.labelled), "last_ns": self.last_ns,
            "label_cursor": self.label_cursor, "next_prune_ns": self.next_prune_ns,
            "halt_new_entries": self.halt_new_entries, "interrupted": self.interrupted})

    @classmethod
    def restore(cls, data: Mapping[str, Any], *, process_restart: bool = True) -> Runtime:
        if data["model"] != MODEL: raise ValueError("wrong model snapshot")
        if data.get("implementation_digest") != IMPLEMENTATION_DIGEST:
            raise ValueError("changed implementation on resume; explicit migration required")
        r = cls(Config(**data["config"]), PolicyConfig(**data["policy"]), fixture_mode=data["fixture_mode"])
        r.evidence = EvidenceIndex.restore(data["evidence"]); r.selector = Selector(r.evidence)
        r.candidate = SingleEngine(r.c, data["candidate"], quote_age_ms=r.pc.max_quote_age_ms)
        r.observer = SingleEngine(r.c, data["observer"], quote_age_ms=r.pc.max_quote_age_ms)
        r.decisions = copy.deepcopy(data["decisions"]); r.evaluations = copy.deepcopy(data["evaluations"])
        r.labelled = set(data["labelled"]); r.last_ns = data["last_ns"]
        r.label_cursor = data.get("label_cursor", len(r.observer.accounts[ARM]["ledger"]))
        r.next_prune_ns = data.get("next_prune_ns", 0)
        if not 0 <= r.label_cursor <= len(r.observer.accounts[ARM]["ledger"]):
            raise ValueError("invalid label cursor")
        if r.evidence.cost_hash != r.c.fingerprint() or r.evidence.fixture_mode != r.fixture_mode or r.evidence.c.digest() != r.pc.digest():
            raise ValueError("snapshot evidence/cost/policy mode mismatch")
        r.halt_new_entries = data["halt_new_entries"]; r.interrupted = data["interrupted"]
        if process_restart and (r.candidate.bundles or r.observer.bundles):
            r.candidate.mark_interrupted(); r.observer.mark_interrupted()
            r.interrupted = True; r.halt_new_entries = True
        return r

# FULL3S_HARDENING_20260916_1
