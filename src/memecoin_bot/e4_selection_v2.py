from __future__ import annotations

import json
import math
import os
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path
from collections.abc import Mapping, Sequence
from typing import Any

from . import e4_live as core

VERSION = "e4-unified-selection-v2"
_MEMORY_VERSION = "e4-selection-memory-v2"


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _saturating(value: float, scale: float) -> float:
    if value <= 0 or scale <= 0:
        return 0.0
    return 1.0 - math.exp(-value / scale)


def _wilson(wins: int, trials: int, z: float = 1.96) -> tuple[float, float]:
    if trials <= 0:
        return 0.0, 1.0
    p = wins / trials
    denom = 1.0 + z * z / trials
    centre = p + z * z / (2.0 * trials)
    spread = z * math.sqrt((p * (1.0 - p) + z * z / (4.0 * trials)) / trials)
    return max(0.0, (centre - spread) / denom), min(1.0, (centre + spread) / denom)


def _posterior_mean(wins: int, losses: int, alpha: float = 2.0, beta: float = 2.0) -> float:
    return (max(0, wins) + alpha) / (max(0, wins) + max(0, losses) + alpha + beta)


def _sample_confidence(trades: int, full_confidence_at: int = 30) -> float:
    if trades <= 0:
        return 0.0
    return _clamp(math.log1p(trades) / math.log1p(full_confidence_at))


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n")
    tmp.replace(path)


@dataclass(frozen=True, slots=True)
class CreatorAssessment:
    creator: str
    historical_trades: int
    historical_wins: int
    posterior_mean: float
    wilson_lower: float
    wilson_upper: float
    confidence: float
    score_delta: float
    robust_positive: bool
    robust_negative: bool
    discovered_score: float
    weak_registry_wins: int
    reason: str


@dataclass(frozen=True, slots=True)
class BuyerAssessment:
    wallets_seen: int
    qualified_wallets: int
    posterior_mean: float
    confidence: float
    score_delta: float
    strongly_negative_wallets: int


@dataclass(frozen=True, slots=True)
class RegimeAssessment:
    observations: int
    win_rate: float | None
    mean_return: float | None
    consecutive_losses: int
    threshold_add: float
    size_multiplier: float
    state: str


@dataclass(frozen=True, slots=True)
class SelectionDecision:
    accepted: bool
    score: float
    threshold: float
    fraction: float
    tier: str
    reason: str
    components: dict[str, float]
    creator: CreatorAssessment
    buyers: BuyerAssessment
    regime: RegimeAssessment


