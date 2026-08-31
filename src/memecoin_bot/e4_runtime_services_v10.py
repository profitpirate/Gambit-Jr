from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from . import e4_fast_execution_v10 as fast
from .e4_pipelines_v10 import CreatorProfile, CreatorTier, E4_WALLET

core = fast.core
v6 = fast.v6
PIPELINES = fast.PIPELINES
LOGGER = logging.getLogger("gambit.e4.runtime_services.v10")


@dataclass(slots=True)
class _E4Position:
    mint: str
    creator: str
    bought_tokens: float = 0.0
    sold_tokens: float = 0.0
    buy_sol: float = 0.0
    sell_sol: float = 0.0
    opened_ns: int = 0
    last_ns: int = 0


class JsonlTailer(threading.Thread):
    def __init__(self, path: Path, callback, *, poll_seconds: float, name: str) -> None:
        super().__init__(name=name, daemon=True)
        self.path = path
        self.callback = callback
        self.poll_seconds = max(0.005, poll_seconds)
        self.stop_event = threading.Event()
        self.offset = 0
        self.identity: tuple[int, int] | None = None

    def stop(self) -> None:
        self.stop_event.set()

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                stat = self.path.stat()
                identity = (stat.st_dev, stat.st_ino)
                if self.identity is not None and identity != self.identity:
                    self.offset = 0
                self.identity = identity
                if stat.st_size < self.offset:
                    self.offset = 0
                if stat.st_size > self.offset:
                    with self.path.open("r", encoding="utf-8") as handle:
                        handle.seek(self.offset)
                        for line in handle:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                payload = json.loads(line)
                                if isinstance(payload, Mapping):
                                    self.callback(dict(payload))
                            except Exception:
                                LOGGER.exception("E4 V10 journal row rejected path=%s", self.path)
                        self.offset = handle.tell()
            except FileNotFoundError:
                pass
            except Exception:
                LOGGER.exception("E4 V10 journal tailer failed path=%s", self.path)
            self.stop_event.wait(self.poll_seconds)


class ModelReloader(threading.Thread):
    def __init__(self, poll_seconds: float = 0.5) -> None:
        super().__init__(name="e4-v10-model-reloader", daemon=True)
        self.poll_seconds = max(0.1, poll_seconds)
        self.stop_event = threading.Event()
        self._mtimes: dict[Path, int] = {}

    def stop(self) -> None:
        self.stop_event.set()

    def run(self) -> None:
        paths = (
            PIPELINES.creators.expectancy_path,
            PIPELINES.creators.discovered_path,
            PIPELINES.creators.operator_path,
        )
        while not self.stop_event.is_set():
            changed = False
            for path in paths:
                try:
                    mtime = path.stat().st_mtime_ns
                except FileNotFoundError:
                    mtime = 0
                if path not in self._mtimes:
                    self._mtimes[path] = mtime
                elif self._mtimes[path] != mtime:
                    self._mtimes[path] = mtime
                    changed = True
            if changed:
                try:
                    PIPELINES.creators.reload()
                except Exception:
                    LOGGER.exception("E4 V10 creator model reload failed")
            self.stop_event.wait(self.poll_seconds)


