from __future__ import annotations

import json
import math
import os
import sys
import threading
import time
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from . import e4_live as core

VERSION = "e4-unified-selection-v2.2"
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
class CoreCandidate:
    score: float
    family: str
    minimum_tier: str


@dataclass(frozen=True, slots=True)
class SelectionDecision:
    accepted: bool
    score: float
    threshold: float
    fraction: float
    tier: str
    family: str
    reason: str
    components: dict[str, float]
    creator: CreatorAssessment
    buyers: BuyerAssessment
    regime: RegimeAssessment


class OnlineMemory:
    """Restart-safe causal memory updated only after a position resolves."""

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

    def discard_pending(self, mint: str) -> bool:
        if not mint:
            return False
        with self.lock:
            existed = self.pending.pop(mint, None) is not None
            if existed:
                self._save()
            return existed

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
    """Confidence-calibrated creator context; never independent entry authority."""

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
    threshold: float = 0.70
    maximum_entry_fdv_usd: float = 8_500.0
    maximum_entry_age_ms: float = 350.0
    minimum_creator_seed_sol: float = 0.025
    maximum_position_fraction: float = 0.10

    @classmethod
    def from_env(cls) -> SelectionConfig:
        return cls(
            threshold=_finite(os.getenv("E4_SELECTION_V2_THRESHOLD"), 0.70),
            maximum_entry_fdv_usd=_finite(
                os.getenv("E4_SELECTION_V2_MAX_ENTRY_FDV_USD"), 8_500.0
            ),
            maximum_entry_age_ms=_finite(
                os.getenv("E4_SELECTION_V2_MAX_ENTRY_AGE_MS"), 350.0
            ),
            minimum_creator_seed_sol=_finite(
                os.getenv("E4_SELECTION_V2_MIN_CREATOR_SEED_SOL"), 0.025
            ),
            maximum_position_fraction=_finite(
                os.getenv("E4_SELECTION_V2_MAX_POSITION_FRACTION"), 0.10
            ),
        )


