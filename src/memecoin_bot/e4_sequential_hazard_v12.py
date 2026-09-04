from __future__ import annotations

import json
import math
import os
import statistics
import threading
import time
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from . import e4_hardening_v12 as v12
from . import e4_role_model_v12 as role_model
from .e4_pipelines_v10 import E4_WALLET

core = v12.core
v6 = v12.v6
PIPELINES = role_model.PIPELINES

FAMILY = "e4_v12_sequential_preintent_hazard"
MODEL_PATH = Path(
    os.getenv(
        "E4_V12_SEQUENTIAL_MODEL_PATH",
        "models/e4/e4-v12-sequential-entry-model.json",
    )
)
STATE_PATH = Path(
    os.getenv(
        "E4_V12_SEQUENTIAL_STATE_PATH",
        "models/e4/e4-v12-sequential-entry-state.json",
    )
)
ENABLED = os.getenv("E4_V12_SEQUENTIAL_ENTRY_ENABLED", "false").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _log1p(value: Any) -> float:
    return math.log1p(max(0.0, _finite(value)))


def _kind(value: Any) -> str:
    return str(getattr(value, "value", value) or "").upper()


def _event_ns(event: Any) -> int:
    return _integer(getattr(event, "received_ns", 0)) or _integer(
        getattr(event, "source_ns", 0)
    ) or time.time_ns()


def _slot(event: Any) -> int:
    direct = _integer(getattr(event, "slot", 0))
    if direct:
        return direct
    raw = getattr(event, "raw", None)
    return _integer(raw.get("slot")) if isinstance(raw, Mapping) else 0


def _event_index(event: Any) -> int:
    direct = _integer(getattr(event, "event_index", 0))
    if direct:
        return direct
    raw = getattr(event, "raw", None)
    return _integer(raw.get("event_index")) if isinstance(raw, Mapping) else 0


def _transaction_index(event: Any) -> int:
    direct = _integer(getattr(event, "transaction_index", -1), -1)
    if direct >= 0:
        return direct
    raw = getattr(event, "raw", None)
    if isinstance(raw, Mapping):
        for key in ("transaction_index", "transactionIndex", "tx_index", "txIndex"):
            if raw.get(key) is not None:
                return _integer(raw.get(key), -1)
    return -1


def _context(mint: str) -> Mapping[str, Any]:
    value = v6._CONTEXT_BY_MINT.get(str(mint), {})
    return value if isinstance(value, Mapping) else {}


def _shape(max_signature: int, create_signature_buys: int) -> str:
    return f"{int(max_signature)}|{int(create_signature_buys)}"


@dataclass
class TokenState:
    mint: str
    creator: str = ""
    created_ns: int = 0
    create_slot: int = 0
    create_signature: str = ""
    mayhem: bool = False
    creator_seed_sol: float = 0.0
    outside_sol: float = 0.0
    buy_count: int = 0
    sell_count: int = 0
    buyers: set[str] = field(default_factory=set)
    first_buyers: list[str] = field(default_factory=list)
    first_buyer_ns: list[int] = field(default_factory=list)
    same_slot_buys: int = 0
    same_slot_unique: set[str] = field(default_factory=set)
    buy_signatures: Counter[str] = field(default_factory=Counter)
    buy_slots: Counter[int] = field(default_factory=Counter)
    fdv_usd: float = 0.0
    price_sol: float = 0.0
    initial_price_sol: float = 0.0
    latest_ns: int = 0
    seen: set[tuple[Any, ...]] = field(default_factory=set)


@dataclass(frozen=True)
class PendingIntent:
    observed_ns: int
    creator: str
    buyers: tuple[str, ...]
    signature_shape: str
    successful: bool


@dataclass(frozen=True)
class HazardDecision:
    mint: str
    observed_ns: int
    probability: float
    margin: float
    features: Mapping[str, float]