class E4OutcomeTeacher:
    """Learn rolling creator expectancy directly from observed E4 Pump trades."""

    def __init__(self, overlay_path: Path | None = None, journal_path: Path | None = None) -> None:
        self.overlay_path = overlay_path or Path(
            os.getenv("E4_LIVE_CREATOR_OVERLAY", "var/e4/e4-live-creator-overlay.json")
        )
        self.journal_path = journal_path or Path(
            os.getenv("E4_LIVE_OUTCOME_JOURNAL", "var/e4/e4-live-outcomes.jsonl")
        )
        self.positions: dict[str, _E4Position] = {}
        self._lock = threading.Lock()
        self.overlay: Mapping[str, CreatorProfile] = MappingProxyType({})
        self._load_overlay()

    def _load_overlay(self) -> None:
        try:
            data = json.loads(self.overlay_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return
        profiles: dict[str, CreatorProfile] = {}
        records = data.get("creators") if isinstance(data, Mapping) else None
        if not isinstance(records, Mapping):
            return
        for creator, row in records.items():
            if not isinstance(row, Mapping):
                continue
            tier_name = str(row.get("tier") or "WATCH").upper()
            try:
                tier = CreatorTier[tier_name]
            except KeyError:
                tier = CreatorTier.WATCH
            profiles[str(creator)] = CreatorProfile(
                creator=str(creator),
                tier=tier,
                score=float(row.get("score") or 0.0),
                wins=int(row.get("wins") or 0),
                losses=int(row.get("losses") or 0),
                trades=int(row.get("trades") or 0),
                gross_win_rate=float(row.get("gross_win_rate") or 0.0),
                gross_pnl_sol=float(row.get("gross_pnl_sol") or 0.0),
                source="live-e4-teacher",
                evidence=tuple(row.get("evidence") or ()),
                updated_ns=int(row.get("updated_ns") or 0),
            )
        self.overlay = MappingProxyType(profiles)

    @staticmethod
    def _tier(wins: int, losses: int) -> CreatorTier:
        trades = wins + losses
        rate = wins / trades if trades else 0.0
        if trades >= 3 and rate <= 0.25:
            return CreatorTier.NEGATIVE
        if trades >= 3 and wins >= 2 and rate >= 0.75:
            return CreatorTier.ELITE
        if wins >= 1 and rate >= 0.50:
            return CreatorTier.APPROVED
        return CreatorTier.WATCH

    def _persist(self, outcome: Mapping[str, Any]) -> None:
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        with self.journal_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(dict(outcome), separators=(",", ":")) + "\n")
        payload = {
            "version": "e4-live-creator-overlay-v1",
            "updated_ns": time.time_ns(),
            "creators": {
                creator: {
                    **asdict(profile),
                    "tier": profile.tier.name,
                }
                for creator, profile in self.overlay.items()
            },
        }
        self.overlay_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.overlay_path.with_suffix(self.overlay_path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temporary.replace(self.overlay_path)

    def _update_creator(self, creator: str, pnl: float, mint: str, observed_ns: int) -> None:
        base = PIPELINES.creators.lookup(creator)
        live = self.overlay.get(creator)
        wins = int(getattr(live, "wins", 0)) + (1 if pnl > 0 else 0)
        losses = int(getattr(live, "losses", 0)) + (1 if pnl <= 0 else 0)
        trades = wins + losses
        rate = wins / trades
        live_pnl = float(getattr(live, "gross_pnl_sol", 0.0)) + pnl
        tier = self._tier(wins, losses)
        if base is not None:
            # Never demote a large historical sample because of one live result;
            # negative live evidence still reaches the overlay and can be audited.
            if base.tier == CreatorTier.NEGATIVE and tier != CreatorTier.NEGATIVE:
                tier = CreatorTier.NEGATIVE
            elif base.trades >= 3:
                tier = max(base.tier, tier)
        profile = CreatorProfile(
            creator=creator,
            tier=tier,
            score={
                CreatorTier.NEGATIVE: 0.05,
                CreatorTier.UNKNOWN: 0.0,
                CreatorTier.WATCH: 0.58,
                CreatorTier.APPROVED: 0.84,
                CreatorTier.ELITE: 0.95,
            }[tier],
            wins=wins,
            losses=losses,
            trades=trades,
            gross_win_rate=rate,
            gross_pnl_sol=live_pnl,
            source="live-e4-teacher",
            evidence=(f"live-e4:{wins}W/{losses}L", f"last-mint:{mint}"),
            updated_ns=observed_ns,
        )
        profiles = dict(self.overlay)
        profiles[creator] = profile
        self.overlay = MappingProxyType(profiles)
        self._persist(
            {
                "mint": mint,
                "creator": creator,
                "gross_pnl_sol": pnl,
                "outcome": "WIN" if pnl > 0 else "LOSS",
                "observed_ns": observed_ns,
                "rolling_wins": wins,
                "rolling_losses": losses,
                "rolling_tier": tier.name,
            }
        )

    def observe_event(self, event: Any) -> None:
        if str(getattr(event, "trader", "") or "") != E4_WALLET:
            return
        kind = str(getattr(getattr(event, "kind", None), "value", getattr(event, "kind", ""))).upper()
        if kind not in {"BUY", "SELL"}:
            return
        mint = str(getattr(event, "mint", "") or "")
        if not mint:
            return
        tokens = max(0.0, float(getattr(event, "token_amount", 0.0) or 0.0))
        sol = max(0.0, float(getattr(event, "sol_amount", 0.0) or 0.0))
        observed_ns = int(getattr(event, "received_ns", 0) or time.time_ns())
        context = v6._CONTEXT_BY_MINT.get(mint, {})
        creator = str(getattr(event, "creator", "") or context.get("creator") or "")
        with self._lock:
            if kind == "BUY":
                position = self.positions.setdefault(
                    mint,
                    _E4Position(mint=mint, creator=creator, opened_ns=observed_ns, last_ns=observed_ns),
                )
                if creator:
                    position.creator = creator
                position.bought_tokens += tokens
                position.buy_sol += sol
                position.last_ns = observed_ns
                return
            position = self.positions.get(mint)
            if position is None:
                return
            position.sold_tokens += tokens
            position.sell_sol += sol
            position.last_ns = observed_ns
            tolerance = max(1e-9, position.bought_tokens * 0.002)
            if position.sold_tokens + tolerance < position.bought_tokens:
                return
            self.positions.pop(mint, None)
            pnl = position.sell_sol - position.buy_sol
            creator = position.creator or creator
            if creator:
                self._update_creator(creator, pnl, mint, observed_ns)


_STARTED = False
_THREADS: list[threading.Thread] = []
_TEACHER: E4OutcomeTeacher | None = None
_PREVIOUS_FROM_ROW = None
_PREVIOUS_LOOKUP = None


def start_runtime_services() -> None:
    global _STARTED, _TEACHER, _PREVIOUS_FROM_ROW, _PREVIOUS_LOOKUP
    if _STARTED or str(os.getenv("E4_V10_SERVICES_ENABLED", "true")).lower() in {"0", "false", "no", "off"}:
        return
    _STARTED = True
    _TEACHER = E4OutcomeTeacher()

    creator_class = type(PIPELINES.creators)
    _PREVIOUS_LOOKUP = creator_class.lookup

    def lookup_with_live_overlay(self, creator):
        if creator and _TEACHER is not None:
            live = _TEACHER.overlay.get(str(creator))
            if live is not None:
                historical = _PREVIOUS_LOOKUP(self, creator)
                if historical is None or live.tier == CreatorTier.NEGATIVE or live.trades >= historical.trades:
                    return live
        return _PREVIOUS_LOOKUP(self, creator)

    creator_class.lookup = lookup_with_live_overlay

    _PREVIOUS_FROM_ROW = core.Event.from_row.__func__

    def from_row_with_teacher(cls, row):
        event = _PREVIOUS_FROM_ROW(cls, row)
        assert _TEACHER is not None
        _TEACHER.observe_event(event)
        return event

    core.Event.from_row = classmethod(from_row_with_teacher)

    social_path = Path(os.getenv("E4_SOCIAL_SIGNAL_JOURNAL", "var/e4/social-stream.jsonl"))
    intent_path = Path(os.getenv("E4_LAUNCH_INTENT_JOURNAL", "var/e4/launch-intents.jsonl"))

    def social_callback(row: Mapping[str, Any]) -> None:
        PIPELINES.observe_social_post(
            source=str(row.get("source") or row.get("platform") or "social"),
            source_account=str(row.get("source_account") or row.get("author") or row.get("handle") or "unknown"),
            text=str(row.get("text") or row.get("content") or ""),
            created_ns=int(row.get("created_ns") or row.get("published_ns") or time.time_ns()),
            observed_ns=int(row.get("observed_ns") or time.time_ns()),
            authority=float(row.get("authority") or row.get("authority_score") or 0.0),
            engagement_velocity=float(row.get("engagement_velocity") or row.get("velocity") or 0.0),
            provenance=str(row.get("provenance") or "social-journal"),
        )

    def intent_callback(row: Mapping[str, Any]) -> None:
        PIPELINES.intents.ingest(row)

    social = JsonlTailer(
        social_path,
        social_callback,
        poll_seconds=float(os.getenv("E4_SOCIAL_TAIL_POLL_MS", "20")) / 1000.0,
        name="e4-v10-social-tailer",
    )
    intents = JsonlTailer(
        intent_path,
        intent_callback,
        poll_seconds=float(os.getenv("E4_INTENT_TAIL_POLL_MS", "10")) / 1000.0,
        name="e4-v10-intent-tailer",
    )
    models = ModelReloader(float(os.getenv("E4_MODEL_RELOAD_SECONDS", "0.5")))
    for thread in (social, intents, models):
        thread.start()
        _THREADS.append(thread)
    LOGGER.info("E4 V10 runtime services started")


def stop_runtime_services() -> None:
    global _STARTED
    for thread in tuple(_THREADS):
        stop = getattr(thread, "stop", None)
        if callable(stop):
            stop()
    for thread in tuple(_THREADS):
        thread.join(timeout=1.0)
    _THREADS.clear()
    PIPELINES.teacher.stop()
    _STARTED = False
