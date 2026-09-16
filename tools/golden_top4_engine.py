"""Four independent PAPER accounts. Frozen strategies; no signer or order API.

Original controls are full exits at 3/4/7s. V2 is loaded byte-for-byte from its
frozen source. This adapter supplies identical incoming observations, rejects
noncausal clocks, preserves every attempted cost, and emits every action.
"""
from __future__ import annotations
import copy
import hashlib
import importlib.util
import json
import math
import os
import sys
import time
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable
import golden_full3s_v2 as v2

CONTROLS = ("FULL_3S", "FULL_4S", "FULL_7S")
V2 = "FULL_3S_V2"
ARMS = (*CONTROLS, V2)
VERSION = "golden-top4-forward-1"

def load_controls():
    name = "_golden_top4_controls"
    if name in sys.modules:
        return sys.modules[name]
    path = Path(__file__).with_name("golden_horizon_engine.py")
    raw = path.read_bytes()
    if hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest() != v2.BASELINE_GIT_BLOB:
        raise RuntimeError("control accounting source is not frozen")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    mod.ARMS = CONTROLS
    return mod

base = load_controls()
Config = base.Config

def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w") as f:
        json.dump(obj, f, sort_keys=True, allow_nan=False)
        f.write("\n"); f.flush(); os.fsync(f.fileno())
    os.replace(tmp, path)