class SequentialHazardModel:
    def __init__(self, payload: Mapping[str, Any]) -> None:
        if str(payload.get("status") or "") != "LIVE_HOLDOUT_CONFIRMED":
            raise ValueError(
                f"sequential model not holdout confirmed: {payload.get('status')}"
            )
        if str(payload.get("version") or "") != "e4-v12-sequential-hazard-v1":
            raise ValueError(f"unsupported sequential model: {payload.get('version')}")
        model = payload.get("model") or {}
        self.kind = str(model.get("kind") or "")
        self.features = tuple(str(value) for value in model.get("features") or ())
        if not self.features:
            raise ValueError("sequential model has no features")
        self.trees = tuple(model.get("trees") or ())
        self.mean = tuple(_finite(value) for value in model.get("mean") or ())
        self.scale = tuple(max(1e-12, _finite(value, 1.0)) for value in model.get("scale") or ())
        self.coefficient = tuple(_finite(value) for value in model.get("coefficient") or ())
        self.intercept = _finite(model.get("intercept"))
        if self.kind == "tree_ensemble":
            if not self.trees:
                raise ValueError("empty sequential tree ensemble")
        elif self.kind == "logistic":
            if not (
                len(self.features)
                == len(self.mean)
                == len(self.scale)
                == len(self.coefficient)
            ):
                raise ValueError("sequential logistic dimensions mismatch")
        else:
            raise ValueError(f"unsupported sequential model kind: {self.kind}")
        gate = payload.get("gate") or {}
        self.threshold = _finite(gate.get("threshold"), float("inf"))
        self.minimum_margin = max(
            0.0, _finite(gate.get("minimum_probability_margin"))
        )
        self.cooldown_ms = max(0.0, _finite(gate.get("cooldown_ms")))
        self.require_identity_top = bool(gate.get("require_identity_top"))
        self.require_seed_or_velocity_top = bool(
            gate.get("require_seed_or_velocity_top")
        )
        self.horizon_ms = max(1.0, _finite(payload.get("horizon_ms"), 500.0))
        guardrails = payload.get("guardrails") or {}
        self.minimum_seed_sol = max(
            0.0, _finite(guardrails.get("minimum_creator_seed_sol"), 0.20)
        )
        self.minimum_fdv_usd = max(
            0.0, _finite(guardrails.get("minimum_fdv_usd"), 2_750.0)
        )
        self.maximum_fdv_usd = max(
            self.minimum_fdv_usd,
            _finite(guardrails.get("maximum_fdv_usd"), 10_000.0),
        )
        self.maximum_age_ms = max(
            1.0, _finite(guardrails.get("maximum_age_ms"), 1_500.0)
        )

    @staticmethod
    def _tree(node: Mapping[str, Any], values: list[float]) -> float:
        current = node
        while not bool(current.get("leaf")):
            feature_index = _integer(current.get("feature_index"), -1)
            if feature_index < 0 or feature_index >= len(values):
                return _finite(current.get("probability"))
            branch = "left" if values[feature_index] <= _finite(
                current.get("threshold")
            ) else "right"
            child = current.get(branch)
            if not isinstance(child, Mapping):
                return _finite(current.get("probability"))
            current = child
        return _finite(current.get("probability"))

    def probability(self, features: Mapping[str, float]) -> float:
        values = [_finite(features.get(name)) for name in self.features]
        if self.kind == "tree_ensemble":
            result = sum(self._tree(tree, values) for tree in self.trees) / len(
                self.trees
            )
            return min(1.0, max(0.0, result))
        linear = self.intercept + sum(
            coefficient * (value - mean) / scale
            for value, mean, scale, coefficient in zip(
                values, self.mean, self.scale, self.coefficient
            )
        )
        if linear >= 0:
            return 1.0 / (1.0 + math.exp(-min(60.0, linear)))
        exp_value = math.exp(max(-60.0, linear))
        return exp_value / (1.0 + exp_value)