class OnlineMemory:
    """Restart-safe causal memory.

    It only learns after a trade has resolved. Pending entry evidence is persisted
    before execution so a process restart cannot silently turn future outcome
    updates into look-ahead or lose the buyer/creator context used at decision time.
    """

    def __init__(self, path: Path, *, persist: bool = True, recent_limit: int = 100) -> None:
        self.path = path
        self.persist = persist
        self.recent_limit = recent_limit
        self.lock = threading.RLock()
        self.buyers: dict[str, dict[str, Any]] = {}
        self.creators: dict[str, dict[str, Any]] = {}
        self.pending: dict[str, dict[str, Any]] = {}
        self.resolved_mints: set[str] = set()
        self.recent_returns: deque[float] = deque(maxlen=recent_limit)
        self._load()

    @staticmethod
    def _empty_stat() -> dict[str, Any]:
        return {"resolved": 0, "wins": 0, "losses": 0, "return_sum": 0.0, "last_ns": 0}

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return
        if payload.get("version") != _MEMORY_VERSION:
            return
        self.buyers = {
            str(key): dict(value)
            for key, value in (payload.get("buyers") or {}).items()
            if isinstance(value, Mapping)
        }
        self.creators = {
            str(key): dict(value)
            for key, value in (payload.get("creators") or {}).items()
            if isinstance(value, Mapping)
        }
        self.pending = {
            str(key): dict(value)
            for key, value in (payload.get("pending") or {}).items()
            if isinstance(value, Mapping)
        }
        self.resolved_mints = {str(x) for x in payload.get("resolved_mints") or []}
        for value in (payload.get("recent_returns") or [])[-self.recent_limit :]:
            number = _finite(value, float("nan"))
            if math.isfinite(number):
                self.recent_returns.append(number)

    def snapshot(self) -> dict[str, Any]:
        return {
            "version": _MEMORY_VERSION,
            "buyers": self.buyers,
            "creators": self.creators,
            "pending": self.pending,
            "resolved_mints": sorted(self.resolved_mints)[-50_000:],
            "recent_returns": list(self.recent_returns),
            "updated_ns": time.time_ns(),
        }

    def _save(self) -> None:
        if self.persist:
            _atomic_json(self.path, self.snapshot())

    def register_entry(
        self,
        mint: str,
        *,
        creator: str,
        buyers: Sequence[str],
        score: float,
        decision_ns: int,
    ) -> None:
        if not mint:
            return
        with self.lock:
            self.pending[mint] = {
                "creator": creator,
                "buyers": sorted({str(x) for x in buyers if x}),
                "score": _clamp(score),
                "decision_ns": int(decision_ns),
            }
            self._save()

    def _update_stat(self, table: dict[str, dict[str, Any]], key: str, result: float) -> None:
        if not key:
            return
        row = table.setdefault(key, self._empty_stat())
        row["resolved"] = _int(row.get("resolved")) + 1
        row["wins"] = _int(row.get("wins")) + int(result > 0)
        row["losses"] = _int(row.get("losses")) + int(result <= 0)
        row["return_sum"] = _finite(row.get("return_sum")) + result
        row["last_ns"] = time.time_ns()

    def resolve(self, mint: str, return_fraction: float) -> bool:
        result = _finite(return_fraction, float("nan"))
        if not mint or not math.isfinite(result):
            return False
        with self.lock:
            if mint in self.resolved_mints:
                return False
            evidence = self.pending.pop(mint, None)
            if evidence is None:
                return False
            creator = str(evidence.get("creator") or "")
            buyers = [str(x) for x in evidence.get("buyers") or []]
            self._update_stat(self.creators, creator, result)
            for wallet in buyers:
                self._update_stat(self.buyers, wallet, result)
            self.resolved_mints.add(mint)
            self.recent_returns.append(result)
            self._save()
            return True

    def creator_stat(self, creator: str) -> Mapping[str, Any]:
        return self.creators.get(creator, {})

    def buyer_assessment(self, buyers: Sequence[str]) -> BuyerAssessment:
        unique = sorted({str(x) for x in buyers if x})
        weighted = 0.0
        total_weight = 0.0
        qualified = 0
        negative = 0
        for wallet in unique:
            row = self.buyers.get(wallet) or {}
            resolved = _int(row.get("resolved"))
            wins = _int(row.get("wins"))
            losses = _int(row.get("losses"))
            if resolved < 3:
                continue
            posterior = _posterior_mean(wins, losses)
            confidence = _sample_confidence(resolved, 20)
            weight = max(0.05, confidence)
            weighted += posterior * weight
            total_weight += weight
            qualified += 1
            _, upper = _wilson(wins, resolved)
            if resolved >= 6 and upper <= 0.45:
                negative += 1
        posterior = weighted / total_weight if total_weight else 0.5
        coverage = qualified / max(len(unique), 1)
        confidence = _clamp(coverage * min(1.0, total_weight / 3.0))
        delta = (posterior - 0.5) * 0.20 * confidence
        if negative:
            delta -= min(0.08, negative * 0.025)
        return BuyerAssessment(
            wallets_seen=len(unique),
            qualified_wallets=qualified,
            posterior_mean=posterior,
            confidence=confidence,
            score_delta=max(-0.12, min(0.10, delta)),
            strongly_negative_wallets=negative,
        )

    def regime(self) -> RegimeAssessment:
        values = list(self.recent_returns)
        if not values:
            return RegimeAssessment(0, None, None, 0, 0.0, 1.0, "UNOBSERVED")
        tail = values[-20:]
        wins = sum(x > 0 for x in tail)
        wr = wins / len(tail)
        mean = sum(tail) / len(tail)
        consecutive_losses = 0
        for value in reversed(tail):
            if value > 0:
                break
            consecutive_losses += 1
        threshold_add = 0.0
        size_multiplier = 1.0
        state = "NORMAL"
        if len(tail) >= 10 and (mean <= -0.08 or wr <= 0.25):
            threshold_add = 0.08
            size_multiplier = 0.40
            state = "DEFENSIVE"
        elif len(tail) >= 10 and (mean <= -0.03 or wr <= 0.35):
            threshold_add = 0.04
            size_multiplier = 0.65
            state = "CAUTIOUS"
        if consecutive_losses >= 5:
            threshold_add = max(threshold_add, 0.03)
            size_multiplier = min(size_multiplier, 0.70)
            state = "LOSS_STREAK" if state == "NORMAL" else state
        return RegimeAssessment(
            observations=len(tail),
            win_rate=wr,
            mean_return=mean,
            consecutive_losses=consecutive_losses,
            threshold_add=threshold_add,
            size_multiplier=size_multiplier,
            state=state,
        )