class CampaignEngine:
    last_instance = None
    output: Path | None = None
    smoke = False
    require_reporter = False

    def __init__(self, config: Config, state=None, *, fixture_mode=False):
        self.c = config
        self.controls = base.Engine(config)
        self.runtime = v2.Runtime(v2.Config(**asdict(config)), fixture_mode=fixture_mode)
        self.fixture_mode = fixture_mode
        self.ops_errors = []
        self.decisions = {}
        self.proofs = {}
        self.action_cursors = {}
        self.last_now = 0
        self.events = []
        self.control_uncertain = set()
        self.local_ticks = []
        self.local_selection = []
        self.restore_count = 0
        if state is not None:
            if state.get("version") != VERSION or state.get("config_hash") != config.fingerprint():
                raise ValueError("incompatible campaign restart")
            if not state.get("clean") or state.get("errors") or state.get("bundles"):
                raise ValueError("unresolved campaign cannot resume from an earlier checkpoint")
            if state.get("fixture_mode") != fixture_mode:
                raise ValueError("synthetic/live mode mixing")
            self.controls = base.Engine(config, state["controls"])
            self.runtime = v2.Runtime.restore(state["v2"])
            self.decisions = copy.deepcopy(state["decisions"])
            self.proofs = copy.deepcopy(state["proofs"])
            self.action_cursors = dict(state["action_cursors"])
            self.last_now = state["last_now"]
            self.events = copy.deepcopy(state["events"])
            self.control_uncertain = set(state["control_uncertain"])
            self.restore_count = state.get("restore_count", 0) + 1
            self.validate()
        CampaignEngine.last_instance = self

    @property
    def errors(self):
        return list(dict.fromkeys(self.ops_errors + self.controls.errors + self.runtime.candidate.errors + self.runtime.observer.errors))

    @property
    def accounts(self):
        return {**self.controls.accounts, V2: self.runtime.candidate.accounts[v2.ARM]}

    @property
    def rejections(self):
        return self.controls.rejections

    def all_engines(self):
        return (self.controls, self.runtime.candidate, self.runtime.observer)

    def _propagate_proofs(self):
        for eng in self.all_engines():
            for row in list(eng.bundles.values()) + eng.history:
                if row.get("creation_proof"):
                    self.proofs[row["mint"]] = row["creation_proof"]
        for eng in self.all_engines():
            for row in list(eng.bundles.values()) + eng.history:
                if row["mint"] in self.proofs:
                    row["creation_proof"] = self.proofs[row["mint"]]

    @property
    def bundles(self):
        self._propagate_proofs()
        out = {}
        for eng in self.all_engines():
            for mint, b in eng.bundles.items():
                out.setdefault(mint, b)
        return out

    @property
    def history(self):
        self._propagate_proofs()
        return [h for e in self.all_engines() for h in e.history]

    def submit(self, d, s, now):
        if self.smoke:
            return False
        if self.require_reporter and self.output is not None:
            try:
                health = json.loads((self.output/"publisher-health.json").read_text())
                healthy = health.get("healthy") is True and time.time_ns()-health["checked_ns"] < 120_000_000_000
            except (OSError, ValueError, KeyError):
                healthy = False
            if not healthy or (self.output/"stop-request").exists():
                self.controls.rejections.append({"mint":d["mint"], "reason":"REPORTING_OR_OPERATOR_ENTRY_PAUSE", "ns":now})
                return False
        if self.errors or now < self.last_now or d["mint"] in self.decisions:
            return False
        self.decisions[d["mint"]] = copy.deepcopy(d)
        t = time.perf_counter_ns()
        accepted_controls = self.controls.submit(copy.deepcopy(d), copy.deepcopy(s), now)
        result = self.runtime.submit(copy.deepcopy(d), copy.deepcopy(s), now)
        self.local_selection.append((time.perf_counter_ns() - t) / 1e6)
        self._event({"kind": "SELECTION", "mint": d["mint"], "decision": d,
                     "controls_submitted": accepted_controls, "v2_evaluation": result,
                     "completed_local_selection_ms": self.local_selection[-1],
                     "data_mode": "SYNTHETIC_FIXTURE" if self.fixture_mode else "FORWARD_LIVE_OBSERVATION"})
        return accepted_controls or result.get("observer_submitted", False) or result.get("candidate_submitted", False)

    def positions(self):
        for arm, a in self.accounts.items():
            positions = {p["signal_id"]: p for p in a["ledger"] + a["aborted_entries"]}
            eng = self.runtime.candidate if arm == V2 else self.controls
            original_arm = v2.ARM if arm == V2 else arm
            for b in eng.bundles.values():
                positions[b["signal_id"]] = b["positions"][original_arm]
            for p in positions.values():
                yield arm, p

    def _event(self, row):
        row = copy.deepcopy(row)
        row.update(event_id=len(self.events)+1, reported_ns=time.time_ns(),
                   execution="PAPER_MODEL", actual_axiom_fill=False,
                   actual_profit_sol=None, actual_fees_sol=None)
        self.events.append(row)
        if self.output is not None:
            self.output.mkdir(parents=True, exist_ok=True)
            with (self.output/"entry-exit-events.jsonl").open("a") as f:
                f.write(json.dumps(row, sort_keys=True, allow_nan=False)+"\n")
                f.flush(); os.fsync(f.fileno())
            write_json(self.output/"activity.json", {"schema": VERSION, "events": self.events,
                "count": len(self.events), "updated_ns": row["reported_ns"], "paper_only": True,
                "axiom_listing_verified": None, "note": "Every entry/exit/failure, not real Axiom fills"})
            print("TRADE_EVENT " + json.dumps(row, sort_keys=True, allow_nan=False), flush=True)

    def emit_actions(self):
        for arm, p in self.positions():
            key = arm + ":" + p["signal_id"]
            start = self.action_cursors.get(key, 0)
            for i in range(start, len(p["actions"])):
                action = p["actions"][i]
                through = p["actions"][:i+1]
                paid = Counter()
                for act in through:
                    paid.update(act.get("fees", {}))
                decision = self.decisions[p["mint"]]
                s = action.get("state") or {}
                q = action.get("quote_state") or {}
                self._event({"kind": "TRANSACTION_ATTEMPT", "model": arm,
                    "mint": p["mint"], "coin_name": decision.get("name"), "symbol": decision.get("symbol"),
                    "signal_id": p["signal_id"], "action_number": i+1,
                    "launch_signature": decision.get("create_signature"), "launch_observed_ns": decision.get("create_ns"),
                    "decision_ns": decision["decision_ns"], "tier": p["tier"], "stake_sol": p["budget"],
                    "action": action, "action_fees_sol": sum(action.get("fees", {}).values()),
                    "cumulative_trade_fees_by_category": dict(paid), "cumulative_trade_fees_sol": sum(paid.values()),
                    "slippage_is_embedded_in_fill_not_double_debited": True,
                    "quote_age_at_modelled_fill_ms": (action["ns"]-s["ns"])/1e6 if s.get("ns") else None,
                    "intended_quote_spot": q["vsol"]/q["vtok"] if q.get("vtok") else None,
                    "observed_fill_state_spot": s["vsol"]/s["vtok"] if s.get("vtok") else None,
                    "gross_closed_trade_pnl_sol": p.get("gross_pnl_sol") if p["status"] == "CLOSED" and i == len(p["actions"])-1 else None,
                    "net_closed_trade_pnl_sol": p.get("net_pnl_sol") if p["status"] == "CLOSED" and i == len(p["actions"])-1 else None,
                    "cash_at_report_sol": self.accounts[arm]["cash"],
                    "inventory_at_report_tokens": p["remaining"],
                    "holding_ms": p.get("hold_ms"),
                    "v2_entry_evaluation": self.runtime.evaluations.get(p["mint"]) if arm == V2 else None,
                    "cost_hash": self.c.fingerprint()})
            self.action_cursors[key] = len(p["actions"])

    def tick(self, now, states, last_feed_ns):
        t = time.perf_counter_ns()
        if now < self.last_now:
            self.ops_errors.append("CLOCK_REGRESSION")
            return
        self.last_now = now
        if now-last_feed_ns > self.c.feed_stale_ms*1_000_000:
            self.control_uncertain.update(self.controls.bundles)
        self._propagate_proofs()
        self.controls.tick(now, states, last_feed_ns)
        self.runtime.tick(now, states, last_feed_ns)
        self._propagate_proofs()
        self.emit_actions()
        self.local_ticks.append((time.perf_counter_ns()-t)/1e6)
        if len(self.local_ticks)>10000:
            del self.local_ticks[:5000]

    def forward_counts(self):
        if self.fixture_mode:
            return {name: 0 for name in ARMS}
        self._propagate_proofs()
        counts = {}
        for arm, a in self.accounts.items():
            uncertain = self.runtime.candidate.uncertain_mints if arm == V2 else self.control_uncertain
            counts[arm] = sum(p["mint"] not in uncertain and
                bool(self.proofs.get(p["mint"], {}).get("verified")) and
                p.get("post", {}).get("complete") is True and
                p.get("post", {}).get("coverage") in ("OBSERVED", "NO_FURTHER_TICKS_OBSERVED")
                for p in a["ledger"])
        return counts

    def matched_closed(self):
        # Compatibility with the collector's stop predicate. This is MIN per-arm
        # verified count, not a claim that V2 took all the same coins as controls.
        return min(self.forward_counts().values(), default=0)

    def summary(self):
        rows = self.controls.summary()
        rows[V2] = self.runtime.candidate.summary()[v2.ARM]
        counts = self.forward_counts()
        for name, row in rows.items():
            row["verified_forward_closed"] = counts[name]
            row["unverified_or_incomplete_count"] = row["closed_trades"]-counts[name]
        return rows

    def persistence(self):
        self._propagate_proofs()
        return {"version": VERSION, "config": asdict(self.c), "config_hash": self.c.fingerprint(),
            "fixture_mode": self.fixture_mode, "paper_only": True, "real_execution": False,
            "clean": not self.bundles and not self.errors, "errors": self.errors,
            "bundles": copy.deepcopy(self.bundles), "controls": self.controls.persistence(),
            "v2": self.runtime.snapshot(), "decisions": self.decisions, "proofs": self.proofs,
            "action_cursors": self.action_cursors, "events": self.events,
            "last_now": self.last_now, "control_uncertain": sorted(self.control_uncertain),
            "restore_count": self.restore_count}

    def validate(self):
        self.controls.validate()
        self.runtime.candidate.validate(); self.runtime.observer.validate()
        if set(self.accounts) != set(ARMS):
            raise ValueError("wrong model set")
        if len({e["event_id"] for e in self.events}) != len(self.events):
            raise ValueError("duplicate report event IDs")
        for arm, p in self.positions():
            key = arm+":"+p["signal_id"]
            if self.action_cursors.get(key, 0) != len(p["actions"]):
                raise ValueError("unreported transaction attempt")
            if p["status"] == "CLOSED":
                fees = sum(sum(x.get("fees", {}).values()) for x in p["actions"])
                if not math.isclose(fees, sum(p["fees"].values()), abs_tol=1e-9):
                    raise ValueError("attempt fees do not reconcile")