class SequentialHazardRuntime:
    def __init__(
        self,
        model: SequentialHazardModel | None,
        state_payload: Mapping[str, Any] | None = None,
    ) -> None:
        self.model = model
        self.lock = threading.RLock()
        self.tokens: dict[str, TokenState] = {}
        self.decisions: dict[str, HazardDecision] = {}
        self.recent_scores: deque[tuple[int, str, float]] = deque()
        self.pending: deque[PendingIntent] = deque()
        self.creator_attempts: Counter[str] = Counter()
        self.creator_successes: Counter[str] = Counter()
        self.creator_failures: Counter[str] = Counter()
        self.buyer_attempts: Counter[str] = Counter()
        self.buyer_successes: Counter[str] = Counter()
        self.pair_attempts: Counter[str] = Counter()
        self.signature_shapes: Counter[str] = Counter()
        self.last_global_decision_ns = 0
        self.intent_seen: set[tuple[str, str, int]] = set()
        self._load_state(state_payload or {})

    @property
    def active(self) -> bool:
        return bool(ENABLED and self.model is not None)

    def _load_state(self, payload: Mapping[str, Any]) -> None:
        if str(payload.get("version") or "") not in {
            "",
            "e4-v12-causal-runtime-state-v1",
        }:
            raise ValueError("unsupported sequential runtime state")
        for key, value in (payload.get("creators") or {}).items():
            if isinstance(value, Mapping):
                self.creator_attempts[str(key)] = _integer(value.get("attempts"))
                self.creator_successes[str(key)] = _integer(value.get("successes"))
                self.creator_failures[str(key)] = _integer(
                    value.get("failed_attempts")
                )
        for key, value in (payload.get("buyers") or {}).items():
            if isinstance(value, Mapping):
                self.buyer_attempts[str(key)] = _integer(value.get("attempts"))
                self.buyer_successes[str(key)] = _integer(value.get("successes"))
        for key, value in (payload.get("creator_buyer_pairs") or {}).items():
            self.pair_attempts[str(key)] = _integer(value)
        for key, value in (payload.get("signature_shapes") or {}).items():
            self.signature_shapes[str(key)] = _integer(value)

    def _flush_pending(self, now_ns: int) -> None:
        while self.pending and self.pending[0].observed_ns < now_ns:
            intent = self.pending.popleft()
            if intent.creator:
                self.creator_attempts[intent.creator] += 1
                self.creator_successes[intent.creator] += int(intent.successful)
                self.creator_failures[intent.creator] += int(not intent.successful)
            for buyer in intent.buyers:
                self.buyer_attempts[buyer] += 1
                self.buyer_successes[buyer] += int(intent.successful)
                if intent.creator:
                    self.pair_attempts[f"{intent.creator}|{buyer}"] += 1
            if intent.signature_shape:
                self.signature_shapes[intent.signature_shape] += 1

    def _token(self, event: Any) -> TokenState:
        mint = str(getattr(event, "mint", "") or "")
        context = _context(mint)
        token = self.tokens.get(mint)
        if token is None:
            token = TokenState(
                mint=mint,
                creator=str(
                    getattr(event, "creator", "")
                    or context.get("creator")
                    or ""
                ),
                created_ns=_event_ns(event),
                create_slot=_slot(event),
                create_signature=str(getattr(event, "signature", "") or ""),
                mayhem=bool(context.get("is_mayhem_mode")),
                latest_ns=_event_ns(event),
            )
            self.tokens[mint] = token
        else:
            token.creator = token.creator or str(
                getattr(event, "creator", "")
                or context.get("creator")
                or ""
            )
            token.mayhem = token.mayhem or bool(context.get("is_mayhem_mode"))
        return token

    @staticmethod
    def _remember(token: TokenState, event: Any) -> bool:
        key = (
            _kind(getattr(event, "kind", None)),
            str(getattr(event, "signature", "") or ""),
            str(getattr(event, "trader", "") or ""),
            round(_finite(getattr(event, "sol_amount", 0.0)), 12),
            round(_finite(getattr(event, "token_amount", 0.0)), 6),
            _event_ns(event),
        )
        if key in token.seen:
            return False
        token.seen.add(key)
        if len(token.seen) > 1_024:
            token.seen = set(tuple(token.seen)[-512:])
        return True

    def _eligible(self, token: TokenState, now_ns: int) -> bool:
        model = self.model
        if model is None or token.created_ns <= 0:
            return False
        age_ms = max(0.0, (now_ns - token.created_ns) / 1e6)
        return bool(
            not token.mayhem
            and token.sell_count == 0
            and model.minimum_fdv_usd <= token.fdv_usd <= model.maximum_fdv_usd
            and token.creator_seed_sol >= model.minimum_seed_sol
            and age_ms <= model.maximum_age_ms
        )

    def _base_features(
        self,
        token: TokenState,
        now_ns: int,
        trigger: Any | None,
    ) -> dict[str, float]:
        creator = token.creator
        attempts = self.creator_attempts[creator]
        successes = self.creator_successes[creator]
        failures = self.creator_failures[creator]
        buyers = token.first_buyers
        buyer_attempts = [self.buyer_attempts[value] for value in buyers]
        buyer_successes = [self.buyer_successes[value] for value in buyers]
        pair_attempts = [self.pair_attempts[f"{creator}|{value}"] for value in buyers]
        unique_count = max(1, len(token.buyers))
        age_ms = max(0.0, (now_ns - token.created_ns) / 1e6)
        first_age = (
            (token.first_buyer_ns[0] - token.created_ns) / 1e6
            if token.first_buyer_ns
            else 9_999.0
        )
        second_age = (
            (token.first_buyer_ns[1] - token.created_ns) / 1e6
            if len(token.first_buyer_ns) > 1
            else 9_999.0
        )
        interbuy = (
            statistics.median(
                (right - left) / 1e6
                for left, right in zip(
                    token.first_buyer_ns, token.first_buyer_ns[1:]
                )
            )
            if len(token.first_buyer_ns) > 1
            else 9_999.0
        )
        max_signature = max(token.buy_signatures.values(), default=0)
        create_signature_buys = _integer(
            token.buy_signatures.get(token.create_signature, 0)
        )
        shape_attempts = self.signature_shapes[
            _shape(max_signature, create_signature_buys)
        ]
        seed = token.creator_seed_sol
        outside = token.outside_sol
        fdv = max(1.0, token.fdv_usd)
        common_seed_distance = min(
            abs(seed - value)
            for value in (
                0.25,
                0.5,
                1.0,
                1.2,
                1.5,
                2.0,
                2.5,
                3.0,
                5.0,
                6.0,
                8.0,
            )
        )
        price_multiple = (
            token.price_sol / token.initial_price_sol
            if token.price_sol > 0 and token.initial_price_sol > 0
            else 1.0
        )
        trader = str(getattr(trigger, "trader", "") or "") if trigger else ""
        trigger_slot = _slot(trigger) if trigger else 0
        trigger_signature = (
            str(getattr(trigger, "signature", "") or "") if trigger else ""
        )
        values = {
            "log_seed": _log1p(seed),
            "log_outside": _log1p(outside),
            "log_fdv": _log1p(fdv),
            "age_100ms": min(20.0, age_ms / 100.0),
            "prior_creator_log": _log1p(attempts),
            "prior_creator_success_log": _log1p(successes),
            "prior_creator_failure_log": _log1p(failures),
            "creator_success_rate": successes / attempts if attempts else 0.0,
            "known_buyer_count": float(
                sum(value > 0 for value in buyer_attempts)
            ),
            "max_prior_buyer_log": _log1p(max(buyer_attempts, default=0)),
            "sum_prior_buyer_log": _log1p(sum(buyer_attempts)),
            "max_prior_buyer_success_log": _log1p(
                max(buyer_successes, default=0)
            ),
            "sum_prior_buyer_success_log": _log1p(sum(buyer_successes)),
            "max_pair_log": _log1p(max(pair_attempts, default=0)),
            "seed_share": seed / max(1e-9, seed + outside),
            "first_buyer_age_100ms": min(100.0, first_age / 100.0),
            "second_buyer_age_100ms": min(100.0, second_age / 100.0),
            "interbuyer_100ms": min(100.0, interbuy / 100.0),
            "distinct_buy_signatures": float(
                len([key for key in token.buy_signatures if key])
            ),
            "max_buys_one_signature": float(max_signature),
            "max_buys_one_slot": float(max(token.buy_slots.values(), default=0)),
            "create_signature_buys": float(create_signature_buys),
            "price_multiple_clip": min(10.0, max(0.0, price_multiple)),
            "prior_signature_shape_log": _log1p(shape_attempts),
            "outside_per_buyer": outside / unique_count,
            "buyer_graph_density": sum(buyer_attempts) / unique_count,
            "buyer_success_density": sum(buyer_successes) / unique_count,
            "identity_strength": (
                1.5 * _log1p(successes)
                + 1.25 * _log1p(attempts)
                + _log1p(sum(buyer_attempts))
            ),
            "slot_cluster_strength": (
                len(token.same_slot_unique)
                + 0.5 * token.same_slot_buys
                + 0.75 * sum(value > 0 for value in buyer_attempts)
            ),
            "launch_velocity": (
                token.buy_count + len(token.buyers)
            ) / max(0.25, age_ms / 1_000.0),
            "seed_to_fdv": seed / fdv * 10_000.0,
            "outside_to_fdv": outside / fdv * 10_000.0,
            "no_public_buyers": float(len(token.buyers) == 0),
            "one_public_buyer": float(len(token.buyers) == 1),
            "two_plus_public_buyers": float(len(token.buyers) >= 2),
            "very_early_50ms": float(age_ms <= 50.0),
            "very_early_150ms": float(age_ms <= 150.0),
            "very_early_400ms": float(age_ms <= 400.0),
            "fdv_core_band": float(3_500.0 <= fdv <= 7_500.0),
            "seed_roundness": math.exp(-4.0 * common_seed_distance),
            "trigger_is_creator": float(bool(trader and trader == creator)),
            "trigger_is_known_buyer": float(
                self.buyer_attempts[trader] > 0 if trader else False
            ),
            "trigger_buyer_attempts_log": _log1p(
                self.buyer_attempts[trader] if trader else 0
            ),
            "trigger_buyer_successes_log": _log1p(
                self.buyer_successes[trader] if trader else 0
            ),
            "trigger_sol_log": _log1p(
                getattr(trigger, "sol_amount", 0.0) if trigger else 0.0
            ),
            "trigger_tx_index_log": _log1p(
                max(0, _transaction_index(trigger)) if trigger else 0
            ),
            "trigger_event_index_log": _log1p(
                _event_index(trigger) if trigger else 0
            ),
            "trigger_same_create_slot": float(
                bool(trigger_slot and trigger_slot == token.create_slot)
            ),
            "trigger_same_create_signature": float(
                bool(trigger_signature and trigger_signature == token.create_signature)
            ),
        }
        return values

    def _features(
        self,
        token: TokenState,
        now_ns: int,
        trigger: Any,
    ) -> dict[str, float]:
        values = self._base_features(token, now_ns, trigger)
        active: list[tuple[TokenState, dict[str, float]]] = []
        for other in self.tokens.values():
            if self._eligible(other, now_ns):
                active.append((other, self._base_features(other, now_ns, None)))
        if not active:
            active = [(token, values)]

        def relative(name: str) -> tuple[int, float, float]:
            current = values[name]
            other_values = [
                feature[name]
                for other, feature in active
                if other.mint != token.mint
            ]
            rank = 1 + sum(value > current for value in other_values)
            best = max(other_values, default=0.0)
            return rank, current - best, best

        seed_rank, seed_gap, max_seed = relative("log_seed")
        identity_rank, identity_gap, max_identity = relative(
            "identity_strength"
        )
        velocity_rank, velocity_gap, max_velocity = relative(
            "launch_velocity"
        )
        current_buyers = len(token.buyers)
        buyer_rank = 1 + sum(
            len(other.buyers) > current_buyers
            for other, _ in active
            if other.mint != token.mint
        )
        values.update(
            {
                "active_count_log": _log1p(len(active)),
                "seed_rank_inverse": 1.0 / seed_rank,
                "identity_rank_inverse": 1.0 / identity_rank,
                "velocity_rank_inverse": 1.0 / velocity_rank,
                "buyers_rank_inverse": 1.0 / buyer_rank,
                "current_is_seed_top": float(seed_rank == 1),
                "current_is_identity_top": float(identity_rank == 1),
                "current_is_velocity_top": float(velocity_rank == 1),
                "seed_gap_to_best": seed_gap,
                "identity_gap_to_best": identity_gap,
                "velocity_gap_to_best": velocity_gap,
                "competitor_max_seed_log": max_seed,
                "competitor_max_identity": max_identity,
                "competitor_max_velocity_log": _log1p(max_velocity),
            }
        )
        return values

    def observe_pre(self, event: Any) -> None:
        if not self.active or self.model is None:
            return
        mint = str(getattr(event, "mint", "") or "")
        if not mint:
            return
        now_ns = _event_ns(event)
        kind = _kind(getattr(event, "kind", None))
        trader = str(getattr(event, "trader", "") or "")
        with self.lock:
            self._flush_pending(now_ns)
            token = self._token(event)
            if not self._remember(token, event):
                return
            token.latest_ns = max(token.latest_ns, now_ns)
            fdv = _finite(getattr(event, "fdv_usd", 0.0))
            price = _finite(getattr(event, "price_sol", 0.0))
            if fdv > 0:
                token.fdv_usd = fdv
            if price > 0:
                token.price_sol = price
                if token.initial_price_sol <= 0:
                    token.initial_price_sol = price
            if kind == "CREATE":
                token.created_ns = now_ns
                token.create_slot = _slot(event)
                token.create_signature = str(
                    getattr(event, "signature", "") or ""
                )
                token.creator = token.creator or trader
            elif trader == E4_WALLET:
                return
            elif kind in {"BUY", "PUMPSWAP_BUY"}:
                sol = max(0.0, _finite(getattr(event, "sol_amount", 0.0)))
                signature = str(getattr(event, "signature", "") or "")
                slot = _slot(event)
                token.buy_count += 1
                token.buy_signatures[signature] += 1
                token.buy_slots[slot] += 1
                if slot and slot == token.create_slot:
                    token.same_slot_buys += 1
                if trader and trader == token.creator:
                    token.creator_seed_sol += sol
                elif trader:
                    token.outside_sol += sol
                    if trader not in token.buyers:
                        token.buyers.add(trader)
                        if len(token.first_buyers) < 12:
                            token.first_buyers.append(trader)
                            token.first_buyer_ns.append(now_ns)
                    if slot and slot == token.create_slot:
                        token.same_slot_unique.add(trader)
            elif kind in {"SELL", "PUMPSWAP_SELL"}:
                token.sell_count += 1
                self.decisions.pop(mint, None)
                return
            else:
                return
            if not self._eligible(token, now_ns):
                return
            features = self._features(token, now_ns, event)
            probability = self.model.probability(features)
            while self.recent_scores and self.recent_scores[0][0] < now_ns - 250_000_000:
                self.recent_scores.popleft()
            best_other = max(
                (
                    score
                    for _, other_mint, score in self.recent_scores
                    if other_mint != mint
                ),
                default=0.0,
            )
            margin = probability - best_other
            self.recent_scores.append((now_ns, mint, probability))
            if probability < self.model.threshold:
                return
            if self.model.require_identity_top and not bool(
                features.get("current_is_identity_top")
            ):
                return
            if self.model.require_seed_or_velocity_top and not (
                bool(features.get("current_is_seed_top"))
                or bool(features.get("current_is_velocity_top"))
            ):
                return
            if margin < self.model.minimum_margin:
                return
            if (
                self.last_global_decision_ns
                and now_ns - self.last_global_decision_ns
                < int(self.model.cooldown_ms * 1e6)
            ):
                return
            if mint not in self.decisions:
                self.decisions[mint] = HazardDecision(
                    mint=mint,
                    observed_ns=now_ns,
                    probability=probability,
                    margin=margin,
                    features=dict(features),
                )
                self.last_global_decision_ns = now_ns

    def observe_post(self, event: Any) -> None:
        if not self.active:
            return
        kind = _kind(getattr(event, "kind", None))
        trader = str(getattr(event, "trader", "") or "")
        if trader != E4_WALLET or kind not in {"BUY", "PUMPSWAP_BUY"}:
            return
        mint = str(getattr(event, "mint", "") or "")
        now_ns = _event_ns(event)
        signature = str(getattr(event, "signature", "") or "")
        dedupe = (mint, signature, now_ns)
        with self.lock:
            if dedupe in self.intent_seen:
                return
            self.intent_seen.add(dedupe)
            token = self.tokens.get(mint)
            if token is None:
                return
            self.pending.append(
                PendingIntent(
                    observed_ns=now_ns,
                    creator=token.creator,
                    buyers=tuple(token.first_buyers),
                    signature_shape=_shape(
                        max(token.buy_signatures.values(), default=0),
                        _integer(
                            token.buy_signatures.get(
                                token.create_signature, 0
                            )
                        ),
                    ),
                    successful=True,
                )
            )

    def observe_failed_intent(self, mint: str, observed_ns: int) -> None:
        if not self.active:
            return
        with self.lock:
            token = self.tokens.get(str(mint))
            if token is None:
                return
            self.pending.append(
                PendingIntent(
                    observed_ns=int(observed_ns),
                    creator=token.creator,
                    buyers=tuple(token.first_buyers),
                    signature_shape=_shape(
                        max(token.buy_signatures.values(), default=0),
                        _integer(
                            token.buy_signatures.get(
                                token.create_signature, 0
                            )
                        ),
                    ),
                    successful=False,
                )
            )

    def decision(self, mint: str, now_ns: int) -> HazardDecision | None:
        if not self.active or self.model is None:
            return None
        with self.lock:
            decision = self.decisions.get(str(mint))
            token = self.tokens.get(str(mint))
            if decision is None or token is None or token.sell_count > 0:
                return None
            if (now_ns - decision.observed_ns) / 1e6 > self.model.horizon_ms:
                return None
            return decision

    def reset_mint(self, mint: str) -> None:
        with self.lock:
            self.tokens.pop(str(mint), None)
            self.decisions.pop(str(mint), None)