class CreatorLibrary:
    """Confidence-calibrated creator context.

    Historical creator data can boost, penalise or veto. It never grants entry
    permission on its own. Tiny samples are deliberately shrunk toward neutral.
    """

    def __init__(
        self,
        expectancy_path: Path,
        winners_path: Path,
        discovered_path: Path,
        memory: OnlineMemory,
    ) -> None:
        self.expectancy_path = expectancy_path
        self.winners_path = winners_path
        self.discovered_path = discovered_path
        self.memory = memory
        self.expectancy: dict[str, dict[str, Any]] = {}
        self.winners: dict[str, dict[str, Any]] = {}
        self.discovered: dict[str, dict[str, Any]] = {}
        self.watchlist: dict[str, dict[str, Any]] = {}
        self._load()

    @staticmethod
    def _json(path: Path) -> Mapping[str, Any]:
        if not path.exists():
            return {}
        try:
            value = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, Mapping) else {}

    def _load(self) -> None:
        expectancy = self._json(self.expectancy_path)
        for row in expectancy.get("top_creators") or []:
            if not isinstance(row, Mapping):
                continue
            creator = str(row.get("creator") or "")
            if creator:
                self.expectancy[creator] = dict(row)
        winners = self._json(self.winners_path)
        self.winners = {
            str(k): dict(v)
            for k, v in (winners.get("creators") or {}).items()
            if isinstance(v, Mapping)
        }
        discovered = self._json(self.discovered_path)
        self.discovered = {
            str(k): dict(v)
            for k, v in (discovered.get("creators") or {}).items()
            if isinstance(v, Mapping)
        }
        self.watchlist = {
            str(k): dict(v)
            for k, v in (discovered.get("watchlist") or {}).items()
            if isinstance(v, Mapping)
        }

    def assess(self, creator: str) -> CreatorAssessment:
        historical = self.expectancy.get(creator) or {}
        runtime = self.memory.creator_stat(creator)
        h_wins = _int(historical.get("wins"))
        h_losses = _int(historical.get("losses"))
        r_wins = _int(runtime.get("wins"))
        r_losses = _int(runtime.get("losses"))
        wins = h_wins + r_wins
        losses = h_losses + r_losses
        trades = wins + losses
        posterior = _posterior_mean(wins, losses)
        lower, upper = _wilson(wins, trades)
        confidence = _sample_confidence(trades)
        robust_positive = trades >= 5 and lower >= 0.55
        robust_negative = trades >= 8 and upper <= 0.50
        delta = 0.0

        if robust_positive:
            delta += min(0.16, (posterior - 0.50) * 0.32 * max(0.45, confidence))
        elif trades > 0:
            # Tiny histories are context only. Three wins cannot become permission.
            delta += max(-0.03, min(0.03, (posterior - 0.50) * 0.10 * confidence))

        if robust_negative:
            delta -= min(0.18, (0.55 - posterior) * 0.30 + 0.05)

        weak_wins = _int((self.winners.get(creator) or {}).get("e4_observed_wins"))
        if creator not in self.expectancy:
            delta += min(0.04, weak_wins * 0.012)

        discovered_score = _finite((self.discovered.get(creator) or {}).get("score"), 0.5)
        if creator in self.discovered:
            delta += max(-0.04, min(0.04, (discovered_score - 0.50) * 0.08))
        if creator in self.watchlist:
            delta -= 0.04

        reason = "neutral_creator_context"
        if robust_negative:
            reason = "robust_negative_creator"
        elif robust_positive:
            reason = "robust_positive_creator"
        elif trades:
            reason = "thin_creator_history"

        return CreatorAssessment(
            creator=creator,
            historical_trades=trades,
            historical_wins=wins,
            posterior_mean=posterior,
            wilson_lower=lower,
            wilson_upper=upper,
            confidence=confidence,
            score_delta=max(-0.22, min(0.18, delta)),
            robust_positive=robust_positive,
            robust_negative=robust_negative,
            discovered_score=discovered_score if creator in self.discovered else 0.0,
            weak_registry_wins=weak_wins,
            reason=reason,
        )


