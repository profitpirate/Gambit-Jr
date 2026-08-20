from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from memecoin_bot.config import Settings
from memecoin_bot.database import Store
from memecoin_bot.discovery import DiscoveryPoller
from memecoin_bot.models import DiscoveryEvent, MarketSnapshot, SafetyAssessment
from memecoin_bot.service import IntelligenceService


def _key(chain: str, address: str) -> str:
    return f"{chain}:{address}"


class ReplayMarket:
    name = "replay_market"

    def __init__(self, sequences: dict[str, list[MarketSnapshot]], errors: dict[str, set[int]] | None = None):
        self.sequences = sequences
        self.index = {token: 0 for token in sequences}
        self.errors = errors or {}

    def advance_all(self) -> None:
        for token, values in self.sequences.items():
            self.index[token] = min(self.index[token] + 1, len(values) - 1)

    async def market_snapshot(self, token: str, chain: str = "solana") -> MarketSnapshot | None:
        key = _key(chain, token)
        values = self.sequences.get(key)
        if self.index.get(key, 0) in self.errors.get(key, set()):
            from memecoin_bot.providers.base import ProviderError
            raise ProviderError("replay provider outage")
        return values[self.index[key]] if values else None


class ReplaySafety:
    name = "replay_safety"

    def __init__(self, values: dict[str, SafetyAssessment]):
        self.values = values

    async def safety(self, chain: str, token: str) -> SafetyAssessment:
        return self.values[_key(chain, token)]


class ReplayDiscovery:
    def __init__(self, events: list[DiscoveryEvent]):
        self.events = events

    async def discover(self) -> list[DiscoveryEvent]:
        events, self.events = self.events, []
        return events


class ReplayNotifier:
    async def send(self, payload: object) -> str:
        return "replay-delivered"


class ReplayRunner:
    """Deterministic provider fixture executed through the production lifecycle."""

    def __init__(self, settings: Settings, store: Store):
        self.settings = settings
        self.store = store

    @staticmethod
    def load(path: str | Path) -> dict[str, Any]:
        return json.loads(Path(path).read_text(encoding="utf-8"))

    @staticmethod
    def _shift_time(value: str | None, offset: timedelta) -> str | None:
        if value is None:
            return None
        return (datetime.fromisoformat(value.replace("Z", "+00:00")) + offset).isoformat()

    async def run(self, fixture_path: str | Path) -> dict[str, Any]:
        fixture = self.load(fixture_path)
        source_times = [
            datetime.fromisoformat(t["discovery"]["discovered_at"].replace("Z", "+00:00"))
            for t in fixture["tokens"]
        ]
        offset = datetime.now(timezone.utc) - timedelta(minutes=2) - min(source_times)
        sequences: dict[str, list[MarketSnapshot]] = {}
        safeties: dict[str, SafetyAssessment] = {}
        discoveries: list[DiscoveryEvent] = []
        market_errors: dict[str, set[int]] = {}
        for token in fixture["tokens"]:
            event_data = dict(token["discovery"])
            event_data["discovered_at"] = self._shift_time(event_data["discovered_at"], offset)
            discovery = DiscoveryEvent(**event_data)
            discoveries.append(discovery)
            snapshots: list[MarketSnapshot] = []
            for raw in token["snapshots"]:
                data = dict(raw)
                data.setdefault("chain", discovery.chain)
                data["captured_at"] = self._shift_time(data["captured_at"], offset)
                data["pair_created_at"] = self._shift_time(data.get("pair_created_at"), offset)
                snapshots.append(MarketSnapshot(**data))
            sequences[_key(discovery.chain, discovery.token_address)] = snapshots
            market_errors[_key(discovery.chain, discovery.token_address)] = set(token.get("market_errors_at") or [])
            safety_data = dict(token["safety"])
            safety_data.setdefault("chain", discovery.chain)
            safety_data["checked_at"] = self._shift_time(safety_data["checked_at"], offset)
            safeties[_key(discovery.chain, discovery.token_address)] = SafetyAssessment(**safety_data)

        market = ReplayMarket(sequences, market_errors)
        service = IntelligenceService(
            self.settings, self.store, DiscoveryPoller(ReplayDiscovery(discoveries)),
            market, ReplaySafety(safeties), ReplayNotifier(),
        )
        initial = await service.scan_once()
        cycles: list[dict[str, Any]] = []
        for _ in range(1, max(len(x) for x in sequences.values())):
            market.advance_all()
            candidates = await service.monitor_candidates_once()
            tracking = await service.tracker.monitor_once()
            await service.flush_outbox()
            cycles.append({"candidates": candidates, "tracking": tracking})

        decisions = [dict(row) for row in self.store.conn.execute(
            "SELECT e.*,t.chain,t.token_address FROM evaluations e JOIN tokens t ON t.id=e.token_id "
            "ORDER BY e.id"
        )]
        signals = [int(row[0]) for row in self.store.conn.execute("SELECT id FROM signals ORDER BY id")]
        return {
            "evidence_type": "SIMULATION_ONLY_NOT_LIVE_E2E",
            "fixture": str(fixture_path), "initial": initial, "decisions": decisions,
            "signals_created": signals, "cycles": cycles,
            "radar_events": [dict(x) for x in self.store.conn.execute("SELECT * FROM radar_events ORDER BY id")],
            "candidate_transitions": [dict(x) for x in self.store.conn.execute(
                "SELECT * FROM candidate_transitions ORDER BY id"
            )],
            "milestones": [dict(x) for x in self.store.conn.execute("SELECT * FROM milestones ORDER BY id")],
            "outcomes": [dict(x) for x in self.store.conn.execute("SELECT * FROM token_outcomes ORDER BY token_id")],
            "active_after_replay": len(self.store.active_signals()),
            "performance": self.store.performance(
                self.settings.scoring_version, major_multiple=self.settings.major_missed_runner_multiple
            ),
        }