def _read_json(path: Path) -> Mapping[str, Any]:
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _load_runtime() -> SequentialHazardRuntime:
    if not ENABLED:
        return SequentialHazardRuntime(None, {})
    return SequentialHazardRuntime(
        SequentialHazardModel(_read_json(MODEL_PATH)),
        _read_json(STATE_PATH),
    )


RUNTIME = _load_runtime()

_PREVIOUS_OBSERVE = role_model.observe_market_event


def _observe_market_event_sequential_v12(event: Any) -> None:
    RUNTIME.observe_pre(event)
    _PREVIOUS_OBSERVE(event)
    RUNTIME.observe_post(event)


role_model.observe_market_event = _observe_market_event_sequential_v12

_PREVIOUS_RESET = role_model.reset_role_model_replay


def _reset_role_model_replay_sequential_v12(mint: str) -> None:
    RUNTIME.reset_mint(mint)
    _PREVIOUS_RESET(mint)


role_model.reset_role_model_replay = _reset_role_model_replay_sequential_v12

_PREVIOUS_ENTRY = core.E4Policy.entry


def _entry_sequential_v12(self: Any, state: Any):
    now_ns = _integer(getattr(state, "latest_ns", 0)) or time.time_ns()
    decision = RUNTIME.decision(str(state.mint), now_ns)
    if decision is None:
        return _PREVIOUS_ENTRY(self, state)
    features = dict(v12.v8._identity_features(state))
    features.update(
        {
            "v12_sequential_preintent": 1.0,
            "v12_sequential_probability": decision.probability,
            "v12_sequential_margin": decision.margin,
            "v12_sequential_decision_ns": float(decision.observed_ns),
        }
    )
    fraction = min(
        _finite(os.getenv("E4_V12_SEQUENTIAL_ENTRY_FRACTION"), 0.0185),
        _finite(getattr(self.settings, "max_position_fraction", 0.20), 0.20),
    )
    return v12._make_profile(
        self,
        state,
        features,
        family=FAMILY,
        score=min(0.985, max(0.84, 0.88 + 0.10 * decision.probability)),
        fraction=fraction,
        reason=(
            "sequential E4 pre-intent hazard "
            f"p={decision.probability:.4f} margin={decision.margin:.4f}"
        ),
    )


