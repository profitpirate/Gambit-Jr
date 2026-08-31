from __future__ import annotations

import hashlib
import json
import math
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class PipelineDecision:
    accepted: bool
    family: str
    score: float
    fraction: float
    reason: str
    evidence: Mapping[str, Any] = field(default_factory=dict)
    decision_ns: int = 0


@dataclass(frozen=True, slots=True)
class CreatorIdentity:
    creator: str
    status: str
    score: float
    wins: int = 0
    losses: int = 0
    trades: int = 0
    gross_win_rate: float = 0.0
    source: str = "unknown"


@dataclass(frozen=True, slots=True)
class E4Signal:
    mint: str
    creator: str
    observed_ns: int
    entry_price_sol: float
    entry_sol: float
    signature: str
    entry_tokens: float = 0.0
    remaining_tokens: float = 0.0
    last_sell_fraction: float = 0.0
    last_sell_ns: int = 0
    last_sell_signature: str = ""
    sell_count: int = 0
    fully_exited: bool = False
    sold: bool = False


@dataclass(slots=True)
class LearningState:
    mint: str
    creator: str
    launched_ns: int
    last_event_ns: int
    entry_price_sol: float
    latest_price_sol: float
    peak_price_sol: float
    sell_count: int = 0
    finalized: bool = False

    @property
    def peak_multiple(self) -> float:
        if self.entry_price_sol <= 0:
            return 0.0
        return self.peak_price_sol / self.entry_price_sol