class UnifiedE4Policy(core.E4Policy):
    """Evidence-backed E4 families + causal memory + calibrated creator context."""

    _TIER_ORDER = ("probe", "standard", "strong", "high", "elite", "exceptional")

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

    @staticmethod
    def _legacy_context(state: core.TokenState) -> Mapping[str, Any]:
        module = sys.modules.get("memecoin_bot.e4_hardening_v6")
        table = getattr(module, "_CONTEXT_BY_MINT", None) if module else None
        if isinstance(table, Mapping):
            row = table.get(state.mint)
            if isinstance(row, Mapping):
                return row
        return {}

    def _creator(self, state: core.TokenState) -> str:
        context = self._legacy_context(state)
        creator = str(state.creator or context.get("creator") or "")
        if not creator:
            create = next(
                (
                    event
                    for event in state.events
                    if event.kind == core.EventKind.CREATE and (event.creator or event.trader)
                ),
                None,
            )
            if create is not None:
                creator = str(create.creator or create.trader or "")
        if creator and not state.creator:
            state.creator = creator
        return creator

    def _fallback_features(self, state: core.TokenState) -> dict[str, float]:
        creator = self._creator(state)
        events = list(state.events)
        buys = [
            event
            for event in events
            if event.kind in {core.EventKind.BUY, core.EventKind.PUMPSWAP_BUY}
        ]
        sells = [
            event
            for event in events
            if event.kind in {core.EventKind.SELL, core.EventKind.PUMPSWAP_SELL}
        ]
        creator_buys = [event for event in buys if creator and event.trader == creator]
        noncreator = [event for event in buys if not creator or event.trader != creator]
        signatures: dict[str, int] = {}
        for event in buys:
            if event.signature:
                signatures[event.signature] = signatures.get(event.signature, 0) + 1
        bundled = sum(count for count in signatures.values() if count > 1)
        first_price = next(
            (event.price_sol for event in events if event.price_sol and event.price_sol > 0),
            None,
        )
        price_multiple = (
            (state.price_sol or 0.0) / first_price
            if first_price and state.price_sol and state.price_sol > 0
            else 0.0
        )
        context = self._legacy_context(state)
        created_ns = state.created_ns or state.latest_ns
        return {
            "age_ms": max(0.0, (state.latest_ns - created_ns) / 1_000_000),
            "fdv_usd": _finite(state.fdv_usd),
            "buy_sol": sum(max(0.0, event.sol_amount) for event in buys),
            "sell_sol": sum(max(0.0, event.sol_amount) for event in sells),
            "buy_count": float(len(buys)),
            "sell_count": float(len(sells)),
            "unique_buyers": float(len({event.trader for event in buys if event.trader})),
            "creator_buy_sol": sum(max(0.0, event.sol_amount) for event in creator_buys),
            "noncreator_buyers": float(
                len({event.trader for event in noncreator if event.trader})
            ),
            "noncreator_buy_sol": sum(max(0.0, event.sol_amount) for event in noncreator),
            "bundled_buys": float(bundled),
            "price_multiple": price_multiple,
            "creator_score": _finite(context.get("creator_score")),
            "source_score": 0.70
            if str(context.get("metadata_host") or "").lower() == "metadata.j7tracker.io"
            or str(context.get("launch_source") or "").lower() == "j7tracker"
            else 0.0,
            "prearmed": 1.0 if context.get("prearmed") else 0.0,
            "mayhem": 1.0 if context.get("mayhem") else 0.0,
        }

    def _legacy_features(self, state: core.TokenState) -> dict[str, float]:
        # Prefer the newest identity-enriched causal feature extractor when the
        # production hardening chain is loaded. Fall back through V6 and finally
        # to a local extractor for isolated tests/tools.
        for module_name, function_name in (
            ("memecoin_bot.e4_hardening_v8", "_identity_features"),
            ("memecoin_bot.e4_hardening_v6", "_entry_features"),
        ):
            module = sys.modules.get(module_name)
            feature_fn = getattr(module, function_name, None) if module else None
            if not callable(feature_fn):
                continue
            try:
                raw = feature_fn(state)
                if isinstance(raw, Mapping):
                    return {
                        str(key): _finite(value)
                        for key, value in raw.items()
                        if isinstance(value, (int, float, bool))
                    }
            except Exception:  # noqa: BLE001 - legacy adapter fails closed to local extractor
                return self._fallback_features(state)
        return self._fallback_features(state)

    def _public_buyers(
        self, state: core.TokenState, creator: str, milliseconds: int = 1000
    ) -> list[str]:
        cutoff = state.latest_ns - milliseconds * 1_000_000
        buyers: list[str] = []
        for event in reversed(state.events):
            if event.source_ns < cutoff:
                break
            if event.kind not in {core.EventKind.BUY, core.EventKind.PUMPSWAP_BUY}:
                continue
            wallet = str(event.trader or "")
            if not wallet or wallet == creator or wallet == self.settings.wallet:
                continue
            if wallet not in buyers:
                buyers.append(wallet)
        return buyers

    def _identity_score(
        self, features: Mapping[str, float], context: Mapping[str, Any]
    ) -> float:
        social = max(
            _finite(context.get("social_authority_score")),
            _finite(context.get("community_score")),
        )
        if not context.get("prelaunch_social"):
            social *= 0.25
        funder = _finite(context.get("funder_score"))
        return _clamp(
            max(
                _finite(features.get("creator_score")),
                _finite(features.get("source_score")),
                social,
                funder,
                1.0 if _finite(features.get("prearmed")) >= 1.0 else 0.0,
            )
        )

    @staticmethod
    def _fdv_fit(fdv: float, target: float = 4_878.0) -> float:
        if fdv <= 0:
            return 0.0
        return math.exp(-abs(math.log(fdv / target)))

    def _core_candidate(
        self,
        features: Mapping[str, float],
        context: Mapping[str, Any],
        identity_score: float,
        creator: CreatorAssessment,
    ) -> CoreCandidate | None:
        fdv_score = self._fdv_fit(_finite(features.get("fdv_usd")))
        creator_seed_score = min(1.0, _finite(features.get("creator_buy_sol")) / 3.0)
        buyer_score = min(1.0, _finite(features.get("noncreator_buyers")) / 6.0)
        capital_score = min(1.0, _finite(features.get("noncreator_buy_sol")) / 12.0)
        acceleration_score = min(
            1.0, max(0.0, _finite(features.get("price_multiple")) - 1.0) / 0.70
        )
        bundle_score = min(1.0, _finite(features.get("bundled_buys")) / 6.0)
        age = _finite(features.get("age_ms"))
        candidates: list[CoreCandidate] = []

        if (
            age <= 300
            and _finite(features.get("noncreator_buyers")) >= 3
            and _finite(features.get("buy_sol")) >= 8.0
            and _finite(features.get("noncreator_buy_sol")) >= 5.0
            and _finite(features.get("price_multiple")) >= 1.15
        ):
            score = min(
                0.965,
                0.54
                + 0.08 * fdv_score
                + 0.08 * creator_seed_score
                + 0.10 * buyer_score
                + 0.11 * capital_score
                + 0.09 * acceleration_score,
            )
            candidates.append(CoreCandidate(score, "public_capital_burst", "standard"))

        if (
            age <= 120
            and _finite(features.get("unique_buyers")) >= 5
            and _finite(features.get("buy_sol")) >= 10.0
            and _finite(features.get("bundled_buys")) >= 3
            and _finite(features.get("price_multiple")) >= 1.25
        ):
            score = min(
                0.975,
                0.60
                + 0.08 * fdv_score
                + 0.09 * creator_seed_score
                + 0.09 * buyer_score
                + 0.08 * capital_score
                + 0.06 * bundle_score,
            )
            candidates.append(
                CoreCandidate(score, "coordinated_capital_burst", "high")
            )

        if (
            age <= 100
            and identity_score >= 0.55
            and (
                _finite(features.get("creator_buy_sol")) >= 2.0
                or _finite(features.get("creator_score")) >= 0.72
            )
        ):
            score = min(
                0.985,
                0.62
                + 0.15 * identity_score
                + 0.10 * creator_seed_score
                + 0.08 * fdv_score
                + 0.05 * acceleration_score,
            )
            candidates.append(
                CoreCandidate(score, "known_creator_or_launch_source", "high")
            )

        # Repeat-creator history is useful only after confidence calibration and
        # live confirmation. This replaces V9's unsafe 1/1 or 2/2 fast-path.
        if (
            creator.robust_positive
            and creator.confidence >= 0.55
            and age <= 110
            and _finite(features.get("noncreator_buyers")) >= 1
            and (
                _finite(features.get("noncreator_buy_sol")) >= 0.10
                or _finite(features.get("price_multiple")) >= 1.05
            )
        ):
            score = min(
                0.982,
                0.78
                + 0.10 * creator.posterior_mean
                + 0.04 * creator.confidence
                + 0.03 * fdv_score
                + 0.03 * creator_seed_score,
            )
            candidates.append(
                CoreCandidate(score, "robust_repeat_e4_creator", "strong")
            )

        # Pre-launch social/community evidence is independent of the developer
        # outcome library. Require causal pre-launch provenance and at least a
        # minimal on-chain confirmation so post-launch metadata cannot authorize.
        social = max(
            _finite(features.get("social_authority_score")),
            _finite(features.get("community_score")),
            _finite(context.get("social_authority_score")),
            _finite(context.get("community_score")),
        )
        prelaunch_social = bool(
            _finite(features.get("prelaunch_social")) >= 1.0
            or context.get("prelaunch_social")
        )
        if (
            prelaunch_social
            and social >= 0.70
            and age <= 120
            and _finite(features.get("noncreator_buyers")) >= 1
        ):
            score = min(
                0.965,
                0.73
                + 0.13 * social
                + 0.04 * fdv_score
                + 0.03 * creator_seed_score
                + 0.02 * acceleration_score,
            )
            candidates.append(
                CoreCandidate(score, "preannounced_social_community_launch", "strong")
            )

        funder = max(
            _finite(features.get("funder_score")),
            _finite(context.get("funder_score")),
        )
        public_confirm = min(
            1.0,
            0.45 * min(1.0, _finite(features.get("noncreator_buyers")) / 4.0)
            + 0.35 * min(1.0, _finite(features.get("noncreator_buy_sol")) / 8.0)
            + 0.20 * min(
                1.0,
                max(0.0, _finite(features.get("price_multiple")) - 1.0) / 0.40,
            ),
        )
        if funder >= 0.80 and public_confirm >= 0.30 and age <= 160:
            score = min(
                0.95,
                0.72
                + 0.12 * funder
                + 0.06 * public_confirm
                + 0.025 * fdv_score,
            )
            candidates.append(
                CoreCandidate(score, "trusted_funder_with_confirmation", "standard")
            )

        if age <= 80 and _finite(features.get("prearmed")) >= 1.0:
            score = min(
                0.995,
                0.82
                + 0.06 * identity_score
                + 0.05 * fdv_score
                + 0.04 * creator_seed_score
                + 0.03 * acceleration_score,
            )
            candidates.append(CoreCandidate(score, "authorized_prearmed_launch", "elite"))

        return max(candidates, key=lambda item: item.score) if candidates else None

    def _optional_model_delta(self, state_features: Mapping[str, float]) -> float:
        if not self.model:
            return 0.0
        logit = _finite(self.model.get("intercept"))
        for name, coefficient in (self.model.get("coefficients") or {}).items():
            logit += _finite(coefficient) * _finite(state_features.get(str(name)))
        probability = 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, logit))))
        return max(-0.04, min(0.04, (probability - 0.50) * 0.08))

    def _tier_at_least(self, score: float, minimum: str) -> str:
        if score >= 0.975:
            tier = "exceptional"
        elif score >= 0.93:
            tier = "elite"
        elif score >= 0.88:
            tier = "high"
        elif score >= 0.82:
            tier = "strong"
        elif score >= 0.76:
            tier = "standard"
        else:
            tier = "probe"
        return self._TIER_ORDER[
            max(self._TIER_ORDER.index(tier), self._TIER_ORDER.index(minimum))
        ]

    def _reject(
        self,
        *,
        score: float,
        threshold: float,
        family: str,
        reason: str,
        components: dict[str, float],
        creator: CreatorAssessment,
        buyers: BuyerAssessment,
        regime: RegimeAssessment,
    ) -> SelectionDecision:
        return SelectionDecision(
            False,
            score,
            threshold,
            0.0,
            "none",
            family,
            reason,
            components,
            creator,
            buyers,
            regime,
        )

    def decision(self, state: core.TokenState) -> SelectionDecision:
        regime = self.memory.regime()
        threshold = _clamp(self.config.threshold + regime.threshold_add, 0.55, 0.92)
        empty_creator = self.library.assess("")
        empty_buyers = BuyerAssessment(0, 0, 0.5, 0.0, 0.0, 0)

        if state.complete or state.migrated or state.wallet_touched:
            return self._reject(
                score=0.0,
                threshold=threshold,
                family="none",
                reason="not an untouched live Pump curve",
                components={},
                creator=empty_creator,
                buyers=empty_buyers,
                regime=regime,
            )
        if state.created_ns is None:
            return self._reject(
                score=0.0,
                threshold=threshold,
                family="none",
                reason="creation event not observed",
                components={},
                creator=empty_creator,
                buyers=empty_buyers,
                regime=regime,
            )

        features = self._legacy_features(state)
        context = self._legacy_context(state)
        creator_key = self._creator(state)
        creator = self.library.assess(creator_key)
        public_buyers = self._public_buyers(state, creator_key)
        buyers = self.memory.buyer_assessment(public_buyers)
        age = _finite(features.get("age_ms"))
        fdv = _finite(features.get("fdv_usd"), _finite(state.fdv_usd))
        maximum_fdv = min(self.settings.max_entry_fdv_usd, self.config.maximum_entry_fdv_usd)

        if age > self.config.maximum_entry_age_ms:
            return self._reject(
                score=0.0,
                threshold=threshold,
                family="none",
                reason="outside E4 launch decision horizon",
                components={"age_ms": age},
                creator=creator,
                buyers=buyers,
                regime=regime,
            )
        if fdv <= 0 or fdv > maximum_fdv:
            return self._reject(
                score=0.0,
                threshold=threshold,
                family="none",
                reason="outside observed E4 entry FDV",
                components={"fdv_usd": fdv},
                creator=creator,
                buyers=buyers,
                regime=regime,
            )
        if _finite(features.get("mayhem")) >= 1.0 or bool(context.get("mayhem")):
            return self._reject(
                score=0.0,
                threshold=threshold,
                family="none",
                reason="Mayhem launch rejected",
                components={"mayhem": 1.0},
                creator=creator,
                buyers=buyers,
                regime=regime,
            )
        if _finite(features.get("sell_count")) > 0 or _finite(features.get("sell_sol")) > 0:
            return self._reject(
                score=0.0,
                threshold=threshold,
                family="none",
                reason="sell appeared before E4 confirmation",
                components={
                    "sell_count": _finite(features.get("sell_count")),
                    "sell_sol": _finite(features.get("sell_sol")),
                },
                creator=creator,
                buyers=buyers,
                regime=regime,
            )
        creator_seed = _finite(features.get("creator_buy_sol"))
        if creator_seed < self.config.minimum_creator_seed_sol:
            return self._reject(
                score=0.0,
                threshold=threshold,
                family="none",
                reason="creator seed not observed",
                components={"creator_seed_sol": creator_seed},
                creator=creator,
                buyers=buyers,
                regime=regime,
            )
        if creator.robust_negative:
            return self._reject(
                score=0.0,
                threshold=threshold,
                family="none",
                reason="robust negative creator veto",
                components={"creator_posterior": creator.posterior_mean},
                creator=creator,
                buyers=buyers,
                regime=regime,
            )

        identity_score = self._identity_score(features, context)
        candidate = self._core_candidate(features, context, identity_score, creator)
        if candidate is None:
            return self._reject(
                score=0.0,
                threshold=threshold,
                family="none",
                reason="no evidence-backed E4 entry family matched",
                components={
                    "identity_score": identity_score,
                    "creator_seed_sol": creator_seed,
                },
                creator=creator,
                buyers=buyers,
                regime=regime,
            )

        if buyers.strongly_negative_wallets >= 2 and not creator.robust_positive:
            return self._reject(
                score=candidate.score,
                threshold=threshold,
                family=candidate.family,
                reason="negative buyer cohort veto",
                components={"negative_buyer_wallets": float(buyers.strongly_negative_wallets)},
                creator=creator,
                buyers=buyers,
                regime=regime,
            )

        model_delta = self._optional_model_delta(state.features())
        score = _clamp(
            candidate.score + creator.score_delta + buyers.score_delta + model_delta
        )
        components = {
            "core_score": candidate.score,
            "creator_delta": creator.score_delta,
            "buyer_delta": buyers.score_delta,
            "optional_model_delta": model_delta,
            "creator_posterior": creator.posterior_mean,
            "buyer_posterior": buyers.posterior_mean,
            "identity_score": identity_score,
            "creator_seed_sol": creator_seed,
            "age_ms": age,
            "fdv_usd": fdv,
            "regime_threshold_add": regime.threshold_add,
        }
        if score < threshold:
            return self._reject(
                score=score,
                threshold=threshold,
                family=candidate.family,
                reason="unified E4 score below threshold",
                components=components,
                creator=creator,
                buyers=buyers,
                regime=regime,
            )

        tier = self._tier_at_least(score, candidate.minimum_tier)
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
            candidate.family,
            f"unified E4 accepted family={candidate.family} tier={tier}",
            components,
            creator,
            buyers,
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
        features["family_index"] = float(
            {
                "none": 0,
                "public_capital_burst": 1,
                "coordinated_capital_burst": 2,
                "known_creator_or_launch_source": 3,
                "robust_repeat_e4_creator": 4,
                "preannounced_social_community_launch": 5,
                "trusted_funder_with_confirmation": 6,
                "authorized_prearmed_launch": 7,
            }.get(decision.family, -1)
        )
        if decision.accepted:
            creator = self._creator(state)
            buyers = self._public_buyers(state, creator)
            self.memory.register_entry(
                state.mint,
                creator=creator,
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


def install(core_module: Any = core, *, force: bool = False) -> None:
    """Install after legacy and final patches; reassert authority if boot order changes."""
    policy_current = core_module.E4Policy is UnifiedE4Policy
    buy_current = bool(
        getattr(core_module.Engine.execute_buy, "_e4_selection_v2_buy_wrapper", False)
    )
    sell_current = bool(
        getattr(core_module.Engine.execute_sell, "_e4_selection_v2_learning_wrapper", False)
    )
    if not force and policy_current and buy_current and sell_current:
        core_module._e4_selection_v2_installed = True
        return

    core_module.E4Policy = UnifiedE4Policy

    if not buy_current:
        original_execute_buy = core_module.Engine.execute_buy

        async def execute_buy_with_memory_cleanup(
            self: Any,
            state: core.TokenState,
            score: float,
            fraction: float,
            reason: str,
        ) -> None:
            try:
                await original_execute_buy(self, state, score, fraction, reason)
            finally:
                if state.mint not in getattr(self, "positions", {}):
                    policy = getattr(self, "policy", None)
                    memory = getattr(policy, "memory", None)
                    discard = getattr(memory, "discard_pending", None)
                    if callable(discard):
                        discard(state.mint)

        execute_buy_with_memory_cleanup._e4_selection_v2_buy_wrapper = True  # type: ignore[attr-defined]
        execute_buy_with_memory_cleanup._e4_selection_v2_wrapped = original_execute_buy  # type: ignore[attr-defined]
        core_module.Engine.execute_buy = execute_buy_with_memory_cleanup

    if not sell_current:
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

        execute_sell_with_learning._e4_selection_v2_learning_wrapper = True  # type: ignore[attr-defined]
        execute_sell_with_learning._e4_selection_v2_wrapped = original_execute_sell  # type: ignore[attr-defined]
        core_module.Engine.execute_sell = execute_sell_with_learning

    core_module._e4_selection_v2_installed = True