core.E4Policy.entry = _entry_sequential_v12

_PREVIOUS_EXIT = core.E4Policy.exit


def _exit_sequential_v12(self: Any, position: Any, state: Any):
    profile = v6._PROFILE_BY_MINT.get(str(position.mint))
    if str(getattr(profile, "family", "") or "") != FAMILY:
        return _PREVIOUS_EXIT(self, position, state)
    source = PIPELINES.e4_signal(position.mint)
    if source is not None and _finite(getattr(source, "entry_tokens", 0.0)) > 0:
        entry_tokens = max(0.0, _finite(source.entry_tokens))
        source_remaining = max(0.0, _finite(source.remaining_tokens))
        target_sold = min(
            1.0, max(0.0, 1.0 - source_remaining / entry_tokens)
        )
        original = max(0.0, _finite(getattr(position, "tokens", 0.0)))
        remaining = max(0.0, _finite(getattr(position, "remaining", 0.0)))
        gambit_sold = (
            min(1.0, max(0.0, 1.0 - remaining / original))
            if original > 0
            else 0.0
        )
        if bool(getattr(source, "fully_exited", False)) or target_sold >= 0.985:
            return "SELL_ALL", 1.0, "E4 V12 sequential source fully exited"
        additional = max(0.0, target_sold - gambit_sold)
        fraction = min(
            1.0, additional / max(1e-12, 1.0 - gambit_sold)
        )
        if fraction >= 0.01:
            return (
                "SELL_PARTIAL",
                fraction,
                "E4 V12 sequential cumulative source-exit mirror",
            )
        return "HOLD", 0.0, "E4 V12 sequential confirmed; awaiting source exit"
    opened_ns = _integer(getattr(position, "opened_ns", 0))
    now_ns = _integer(getattr(state, "latest_ns", 0)) or time.time_ns()
    confirmation_ms = max(
        100.0,
        _finite(os.getenv("E4_V12_SEQUENTIAL_CONFIRMATION_MS"), 1_500.0),
    )
    if opened_ns > 0 and (now_ns - opened_ns) / 1e6 < confirmation_ms:
        return "HOLD", 0.0, "E4 V12 sequential awaiting E4 confirmation"
    return _PREVIOUS_EXIT(self, position, state)


core.E4Policy.exit = _exit_sequential_v12