class PipelineManager:
    """Canonical V11 authority for creator, social and E4-copy decisions.

    The hot path uses immutable snapshots and dictionary lookups only. File I/O,
    model enrichment and persistence remain off the launch-decision path.
    """

    def __init__(self) -> None:
        # Imported lazily to avoid a circular import while e4_pipelines_v10
        # exposes this class as a backwards-compatible public API.
        from . import e4_pipelines_v10 as base

        self._base = base
        self.creators = base.CreatorRegistry()
        self.narratives = base.NarrativeCache()
        self.teacher = base.E4Learner()
        self.intents = base.LaunchIntentRegistry()
        self._lock = threading.RLock()
        self._e4_entries: Mapping[str, E4Signal] = MappingProxyType({})
        self._social_by_ca: Mapping[str, tuple[Any, ...]] = MappingProxyType({})
        self._learning: dict[str, LearningState] = {}
        self._learned_creator_outcomes: dict[str, list[bool]] = {}
        self._authorized_plain: Mapping[tuple[str, str], tuple[int, str]] = MappingProxyType({})
        self.learning_path = Path(os.getenv("E4_CREATOR_LEARNING_PATH", "var/e4/e4-creator-learning.json"))
        self.discovery_queue_path = Path(os.getenv("E4_DISCOVERY_QUEUE_PATH", "var/e4/e4-discovery-queue.jsonl"))
        self.direct_ca_max_age_ns = int(float(os.getenv("E4_DIRECT_CA_MAX_AGE_MS", "1500")) * 1_000_000)
        self.copy_max_age_ns = int(float(os.getenv("E4_COPY_MAX_AGE_MS", "100")) * 1_000_000)

    def __call__(self) -> "PipelineManager":
        return self

    @staticmethod
    def _float(value: Any, default: float = 0.0) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return default
        return number if math.isfinite(number) else default

    def reload_models(self) -> None:
        self.creators.reload()

    def creator_identity(self, creator: str | None) -> CreatorIdentity | None:
        profile = self.creators.lookup(creator)
        if profile is None:
            return None
        return CreatorIdentity(
            creator=profile.creator,
            status=profile.tier.name,
            score=profile.score,
            wins=profile.wins,
            losses=profile.losses,
            trades=profile.trades,
            gross_win_rate=profile.gross_win_rate,
            source=profile.source,
        )

    def register_authorized_intent(self, payload: Mapping[str, Any]) -> Any:
        # Signed external intents use the HMAC registry. Internal cooperating
        # launchers can explicitly set authorized=true; those records are still
        # bounded by creator/mint and expiry and consumed once.
        if payload.get("signature") or payload.get("hmac"):
            return self.intents.ingest(payload)
        if not bool(payload.get("authorized") or payload.get("prearmed")):
            raise ValueError("unsigned launch intent must be explicitly authorized")
        creator = str(payload.get("creator") or "")
        mint = str(payload.get("mint") or "")
        if not creator:
            raise ValueError("launch intent requires creator")
        expires_ns = int(payload.get("expires_ns") or 0)
        if expires_ns <= time.time_ns():
            raise ValueError("launch intent is expired")
        source = str(payload.get("source") or "authorized-deployer")
        with self._lock:
            current = dict(self._authorized_plain)
            current[(creator, mint)] = (expires_ns, source)
            self._authorized_plain = MappingProxyType(current)
        return {"creator": creator, "mint": mint, "expires_ns": expires_ns, "source": source}

    def _plain_intent(self, creator: str, mint: str, now_ns: int) -> tuple[int, str] | None:
        for key in ((creator, mint), (creator, "")):
            record = self._authorized_plain.get(key)
            if record and record[0] >= now_ns:
                return record
        return None

    def observe_social_post(self, payload: Mapping[str, Any] | None = None, **kwargs: Any) -> Any:
        row = dict(payload or {})
        row.update(kwargs)
        created_ns = int(row.get("created_ns") or row.get("timestamp_ns") or time.time_ns())
        observed_ns = int(row.get("observed_ns") or time.time_ns())
        handle = str(row.get("handle") or row.get("source_account") or row.get("account") or row.get("source") or "")
        text = str(row.get("text") or row.get("content") or "")
        authority = self._float(row.get("authority"), 0.0)
        if authority <= 0:
            followers = self._float(row.get("followers"), 0.0)
            authority = min(1.0, math.log10(max(1.0, followers)) / 7.0) if followers else 0.0
        signal = self.narratives.observe(
            source=str(row.get("source") or "social-stream"),
            source_account=handle,
            text=text,
            created_ns=created_ns,
            observed_ns=observed_ns,
            authority=authority,
            engagement_velocity=self._float(row.get("engagement_velocity"), 0.0),
            provenance=str(row.get("provenance") or "stream"),
        )
        if signal is not None and signal.contract_addresses:
            with self._lock:
                index = dict(self._social_by_ca)
                for mint in signal.contract_addresses:
                    rows = list(index.get(mint, ()))
                    rows.append(signal)
                    index[mint] = tuple(rows[-16:])
                self._social_by_ca = MappingProxyType(index)
        return signal

    def observe_e4_entry(self, payload: Mapping[str, Any]) -> E4Signal | None:
        mint = str(payload.get("mint") or "")
        if not mint:
            return None
        observed_ns = int(payload.get("observed_ns") or payload.get("received_ns") or time.time_ns())
        tokens = max(0.0, self._float(payload.get("token_amount") or payload.get("tokens")))
        signal = E4Signal(
            mint=mint,
            creator=str(payload.get("creator") or ""),
            observed_ns=observed_ns,
            entry_price_sol=max(0.0, self._float(payload.get("entry_price_sol") or payload.get("price_sol"))),
            entry_sol=max(0.0, self._float(payload.get("entry_sol") or payload.get("sol_amount"))),
            signature=str(payload.get("signature") or ""),
            entry_tokens=tokens,
            remaining_tokens=tokens,
        )
        with self._lock:
            current = dict(self._e4_entries)
            old = current.get(mint)
            if old is not None and old.signature and old.signature == signal.signature:
                return old
            current[mint] = signal
            self._e4_entries = MappingProxyType(current)
        try:
            from .e4_pipelines_v10 import E4Observation
            observation = E4Observation(
                observation_id=hashlib.blake2s(f"{signal.signature}|{mint}".encode(), digest_size=16).hexdigest(),
                mint=mint,
                creator=signal.creator or None,
                signature=signal.signature or None,
                observed_ns=observed_ns,
                sol_amount=signal.entry_sol or None,
                source=str(payload.get("source") or "e4-wallet"),
            )
            self.teacher.observe(observation)
        except Exception:
            pass
        return signal

    def observe_e4_exit(
        self,
        mint: str,
        *,
        token_amount: float = 0.0,
        sell_fraction: float = 0.0,
        fully_exited: bool = False,
        observed_ns: int | None = None,
        signature: str = "",
    ) -> E4Signal | None:
        existing = self._e4_entries.get(str(mint))
        if existing is None:
            return None
        if signature and existing.last_sell_signature == signature:
            return existing
        before = max(0.0, existing.remaining_tokens or existing.entry_tokens)
        sold_tokens = max(0.0, self._float(token_amount))
        fraction = min(1.0, max(0.0, self._float(sell_fraction)))
        if fraction <= 0 and sold_tokens > 0 and before > 0:
            fraction = min(1.0, sold_tokens / before)
        if fraction <= 0 and not fully_exited:
            # A wallet sell notification without token accounting is still an
            # exit signal. Treat first unknown sell conservatively as partial.
            fraction = 0.30 if existing.sell_count == 0 else 1.0
        remaining = max(0.0, before - sold_tokens) if sold_tokens > 0 else before * (1.0 - fraction)
        complete = bool(fully_exited or fraction >= 0.985 or (existing.sell_count >= 1 and fraction >= 0.50))
        if existing.entry_tokens > 0 and remaining <= existing.entry_tokens * 1e-6:
            complete = True
        updated = E4Signal(
            mint=existing.mint,
            creator=existing.creator,
            observed_ns=existing.observed_ns,
            entry_price_sol=existing.entry_price_sol,
            entry_sol=existing.entry_sol,
            signature=existing.signature,
            entry_tokens=existing.entry_tokens,
            remaining_tokens=0.0 if complete else remaining,
            last_sell_fraction=fraction,
            last_sell_ns=int(observed_ns or time.time_ns()),
            last_sell_signature=signature,
            sell_count=existing.sell_count + 1,
            fully_exited=complete,
            sold=True,
        )
        with self._lock:
            current = dict(self._e4_entries)
            current[mint] = updated
            self._e4_entries = MappingProxyType(current)
        return updated

    def e4_signal(self, mint: str) -> E4Signal | None:
        return self._e4_entries.get(str(mint))

    def observe_launch_event(
        self,
        *,
        mint: str,
        creator: str,
        received_ns: int,
        price_sol: float,
        **_: Any,
    ) -> None:
        price = max(0.0, self._float(price_sol))
        self._learning[mint] = LearningState(
            mint=mint,
            creator=creator,
            launched_ns=int(received_ns),
            last_event_ns=int(received_ns),
            entry_price_sol=price,
            latest_price_sol=price,
            peak_price_sol=price,
        )

    def observe_trade_event(
        self,
        *,
        mint: str,
        received_ns: int,
        price_sol: float,
        is_buy: bool,
        **_: Any,
    ) -> None:
        row = self._learning.get(mint)
        if row is None:
            return
        price = max(0.0, self._float(price_sol))
        row.last_event_ns = max(row.last_event_ns, int(received_ns))
        row.latest_price_sol = price or row.latest_price_sol
        row.peak_price_sol = max(row.peak_price_sol, price)
        if not is_buy:
            row.sell_count += 1

    def _promote_learned_creator(self, creator: str) -> None:
        from .e4_pipelines_v10 import CreatorProfile, CreatorSnapshot, CreatorTier

        outcomes = self._learned_creator_outcomes.get(creator, [])
        trades = len(outcomes)
        wins = sum(1 for row in outcomes if row)
        losses = trades - wins
        rate = wins / trades if trades else 0.0
        if trades >= 5 and rate >= 0.70:
            tier = CreatorTier.ELITE
            score = min(0.98, 0.86 + 0.12 * rate)
        elif trades >= 3 and rate >= 0.60:
            tier = CreatorTier.APPROVED
            score = min(0.90, 0.72 + 0.18 * rate)
        elif trades >= 3 and rate <= 0.25:
            tier = CreatorTier.NEGATIVE
            score = 0.05
        else:
            tier = CreatorTier.WATCH
            score = 0.58
        learned = CreatorProfile(
            creator=creator,
            tier=tier,
            score=score,
            wins=wins,
            losses=losses,
            trades=trades,
            gross_win_rate=rate,
            source="v11-observed-launch-history",
            evidence=(f"observed:{wins}W/{losses}L",),
            updated_ns=time.time_ns(),
        )
        current = dict(self.creators._snapshot.profiles)
        existing = current.get(creator)
        if existing is None or learned.tier > existing.tier or learned.trades >= existing.trades:
            current[creator] = learned
            self.creators._snapshot = CreatorSnapshot(MappingProxyType(current), time.time_ns(), "v11-learning")

    def finalize_stale_learning(
        self,
        *,
        now_ns: int | None = None,
        max_age_seconds: float = 300.0,
        quiet_seconds: float = 30.0,
    ) -> int:
        now = int(now_ns or time.time_ns())
        max_age_ns = int(max_age_seconds * 1e9)
        quiet_ns = int(quiet_seconds * 1e9)
        finalized = 0
        touched: set[str] = set()
        for row in tuple(self._learning.values()):
            if row.finalized:
                continue
            old_enough = now - row.launched_ns >= max_age_ns
            quiet = now - row.last_event_ns >= quiet_ns
            if not (old_enough or quiet):
                continue
            row.finalized = True
            # A 2x peak is a clear runner under the discovery model. Smaller
            # moves can still become profitable in the trading replay but are
            # not enough on their own to whitelist a developer.
            won = row.peak_multiple >= 2.0
            self._learned_creator_outcomes.setdefault(row.creator, []).append(won)
            touched.add(row.creator)
            finalized += 1
        for creator in touched:
            self._promote_learned_creator(creator)
        if finalized:
            self._persist_learning()
        return finalized

    def _persist_learning(self) -> None:
        try:
            self.learning_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": "v11",
                "updated_ns": time.time_ns(),
                "creators": {
                    creator: {
                        "trades": len(rows),
                        "wins": sum(1 for row in rows if row),
                        "losses": sum(1 for row in rows if not row),
                    }
                    for creator, rows in self._learned_creator_outcomes.items()
                },
            }
            self.learning_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        except OSError:
            pass

    def decide_launch(
        self,
        *,
        mint: str,
        creator: str,
        name: str = "",
        symbol: str = "",
        metadata_uri: str = "",
        launch_ns: int,
        now_ns: int | None = None,
        fdv_usd: float = 0.0,
        creator_buy_sol: float = 0.0,
        sell_count: int = 0,
        price_sol: float = 0.0,
        e4_confirmed: bool = False,
        e4_observed_ns: int = 0,
        e4_entry_price: float = 0.0,
        **_: Any,
    ) -> PipelineDecision:
        started = time.perf_counter_ns()
        now = int(now_ns or time.time_ns())
        evidence: dict[str, Any] = {}

        plain = self._plain_intent(creator, mint, now)
        signed = self.intents.match(creator, mint, now)
        if plain is not None or signed is not None:
            if signed is not None:
                self.intents.consume(signed.intent_id)
            decision_ns = time.perf_counter_ns() - started
            return PipelineDecision(True, "authorized_prearmed_launch", 0.97, 0.10,
                                    "authorized_prearmed_launch identity authority", evidence, decision_ns)

        profile = self.creators.lookup(creator)
        if profile is not None:
            evidence["creator_tier"] = profile.tier.name
            evidence["creator_score"] = profile.score
            if profile.negative:
                decision_ns = time.perf_counter_ns() - started
                return PipelineDecision(False, "negative_creator", 0.0, 0.0,
                                        "negative creator history identity veto", evidence, decision_ns)
            if profile.tier.name == "ELITE":
                decision_ns = time.perf_counter_ns() - started
                return PipelineDecision(True, "elite_recurring_creator", max(0.94, profile.score), 0.05,
                                        "elite_recurring_creator identity fast path", evidence, decision_ns)
            if profile.approved:
                fraction = 0.03 if profile.trades >= 3 else 0.0185
                family = "proven_repeat_creator" if profile.trades >= 3 else "prior_e4_winning_creator"
                decision_ns = time.perf_counter_ns() - started
                return PipelineDecision(True, family, max(0.82, profile.score), fraction,
                                        f"{family} identity fast path", evidence, decision_ns)

        # Exact contract-address posts are the only social signal allowed after
        # creation, and only for a very short, unsold, low-FDV window.
        if sell_count == 0 and (not fdv_usd or fdv_usd <= 15_000):
            for signal in self._social_by_ca.get(mint, ()):
                age = now - int(signal.created_ns)
                if signal.created_ns >= launch_ns and 0 <= age <= self.direct_ca_max_age_ns and signal.authority >= 0.90:
                    evidence["social_authority"] = signal.authority
                    evidence["social_signal_id"] = signal.signal_id
                    decision_ns = time.perf_counter_ns() - started
                    return PipelineDecision(True, "exact_ca_social_launch", 0.93, 0.03,
                                            "exact_ca_social_launch identity-linked social authority", evidence, decision_ns)

        match = self.narratives.match_launch(
            name=name,
            symbol=symbol,
            uri=metadata_uri,
            mint=mint,
            launch_ns=launch_ns,
        )
        if match.matched and sell_count == 0:
            evidence["narrative_match"] = True
            evidence["narrative_score"] = match.score
            decision_ns = time.perf_counter_ns() - started
            return PipelineDecision(True, "preannounced_social_community_launch", max(0.86, match.score), 0.03,
                                    "preannounced social identity/narrative authority", evidence, decision_ns)

        source = self.e4_signal(mint)
        if source is not None:
            e4_confirmed = not source.fully_exited
            e4_observed_ns = source.observed_ns
            e4_entry_price = source.entry_price_sol
        if e4_confirmed:
            age = max(0, now - int(e4_observed_ns or now))
            drift = 0.0
            if e4_entry_price > 0 and price_sol > 0:
                drift = max(0.0, price_sol / e4_entry_price - 1.0)
            if age <= self.copy_max_age_ns and drift <= 0.08 and sell_count == 0:
                evidence["e4_age_ms"] = age / 1e6
                evidence["e4_price_drift"] = drift
                decision_ns = time.perf_counter_ns() - started
                return PipelineDecision(True, "e4_confirmed_fast_copy", 0.86, 0.0185,
                                        "copy_safe fresh E4 identity confirmation", evidence, decision_ns)

        decision_ns = time.perf_counter_ns() - started
        return PipelineDecision(False, "identity_only_reject", 0.0, 0.0,
                                "identity-only gate: no approved creator, prelaunch narrative, or fresh E4 confirmation",
                                evidence, decision_ns)