@dataclass(frozen=True, slots=True)
class SelectionConfig:
    threshold: float = 0.62
    minimum_flow_buy_sol: float = 0.06
    minimum_flow_buyers: int = 2
    minimum_flow_ratio: float = 1.10
    prearmed_minimum_buy_sol: float = 0.02
    prearmed_minimum_buyers: int = 1
    maximum_position_fraction: float = 0.10

    @classmethod
    def from_env(cls) -> SelectionConfig:
        return cls(
            threshold=_finite(os.getenv("E4_SELECTION_V2_THRESHOLD"), 0.62),
            minimum_flow_buy_sol=_finite(os.getenv("E4_SELECTION_V2_MIN_FLOW_SOL"), 0.06),
            minimum_flow_buyers=_int(os.getenv("E4_SELECTION_V2_MIN_BUYERS"), 2),
            minimum_flow_ratio=_finite(os.getenv("E4_SELECTION_V2_MIN_RATIO"), 1.10),
            prearmed_minimum_buy_sol=_finite(
                os.getenv("E4_SELECTION_V2_PREARMED_MIN_FLOW_SOL"), 0.02
            ),
            prearmed_minimum_buyers=_int(
                os.getenv("E4_SELECTION_V2_PREARMED_MIN_BUYERS"), 1
            ),
            maximum_position_fraction=_finite(
                os.getenv("E4_SELECTION_V2_MAX_POSITION_FRACTION"), 0.10
            ),
        )


