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
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

from . import e4_hardening_v12 as v12
from . import e4_role_model_v12 as role_model
from .e4_pipelines_v10 import E4_WALLET

core = v12.core
v6 = v12.v6
PIPELINES = role_model.PIPELINES

FAMILY = "e4_v12_causal_preimpact_choice"
MODEL_PATH = Path(
    os.getenv(
        "E4_V12_CAUSAL_MODEL_PATH",
        "models/e4/e4-v12-causal-entry-model.json",
    )
)
STATE_PATH = Path(
    os.getenv(
        "E4_V12_CAUSAL_STATE_PATH",
        "models/e4/e4-v12-causal-entry-state.json",
    )
)
ENABLED = os.getenv("E4_V12_CAUSAL_ENTRY_ENABLED", "false").strip().lower() in {
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


def _context(mint: str) -> Mapping[str, Any]:
    value = v6._CONTEXT_BY_MINT.get(str(mint), {})
    return value if isinstance(value, Mapping) else {}


def _sig_shape(max_one_signature: int, create_signature_buys: int) -> str:
    return f"{int(max_one_signature)}|{int(create_signature_buys)}"


@dataclass
class TokenRuntime:
    mint: str
    creator: str = ""
    created_ns: int = 0
    create_slot: int = 0
    create_signature: str = ""
    uri: str = ""
    metadata_host: str = ""
    token_program: str = ""
    mayhem: bool = False
    cashback: bool = False
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
class ChoiceDecision:
    mint: str
    observed_ns: int
    utility: float
    margin: float
    rank: int
    mode: str
    features: Mapping[str, float]


@dataclass(frozen=True)
class PendingIntent:
    observed_ns: int
    creator: str
    buyers: tuple[str, ...]
    signature_shape: str
    successful: bool


class ConditionalChoiceModel:
    def __init__(self, payload: Mapping[str, Any]) -> None:
        status = str(payload.get("status") or "")
        if status != "LIVE_HOLDOUT_CONFIRMED":
            raise ValueError(f"causal model is not live-holdout confirmed: {status}")
        version = str(payload.get("version") or "")
        if version != "e4-v12-conditional-choice-ranker-v1":
            raise ValueError(f"unsupported causal model version: {version}")
        ranker = payload.get("ranker") or {}
        self.features = tuple(str(value) for value in ranker.get("features") or ())
        self.scale = tuple(max(1e-12, _finite(value, 1.0)) for value in ranker.get("scale") or ())
        self.coefficient = tuple(_finite(value) for value in ranker.get("coefficient") or ())
        if not self.features or not (
            len(self.features) == len(self.scale) == len(self.coefficient)
        ):
            raise ValueError("causal model dimensions are invalid")
        gate = payload.get("gate") or {}
        self.minimum_utility = _finite(gate.get("minimum_utility"), float("inf"))
        self.minimum_margin = max(0.0, _finite(gate.get("minimum_margin")))
        self.maximum_rank = max(1, _integer(gate.get("maximum_rank"), 1))
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
        self.competition_window_ms = max(
            1.0,
            _finite(guardrails.get("create_competition_window_ms"), 750.0),
        )

    def utility(self, values: Mapping[str, float]) -> float:
        return sum(
            coefficient * _finite(values.get(name)) / scale
            for name, scale, coefficient in zip(
                self.features, self.scale, self.coefficient
            )
        )


class CausalChoiceRuntime:
    def __init__(
        self,
        model: ConditionalChoiceModel | None,
        state_payload: Mapping[str, Any] | None = None,
    ) -> None:
        self.model = model
        self.lock = threading.RLock()
        self.tokens: dict[str, TokenRuntime] = {}
        self.decisions: dict[str, ChoiceDecision] = {}
        self.pending: deque[PendingIntent] = deque()
        self.creator_attempts: Counter[str] = Counter()
        self.creator_successes: Counter[str] = Counter()
        self.creator_failures: Counter[str] = Counter()
        self.buyer_attempts: Counter[str] = Counter()
        self.buyer_successes: Counter[str] = Counter()
        self.creator_buyer_attempts: Counter[str] = Counter()
        self.signature_shapes: Counter[str] = Counter()
        self.last_claim_ns = 0
        self.last_claim_utility = -float("inf")
        self.last_claim_mint = ""
        self._load_state(state_payload or {})

    @property
    def active(self) -> bool:
        return bool(ENABLED and self.model is not None)

    def _load_state(self, payload: Mapping[str, Any]) -> None:
        if str(payload.get("version") or "") not in {
            "",
            "e4-v12-causal-runtime-state-v1",
        }:
            raise ValueError("unsupported causal runtime-state version")
        for creator, value in (payload.get("creators") or {}).items():
            if not isinstance(value, Mapping):
                continue
            key = str(creator)
            self.creator_attempts[key] = _integer(value.get("attempts"))
            self.creator_successes[key] = _integer(value.get("successes"))
            self.creator_failures[key] = _integer(value.get("failed_attempts"))
        for buyer, value in (payload.get("buyers") or {}).items():
            if not isinstance(value, Mapping):
                continue
            key = str(buyer)
            self.buyer_attempts[key] = _integer(value.get("attempts"))
            self.buyer_successes[key] = _integer(value.get("successes"))
        for pair, value in (payload.get("creator_buyer_pairs") or {}).items():
            self.creator_buyer_attempts[str(pair)] = _integer(value)
        for shape, value in (payload.get("signature_shapes") or {}).items():
            self.signature_shapes[str(shape)] = _integer(value)

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
                    self.creator_buyer_attempts[
                        f"{intent.creator}|{buyer}"
                    ] += 1
            if intent.signature_shape:
                self.signature_shapes[intent.signature_shape] += 1

    def _remember(self, token: TokenRuntime, event: Any) -> bool:
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

    def _token(self, event: Any) -> TokenRuntime:
        mint = str(getattr(event, "mint", "") or "")
        token = self.tokens.get(mint)
        context = _context(mint)
        if token is None:
            uri = str(context.get("uri") or "")
            token = TokenRuntime(
                mint=mint,
                creator=str(
                    getattr(event, "creator", "")
                    or context.get("creator")
                    or ""
                ),
                created_ns=_event_ns(event),
                create_slot=_slot(event),
                create_signature=str(getattr(event, "signature", "") or ""),
                uri=uri,
                metadata_host=(urlparse(uri).netloc or "").lower(),
                token_program=str(context.get("token_program") or ""),
                mayhem=bool(context.get("is_mayhem_mode")),
                cashback=bool(context.get("is_cashback_enabled")),
                latest_ns=_event_ns(event),
            )
            self.tokens[mint] = token
        else:
            token.creator = token.creator or str(
                getattr(event, "creator", "")
                or context.get("creator")
                or ""
            )
            if not token.uri and context.get("uri"):
                token.uri = str(context.get("uri") or "")
                token.metadata_host = (urlparse(token.uri).netloc or "").lower()
            token.token_program = token.token_program or str(
                context.get("token_program") or ""
            )
            token.mayhem = token.mayhem or bool(context.get("is_mayhem_mode"))
            token.cashback = token.cashback or bool(
                context.get("is_cashback_enabled")
            )
        return token

    def observe_pre(self, event: Any) -> None:
        if not self.active:
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
                return

            if trader == E4_WALLET:
                # The current E4 transaction cannot authorize a pre-impact
                # decision. Direct-copy handling remains in role_model_v12.
                return

            if kind in {"BUY", "PUMPSWAP_BUY"}:
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
                self._evaluate_and_store(mint, now_ns)
            elif kind in {"SELL", "PUMPSWAP_SELL"}:
                token.sell_count += 1
                self.decisions.pop(mint, None)

    def observe_post(self, event: Any) -> None:
        if not self.active:
            return
        kind = _kind(getattr(event, "kind", None))
        trader = str(getattr(event, "trader", "") or "")
        if trader != E4_WALLET or kind not in {"BUY", "PUMPSWAP_BUY"}:
            return
        mint = str(getattr(event, "mint", "") or "")
        now_ns = _event_ns(event)
        with self.lock:
            token = self.tokens.get(mint)
            if token is None:
                return
            shape = _sig_shape(
                max(token.buy_signatures.values(), default=0),
                _integer(token.buy_signatures.get(token.create_signature, 0)),
            )
            self.pending.append(
                PendingIntent(
                    observed_ns=now_ns,
                    creator=token.creator,
                    buyers=tuple(token.first_buyers),
                    signature_shape=shape,
                    successful=True,
                )
            )

    def observe_failed_intent(
        self,
        *,
        mint: str,
        observed_ns: int,
    ) -> None:
        """Accept a causally decoded failed E4 buy from a wallet observer."""
        if not self.active:
            return
        with self.lock:
            token = self.tokens.get(str(mint))
            if token is None:
                return
            shape = _sig_shape(
                max(token.buy_signatures.values(), default=0),
                _integer(token.buy_signatures.get(token.create_signature, 0)),
            )
            self.pending.append(
                PendingIntent(
                    observed_ns=int(observed_ns),
                    creator=token.creator,
                    buyers=tuple(token.first_buyers),
                    signature_shape=shape,
                    successful=False,
                )
            )

    def _eligible(self, token: TokenRuntime, now_ns: int) -> bool:
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

    def _features(self, token: TokenRuntime, now_ns: int) -> dict[str, float]:
        age_ms = max(0.0, (now_ns - token.created_ns) / 1e6)
        attempts = self.creator_attempts[token.creator]
        successes = self.creator_successes[token.creator]
        buyers = token.first_buyers
        buyer_attempts = [self.buyer_attempts[value] for value in buyers]
        buyer_successes = [self.buyer_successes[value] for value in buyers]
        pair_attempts = [
            self.creator_buyer_attempts[f"{token.creator}|{value}"]
            for value in buyers
        ]
        unique = max(1, len(token.buyers))
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
        price_multiple = (
            token.price_sol / token.initial_price_sol
            if token.price_sol > 0 and token.initial_price_sol > 0
            else 1.0
        )
        max_one_signature = max(token.buy_signatures.values(), default=0)
        create_signature_buys = _integer(
            token.buy_signatures.get(token.create_signature, 0)
        )
        shape_attempts = self.signature_shapes[
            _sig_shape(max_one_signature, create_signature_buys)
        ]
        visible = sum(
            1
            for other in self.tokens.values()
            if other.mint != token.mint
            and 0 <= now_ns - other.created_ns <= 500_000_000
            and self._eligible(other, now_ns)
        )
        seed = token.creator_seed_sol
        outside = token.outside_sol
        fdv = max(1.0, token.fdv_usd)
        seed_share = seed / max(1e-9, seed + outside)
        common_seed_distance = min(
            abs(seed - value)
            for value in (0.25, 0.5, 1.0, 1.2, 1.5, 2.0, 2.5, 3.0, 5.0, 6.0, 8.0)
        )
        values = {
            "log_seed": _log1p(seed),
            "log_outside": _log1p(outside),
            "log_fdv": _log1p(token.fdv_usd),
            "age_100ms": min(20.0, age_ms / 100.0),
            "prior_creator_log": _log1p(attempts),
            "prior_creator_success_log": _log1p(successes),
            "known_buyer_count": float(sum(value > 0 for value in buyer_attempts)),
            "max_prior_buyer_log": _log1p(max(buyer_attempts, default=0)),
            "sum_prior_buyer_log": _log1p(sum(buyer_attempts)),
            "max_prior_buyer_success_log": _log1p(
                max(buyer_successes, default=0)
            ),
            "sum_prior_buyer_success_log": _log1p(sum(buyer_successes)),
            "max_pair_log": _log1p(max(pair_attempts, default=0)),
            "seed_share": seed_share,
            "first_buyer_age_100ms": min(100.0, first_age / 100.0),
            "second_buyer_age_100ms": min(100.0, second_age / 100.0),
            "interbuyer_100ms": min(100.0, interbuy / 100.0),
            "distinct_buy_signatures": float(
                len([key for key in token.buy_signatures if key])
            ),
            "max_buys_one_signature": float(max_one_signature),
            "max_buys_one_slot": float(max(token.buy_slots.values(), default=0)),
            "create_signature_buys": float(create_signature_buys),
            "price_multiple_clip": min(10.0, max(0.0, price_multiple)),
            "visible_competitors_log": _log1p(visible),
            "prior_signature_shape_log": _log1p(shape_attempts),
            "outside_per_buyer": outside / unique,
            "buyer_graph_density": sum(buyer_attempts) / unique,
            "buyer_success_density": sum(buyer_successes) / unique,
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
            "fdv_core_band": float(3_500.0 <= token.fdv_usd <= 7_500.0),
            "seed_roundness": math.exp(-4.0 * common_seed_distance),
        }
        return values

    def _rank(self, mint: str, now_ns: int) -> tuple[int, float, float, str, dict[str, float]]:
        assert self.model is not None
        candidates: list[tuple[float, str, dict[str, float]]] = []
        current_features: dict[str, float] = {}
        window_ns = int(self.model.competition_window_ms * 1e6)
        for token in self.tokens.values():
            if not self._eligible(token, now_ns):
                continue
            if abs(token.created_ns - self.tokens[mint].created_ns) > window_ns:
                continue
            features = self._features(token, now_ns)
            utility = self.model.utility(features)
            candidates.append((utility, token.mint, features))
            if token.mint == mint:
                current_features = features
        candidates.sort(key=lambda row: (row[0], row[1]), reverse=True)
        current = next(
            ((score, index, features) for index, (score, key, features) in enumerate(candidates, start=1) if key == mint),
            (-float("inf"), 10**9, current_features),
        )
        utility, rank, features = current
        runner_up = max(
            (score for score, key, _ in candidates if key != mint),
            default=-float("inf"),
        )
        margin = utility - runner_up if math.isfinite(runner_up) else utility
        identity = features.get("identity_strength", 0.0)
        cluster = features.get("slot_cluster_strength", 0.0) + features.get(
            "buyer_graph_density", 0.0
        )
        mode = "LAUNCH_AUTHORITY" if identity >= cluster else "WALLET_CLUSTER"
        return rank, utility, margin, mode, features

    def _evaluate_and_store(self, mint: str, now_ns: int) -> None:
        if self.model is None:
            return
        token = self.tokens.get(mint)
        if token is None or not self._eligible(token, now_ns):
            return
        rank, utility, margin, mode, features = self._rank(mint, now_ns)
        if utility < self.model.minimum_utility:
            return
        if rank > self.model.maximum_rank:
            return
        if margin < self.model.minimum_margin:
            return
        claim_window_ns = int(self.model.competition_window_ms * 1e6)
        if (
            self.last_claim_ns
            and now_ns - self.last_claim_ns <= claim_window_ns
            and self.last_claim_mint != mint
            and utility <= self.last_claim_utility
        ):
            return
        decision = ChoiceDecision(
            mint=mint,
            observed_ns=now_ns,
            utility=utility,
            margin=margin,
            rank=rank,
            mode=mode,
            features=dict(features),
        )
        existing = self.decisions.get(mint)
        if existing is None or now_ns < existing.observed_ns:
            self.decisions[mint] = decision
            self.last_claim_ns = now_ns
            self.last_claim_utility = utility
            self.last_claim_mint = mint

    def decision(self, mint: str, now_ns: int) -> ChoiceDecision | None:
        if not self.active or self.model is None:
            return None
        with self.lock:
            decision = self.decisions.get(str(mint))
            token = self.tokens.get(str(mint))
            if decision is None or token is None:
                return None
            if token.sell_count > 0:
                return None
            age_ms = max(0.0, (now_ns - decision.observed_ns) / 1e6)
            if age_ms > self.model.maximum_age_ms:
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


def _load_runtime() -> CausalChoiceRuntime:
    if not ENABLED:
        return CausalChoiceRuntime(None, {})
    model_payload = _read_json(MODEL_PATH)
    state_payload = _read_json(STATE_PATH)
    model = ConditionalChoiceModel(model_payload)
    return CausalChoiceRuntime(model, state_payload)


RUNTIME = _load_runtime()

_PREVIOUS_OBSERVE_MARKET_EVENT = role_model.observe_market_event


def _observe_market_event_causal_v12(event: Any) -> None:
    RUNTIME.observe_pre(event)
    _PREVIOUS_OBSERVE_MARKET_EVENT(event)
    RUNTIME.observe_post(event)


role_model.observe_market_event = _observe_market_event_causal_v12

_PREVIOUS_RESET_REPLAY = role_model.reset_role_model_replay


def _reset_role_model_replay_causal_v12(mint: str) -> None:
    RUNTIME.reset_mint(mint)
    _PREVIOUS_RESET_REPLAY(mint)


role_model.reset_role_model_replay = _reset_role_model_replay_causal_v12

_PREVIOUS_ENTRY = core.E4Policy.entry


def _entry_causal_v12(self: Any, state: Any):
    now_ns = _integer(getattr(state, "latest_ns", 0)) or time.time_ns()
    decision = RUNTIME.decision(str(state.mint), now_ns)
    if decision is None:
        return _PREVIOUS_ENTRY(self, state)
    features = dict(v12.v8._identity_features(state))
    features.update(
        {
            "v12_causal_preimpact": 1.0,
            "v12_causal_utility": decision.utility,
            "v12_causal_margin": decision.margin,
            "v12_causal_rank": float(decision.rank),
            "v12_causal_decision_ns": float(decision.observed_ns),
            "v12_causal_mode_authority": float(
                decision.mode == "LAUNCH_AUTHORITY"
            ),
            "v12_causal_mode_wallet_cluster": float(
                decision.mode == "WALLET_CLUSTER"
            ),
        }
    )
    score = min(0.985, max(0.84, 0.90 + 0.02 * min(4.0, decision.margin)))
    fraction = min(
        _finite(os.getenv("E4_V12_CAUSAL_ENTRY_FRACTION"), 0.0185),
        _finite(getattr(self.settings, "max_position_fraction", 0.20), 0.20),
    )
    return v12._make_profile(
        self,
        state,
        features,
        family=FAMILY,
        score=score,
        fraction=fraction,
        reason=(
            f"causal pre-impact {decision.mode.lower()} choice "
            f"utility={decision.utility:.4f} margin={decision.margin:.4f}"
        ),
    )


core.E4Policy.entry = _entry_causal_v12

_PREVIOUS_EXIT = core.E4Policy.exit


def _exit_causal_v12(self: Any, position: Any, state: Any):
    profile = v6._PROFILE_BY_MINT.get(str(position.mint))
    if str(getattr(profile, "family", "") or "") != FAMILY:
        return _PREVIOUS_EXIT(self, position, state)

    source = PIPELINES.e4_signal(position.mint)
    if source is not None and _finite(getattr(source, "entry_tokens", 0.0)) > 0:
        entry_tokens = max(0.0, _finite(source.entry_tokens))
        source_remaining = max(0.0, _finite(source.remaining_tokens))
        target_sold = min(1.0, max(0.0, 1.0 - source_remaining / entry_tokens))
        original = max(0.0, _finite(getattr(position, "tokens", 0.0)))
        remaining = max(0.0, _finite(getattr(position, "remaining", 0.0)))
        gambit_sold = min(1.0, max(0.0, 1.0 - remaining / original)) if original > 0 else 0.0
        if bool(getattr(source, "fully_exited", False)) or target_sold >= 0.985:
            return "SELL_ALL", 1.0, "E4 V12 causal entry source fully exited"
        additional = max(0.0, target_sold - gambit_sold)
        fraction = min(1.0, additional / max(1e-12, 1.0 - gambit_sold))
        if fraction >= 0.01:
            return (
                "SELL_PARTIAL",
                fraction,
                "E4 V12 causal entry cumulative source-exit mirror",
            )
        return "HOLD", 0.0, "E4 V12 causal entry confirmed; awaiting source exit"

    opened_ns = _integer(getattr(position, "opened_ns", 0))
    now_ns = _integer(getattr(state, "latest_ns", 0)) or time.time_ns()
    confirmation_ms = max(
        100.0,
        _finite(os.getenv("E4_V12_CAUSAL_CONFIRMATION_MS"), 1_500.0),
    )
    if opened_ns > 0 and (now_ns - opened_ns) / 1e6 < confirmation_ms:
        return "HOLD", 0.0, "E4 V12 causal entry awaiting E4 confirmation"
    return _PREVIOUS_EXIT(self, position, state)


core.E4Policy.exit = _exit_causal_v12
