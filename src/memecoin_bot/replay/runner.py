from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from memecoin_bot.config import Settings
from memecoin_bot.database import Store
from memecoin_bot.models import (
    CandidateState, DeveloperClass, DiscoveryEvent, MarketSnapshot, SafetyAssessment, SignalClass,
)
from memecoin_bot.safety import SafetyGates
from memecoin_bot.scoring import ScoringEngine
from memecoin_bot.signals import signal_payload
from memecoin_bot.tracking import SignalTracker


class ReplayMarket:
    def __init__(self, sequences: dict[str, list[MarketSnapshot]]):
        self.sequences = sequences
        self.index = {token: 0 for token in sequences}

    def advance(self, token: str) -> None:
        self.index[token] = min(self.index[token] + 1, len(self.sequences[token]) - 1)

    async def market_snapshot(self, token: str) -> MarketSnapshot | None:
        values = self.sequences.get(token)
        return values[self.index[token]] if values else None


class ReplayRunner:
    """Deterministic mechanics proof. Output is always explicitly marked simulated."""

    def __init__(self, settings: Settings, store: Store):
        self.settings = settings
        self.store = store

    @staticmethod
    def load(path: str | Path) -> dict[str, Any]:
        return json.loads(Path(path).read_text(encoding="utf-8"))

    async def run(self, fixture_path: str | Path) -> dict[str, Any]:
        fixture = self.load(fixture_path)
        sequences: dict[str, list[MarketSnapshot]] = {}
        safeties: dict[str, SafetyAssessment] = {}
        components: dict[str, dict[str, float | None]] = {}
        for token in fixture["tokens"]:
            address = token["discovery"]["token_address"]
            sequences[address] = [MarketSnapshot(**x) for x in token["snapshots"]]
            safeties[address] = SafetyAssessment(**token["safety"])
            components[address] = token["components"]
        market = ReplayMarket(sequences)
        scoring = ScoringEngine(self.settings)
        gates = SafetyGates(self.settings)
        created: list[int] = []
        decisions: list[dict[str, Any]] = []
        for token in fixture["tokens"]:
            discovery = DiscoveryEvent(**token["discovery"])
            token_id, _ = self.store.upsert_discovery(discovery)
            candidate_id, _ = self.store.ensure_candidate(
                token_id, discovery.discovered_at, self.settings.scoring_version
            )
            snapshot = sequences[discovery.token_address][0]
            safety = safeties[discovery.token_address]
            self.store.save_snapshot(token_id, snapshot, safety.holder_count)
            hard = gates.evaluate(snapshot, safety)
            result = scoring.score(components[discovery.token_address], hard)
            evidence = {"simulation": True, "fixture": str(fixture_path),
                        "market": snapshot.to_dict(), "safety": asdict(safety)}
            self.store.save_evaluation(token_id, result, evidence)
            if hard:
                self.store.update_candidate(candidate_id, CandidateState.REJECTED_UNSAFE,
                                            hard[0], snapshot, result, hard_rejections=hard)
            decisions.append({"token_address": discovery.token_address,
                              "classification": str(result.classification),
                              "score": result.total, "rejections": result.hard_rejections})
            if result.classification in {SignalClass.WATCH, SignalClass.STRONG, SignalClass.HIGH_CONVICTION}:
                intelligence = {
                    "developer": {"classification": DeveloperClass.KNOWN_OF, "score": components[discovery.token_address]["developer"]},
                    "narrative": {"label": "REPLAY_CATALYST", "score": components[discovery.token_address]["narrative"]},
                    "social": {"score": components[discovery.token_address]["social"]},
                    "onchain": {"score": components[discovery.token_address]["onchain"]},
                    "momentum": {"score": components[discovery.token_address]["momentum"], "buy_sell_ratio": 2.4},
                    "risks": ["SIMULATED_REPLAY_NOT_LIVE_EVIDENCE"],
                    "thesis": ["Deterministic fixture exercises production scoring and tracking"],
                }
                payload = signal_payload(discovery, snapshot, safety, intelligence, result, True)
                signal_id = self.store.create_signal(
                    token_id, snapshot, result,
                    {k: intelligence[k] for k in ("developer", "narrative", "social", "onchain")},
                    intelligence["risks"], payload, safety.holder_count,
                )
                if signal_id:
                    created.append(signal_id)
                    self.store.update_candidate(candidate_id, CandidateState.SIGNALLED,
                                                f"PROMOTED_{result.classification}", snapshot, result,
                                                signal_id=signal_id)
            elif not hard:
                self.store.update_candidate(candidate_id, CandidateState.PENDING_EVIDENCE,
                                            "REPLAY_SCORE_BELOW_THRESHOLD", snapshot, result,
                                            waiting_reasons=["REPLAY_SCORE_BELOW_THRESHOLD"])

        tracker = SignalTracker(self.store, market, self.settings)
        cycle_results = []
        max_length = max(len(x) for x in sequences.values())
        for _ in range(1, max_length):
            for address in sequences:
                market.advance(address)
            cycle_results.append(await tracker.monitor_once())
        return {
            "evidence_type": "SIMULATION_ONLY_NOT_LIVE_E2E",
            "fixture": str(fixture_path),
            "decisions": decisions,
            "signals_created": created,
            "cycles": cycle_results,
            "milestones": [dict(x) for x in self.store.conn.execute("SELECT * FROM milestones ORDER BY id")],
            "active_after_replay": len(self.store.active_signals()),
            "candidate_transitions": [dict(x) for x in self.store.conn.execute(
                "SELECT * FROM candidate_transitions ORDER BY id"
            )],
            "performance": self.store.performance(self.settings.scoring_version),
        }