class UnifiedE4Policy(core.E4Policy):
    """Actual production E4 signal + causal memory + calibrated creator context."""

    def __init__(self, settings: core.Settings):
        super().__init__(settings)
        self.enabled = os.getenv("E4_SELECTION_V2_ENABLED", "true").lower() not in {
            "0",
            "false",
            "no",
            "off",
        }
        self.config = SelectionConfig.from_env()
        persist = os.getenv("E4_SELECTION_MEMORY_PERSIST", "true").lower() not in {
            "0",
            "false",
            "no",
            "off",
        }
        self.memory = OnlineMemory(
            Path(os.getenv("E4_SELECTION_MEMORY_PATH", "data/e4-selection-memory-v2.json")),
            persist=persist,
        )
        self.library = CreatorLibrary(
            Path(
                os.getenv(
                    "E4_CREATOR_EXPECTANCY_PATH", "models/e4/e4-creator-expectancy.json"
                )
            ),
            Path(os.getenv("E4_WINNING_CREATORS_PATH", "models/e4/e4-winning-creators.json")),
            Path(
                os.getenv(
                    "E4_DISCOVERED_CREATORS_PATH", "models/e4/e4-discovered-creators.json"
                )
            ),
            self.memory,
        )
        self.size_tiers = {
            "probe": 0.0075,
            "standard": 0.0125,
            "strong": 0.0185,
            "high": 0.03,
            "elite": 0.05,
            "exceptional": 0.10,
        }
        self._load_size_tiers()

    def _load_size_tiers(self) -> None:
        path = Path(os.getenv("E4_SELECTION_PROFILE_PATH", "models/e4/e4-selection-v2.json"))
        if not path.exists():
            return
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return
        tiers = payload.get("relative_size_tiers") or {}
        for name in tuple(self.size_tiers):
            value = _finite(tiers.get(name), self.size_tiers[name])
            if 0 < value <= 0.20:
                self.size_tiers[name] = value

    def _public_buyers(self, state: core.TokenState, milliseconds: int = 1000) -> list[str]:
        cutoff = state.latest_ns - milliseconds * 1_000_000
        buyers: list[str] = []
        for event in reversed(state.events):
            if event.source_ns < cutoff:
                break
            if event.kind not in {core.EventKind.BUY, core.EventKind.PUMPSWAP_BUY}:
                continue
            wallet = str(event.trader or "")
            if not wallet or wallet == state.creator or wallet == self.settings.wallet:
                continue
            if wallet not in buyers:
                buyers.append(wallet)
        return buyers

    def _model_score(self, features: Mapping[str, float], fdv: float) -> float:
        if self.model:
            logit = float(self.model.get("intercept", 0.0)) + sum(
                float(coef) * _finite(features.get(name))
                for name, coef in self.model.get("coefficients", {}).items()
            )
            return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, logit))))
        flow = _finite(features.get("buy_sol_1000ms"))
        buyers = _finite(features.get("buyers_1000ms"))
        ratio = _finite(features.get("ratio_1000ms"))
        fdv_score = max(
            0.0,
            1.0
            - abs(fdv - self.settings.target_entry_fdv_usd)
            / max(self.settings.target_entry_fdv_usd, 1.0),
        )
        return _clamp(
            0.28 * fdv_score
            + 0.24 * _saturating(buyers, 4.0)
            + 0.30 * _saturating(flow, 1.0)
            + 0.18 * _clamp(math.log1p(max(ratio, 0.0)) / math.log(6.0))
        )

    def _flow_score(self, state: core.TokenState, buyers: Sequence[str]) -> float:
        flow250 = state.flow(250)
        flow1s = state.flow(1000)
        ratio1s = 8.0 if math.isinf(flow1s.ratio) else max(0.0, flow1s.ratio)
        ratio250 = 8.0 if math.isinf(flow250.ratio) else max(0.0, flow250.ratio)
        return _clamp(
            0.28 * _saturating(len(buyers), 4.0)
            + 0.28 * _saturating(flow1s.buy_sol, 1.0)
            + 0.16 * _saturating(max(flow250.net, 0.0), 0.25)
            + 0.16 * _clamp(math.log1p(ratio1s) / math.log(6.0))
            + 0.12 * _clamp(math.log1p(ratio250) / math.log(6.0))
        )

    def _fdv_score(self, fdv: float) -> float:
        target = max(self.settings.target_entry_fdv_usd, 1.0)
        distance = abs(math.log(max(fdv, 1.0) / target))
        return math.exp(-distance)

    def _tier(self, score: float) -> str:
        if score >= 0.90:
            return "exceptional"
        if score >= 0.83:
            return "elite"
        if score >= 0.76:
            return "high"
        if score >= 0.70:
            return "strong"
        if score >= 0.65:
            return "standard"
        return "probe"

    def decision(self, state: core.TokenState) -> SelectionDecision:
        neutral_creator = self.library.assess(str(state.creator or ""))
        neutral_buyers = BuyerAssessment(0, 0, 0.5, 0.0, 0.0, 0)
        regime = self.memory.regime()
        threshold = _clamp(self.config.threshold + regime.threshold_add, 0.50, 0.90)

        if state.complete or state.migrated or state.wallet_touched:
            return SelectionDecision(
                False,
                0.0,
                threshold,
                0.0,
                "none",
                "not an untouched live Pump curve",
                {},
                neutral_creator,
                neutral_buyers,
                regime,
            )
        fdv = _finite(state.fdv_usd)
        if fdv <= 0 or fdv > self.settings.max_entry_fdv_usd:
            return SelectionDecision(
                False,
                0.0,
                threshold,
                0.0,
                "none",
                "outside observed E4 entry FDV",
                {},
                neutral_creator,
                neutral_buyers,
                regime,
            )

        features = state.features()
        buyers = self._public_buyers(state)
        creator = self.library.assess(str(state.creator or ""))
        buyer_assessment = self.memory.buyer_assessment(buyers)
        model_score = self._model_score(features, fdv)
        flow_score = self._flow_score(state, buyers)
        fdv_score = self._fdv_score(fdv)

        score = _clamp(
            0.48 * model_score
            + 0.30 * flow_score
            + 0.12 * fdv_score
            + 0.10 * buyer_assessment.posterior_mean
            + creator.score_delta
            + buyer_assessment.score_delta
        )
        components = {
            "model_score": model_score,
            "flow_score": flow_score,
            "fdv_score": fdv_score,
            "creator_delta": creator.score_delta,
            "buyer_delta": buyer_assessment.score_delta,
            "buyer_posterior": buyer_assessment.posterior_mean,
            "creator_posterior": creator.posterior_mean,
            "regime_threshold_add": regime.threshold_add,
        }

        if creator.robust_negative:
            return SelectionDecision(
                False,
                score,
                threshold,
                0.0,
                "none",
                "robust negative creator veto",
                components,
                creator,
                buyer_assessment,
                regime,
            )

        flow1s = state.flow(1000)
        ratio = 8.0 if math.isinf(flow1s.ratio) else max(0.0, flow1s.ratio)
        normal_flow = (
            len(buyers) >= self.config.minimum_flow_buyers
            and flow1s.buy_sol >= self.config.minimum_flow_buy_sol
            and ratio >= self.config.minimum_flow_ratio
        )
        # High-confidence repeat creators may enter earlier, but creator history
        # still cannot bypass all live confirmation.
        prearmed_flow = (
            creator.robust_positive
            and creator.confidence >= 0.60
            and len(buyers) >= self.config.prearmed_minimum_buyers
            and flow1s.buy_sol >= self.config.prearmed_minimum_buy_sol
        )
        if not normal_flow and not prearmed_flow:
            return SelectionDecision(
                False,
                score,
                threshold,
                0.0,
                "none",
                "live flow confirmation missing",
                components,
                creator,
                buyer_assessment,
                regime,
            )
        if buyer_assessment.strongly_negative_wallets >= 2 and not creator.robust_positive:
            return SelectionDecision(
                False,
                score,
                threshold,
                0.0,
                "none",
                "negative buyer cohort veto",
                components,
                creator,
                buyer_assessment,
                regime,
            )
        if score < threshold:
            return SelectionDecision(
                False,
                score,
                threshold,
                0.0,
                "none",
                "unified E4 score below threshold",
                components,
                creator,
                buyer_assessment,
                regime,
            )

        tier = self._tier(score)
        fraction = min(
            self.settings.max_position_fraction,
            self.config.maximum_position_fraction,
            self.size_tiers[tier] * regime.size_multiplier,
        )
        return SelectionDecision(
            True,
            score,
            threshold,
            max(0.0, fraction),
            tier,
            f"unified E4 accepted ({tier})",
            components,
            creator,
            buyer_assessment,
            regime,
        )

    def entry(self, state: core.TokenState) -> tuple[bool, float, float, str, dict[str, float]]:
        if not self.enabled:
            return super().entry(state)
        decision = self.decision(state)
        features = dict(state.features())
        features.update(decision.components)
        features["selection_threshold"] = decision.threshold
        features["position_fraction"] = decision.fraction
        if decision.accepted:
            buyers = self._public_buyers(state)
            self.memory.register_entry(
                state.mint,
                creator=str(state.creator or ""),
                buyers=buyers,
                score=decision.score,
                decision_ns=state.latest_ns,
            )
        return (
            decision.accepted,
            decision.score,
            decision.fraction,
            decision.reason,
            features,
        )

    def observe_outcome(self, mint: str, return_fraction: float) -> bool:
        return self.memory.resolve(mint, return_fraction)

    def exit(self, position: core.Position, state: core.TokenState) -> tuple[str, float, str]:
        action, fraction, reason = super().exit(position, state)
        if action != "HOLD":
            return action, fraction, reason
        price = state.price_sol or position.last_price
        if not price or position.entry_price <= 0:
            return action, fraction, reason
        markout = position.markout_bps(price)
        flow250 = state.flow(250)
        flow1s = state.flow(1000)
        ratio250 = 8.0 if math.isinf(flow250.ratio) else flow250.ratio

        if markout <= -2800:
            return "SELL_ALL", 1.0, "V2.1 catastrophic loss ceiling"
        if position.age_ms <= 2000 and flow250.net < 0 and ratio250 < 0.70 and markout <= -75:
            return "SELL_ALL", 1.0, "V2.1 early adverse flow"
        if position.first_partial_done:
            drawdown = position.drawdown_bps(price)
            if flow250.net < 0 and flow1s.ratio < 0.90 and drawdown >= 250:
                return "SELL_ALL", 1.0, "V2.1 post-partial flow breakdown"
            if position.max_price >= position.entry_price * 1.15 and markout <= 100:
                return "SELL_ALL", 1.0, "V2.1 profit-floor protection"
        return action, fraction, reason

    def audit_snapshot(self) -> dict[str, Any]:
        robust_positive = 0
        robust_negative = 0
        thin = 0
        for creator in set(self.library.expectancy) | set(self.library.winners):
            assessment = self.library.assess(creator)
            robust_positive += int(assessment.robust_positive)
            robust_negative += int(assessment.robust_negative)
            thin += int(0 < assessment.historical_trades < 5)
        return {
            "version": VERSION,
            "enabled": self.enabled,
            "config": asdict(self.config),
            "historical_creator_rows": len(self.library.expectancy),
            "weak_winner_registry_rows": len(self.library.winners),
            "discovered_creator_rows": len(self.library.discovered),
            "robust_positive_creators": robust_positive,
            "robust_negative_creators": robust_negative,
            "thin_creator_histories": thin,
            "runtime_memory": {
                "buyers": len(self.memory.buyers),
                "creators": len(self.memory.creators),
                "pending": len(self.memory.pending),
                "resolved_mints": len(self.memory.resolved_mints),
            },
        }


def install(core_module: Any = core) -> None:
    """Install production selection and causal learning exactly once."""
    if getattr(core_module, "_e4_selection_v2_installed", False):
        return
    core_module.E4Policy = UnifiedE4Policy
    original_execute_sell = core_module.Engine.execute_sell

    async def execute_sell_with_learning(
        self: Any, position: core.Position, fraction: float, reason: str
    ) -> None:
        was_closed = position.status == core_module.PositionStatus.CLOSED
        await original_execute_sell(self, position, fraction, reason)
        if was_closed or position.status != core_module.PositionStatus.CLOSED:
            return
        policy = getattr(self, "policy", None)
        observe = getattr(policy, "observe_outcome", None)
        if not callable(observe) or position.entry_sol <= 0:
            return
        realised_return = position.realized_sol / position.entry_sol - 1.0
        observe(position.mint, realised_return)

    core_module.Engine.execute_sell = execute_sell_with_learning
    core_module._e4_selection_v2_installed = True
