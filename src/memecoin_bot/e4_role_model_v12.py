from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import os
import time
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping
from urllib.parse import urlparse, urlunparse

from . import e4_hardening_v12 as v12
from . import e4_pipeline_manager_v11 as manager_module
from . import e4_pipeline_runtime_v10 as pipeline_runtime
from .e4_pipelines_v10 import E4_WALLET

core = v12.core
v6 = v12.v6
v8 = v12.v8
PIPELINES = v12.PIPELINES
LOGGER = logging.getLogger("gambit.e4.role-model.v12")

ROLE_MODEL_FAMILY = "e4_confirmed_fast_copy"
LEGACY_ROLE_MODEL_FAMILY = "e4_teacher_confirmed_copy_safe"
_DEFAULT_COPY_MAX_AGE_MS = 500.0
_DEFAULT_CREATOR_MAX_AGE_MS = 400.0
_DEFAULT_RECENT_PROFILE_SECONDS = 86_400.0


def policy_fingerprint() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def assert_policy_fingerprint(expected: str) -> None:
    expected = str(expected or "").strip().lower()
    actual = policy_fingerprint()
    if not expected or expected != actual:
        raise RuntimeError(
            "E4 V12 role-model policy fingerprint mismatch "
            f"expected={expected or '<missing>'} actual={actual}"
        )


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


def _kind(value: Any) -> str:
    return str(getattr(value, "value", value) or "").upper()


def _context(mint: str) -> dict[str, Any]:
    return v6._CONTEXT_BY_MINT.setdefault(str(mint), {})


def _config_section(name: str) -> dict[str, Any]:
    value = getattr(v12, "_CFG", {}).get(name)
    return dict(value) if isinstance(value, Mapping) else {}


def _config_float(section: str, key: str, default: float) -> float:
    return _finite(_config_section(section).get(key), default)


def _config_int(section: str, key: str, default: int) -> int:
    return _integer(_config_section(section).get(key), default)


def _copy_fraction(entry_sol: float) -> float:
    """Map the observed E4 absolute stake band to V12's discrete risk tiers."""
    amount = max(0.0, _finite(entry_sol))
    if amount >= 6.0:
        return 0.05
    if amount >= 3.0:
        return 0.03
    if amount >= 1.5:
        return 0.0185
    return 0.0125


def _remember_once(event: Any) -> bool:
    mint = str(getattr(event, "mint", "") or "")
    if not mint:
        return False
    context = _context(mint)
    seen = context.setdefault("v12_role_model_event_keys", set())
    kind = _kind(getattr(event, "kind", None))
    key = (
        kind,
        str(getattr(event, "signature", "") or ""),
        _integer(getattr(event, "event_index", 0)),
        round(_finite(getattr(event, "token_amount", 0.0)), 9),
        str(getattr(event, "trader", "") or ""),
    )
    if key in seen:
        return False
    seen.add(key)
    if len(seen) > 512:
        context["v12_role_model_event_keys"] = set(tuple(seen)[-256:])
    return True


def observe_market_event(event: Any) -> None:
    """Feed the three V12 pipelines from the exact primary Pump event path.

    CREATE/BUY/SELL updates the creator learner's current launch state. An E4
    wallet BUY is registered before the policy evaluates that same event, which
    is the only faithful way to cover first-seen creators without inventing a
    generic public-flow strategy. E4 SELL events update the mirror target.
    """
    mint = str(getattr(event, "mint", "") or "")
    if not mint or not _remember_once(event):
        return
    kind = _kind(getattr(event, "kind", None))
    received_ns = _integer(getattr(event, "received_ns", 0)) or time.time_ns()
    price_sol = max(0.0, _finite(getattr(event, "price_sol", 0.0)))
    trader = str(getattr(event, "trader", "") or "")
    context = _context(mint)
    creator = str(
        getattr(event, "creator", "")
        or context.get("creator")
        or ""
    )

    try:
        if kind == "CREATE":
            if creator:
                context["creator"] = creator
            PIPELINES.observe_launch_event(
                mint=mint,
                creator=creator,
                received_ns=received_ns,
                price_sol=price_sol,
            )
            return

        if kind in {"BUY", "SELL", "PUMPSWAP_BUY", "PUMPSWAP_SELL"}:
            is_buy = kind in {"BUY", "PUMPSWAP_BUY"}
            PIPELINES.observe_trade_event(
                mint=mint,
                received_ns=received_ns,
                price_sol=price_sol,
                is_buy=is_buy,
            )
        else:
            return

        if trader != E4_WALLET:
            return

        token_amount = max(0.0, _finite(getattr(event, "token_amount", 0.0)))
        signature = str(getattr(event, "signature", "") or "")
        if kind in {"BUY", "PUMPSWAP_BUY"}:
            PIPELINES.observe_e4_entry(
                {
                    "kind": "e4_buy",
                    "mint": mint,
                    "creator": creator,
                    "observed_ns": received_ns,
                    "entry_price_sol": price_sol,
                    "entry_sol": max(0.0, _finite(getattr(event, "sol_amount", 0.0))),
                    "token_amount": token_amount,
                    "signature": signature,
                    "source": "primary-pump-event-v12",
                }
            )
            context["v12_role_model_entry_ns"] = received_ns
        else:
            PIPELINES.observe_e4_exit(
                mint,
                token_amount=token_amount,
                observed_ns=received_ns,
                signature=signature,
            )
            context["v12_role_model_last_exit_ns"] = received_ns
    except Exception:
        LOGGER.exception("E4 V12 role-model event rejected mint=%s kind=%s", mint, kind)


_PREVIOUS_FROM_ROW = core.Event.from_row.__func__


def _from_row_role_model_v12(cls: type[Any], row: Mapping[str, Any]) -> Any:
    event = _PREVIOUS_FROM_ROW(cls, row)
    observe_market_event(event)
    return event


core.Event.from_row = classmethod(_from_row_role_model_v12)


def reset_role_model_replay(mint: str) -> None:
    """Reset only per-mint live state before each independent holdout replay."""
    mint = str(mint or "")
    if not mint:
        return
    lock = getattr(PIPELINES, "_lock", None)
    if lock is None:
        current = dict(getattr(PIPELINES, "_e4_entries", {}))
        current.pop(mint, None)
        PIPELINES._e4_entries = MappingProxyType(current)
    else:
        with lock:
            current = dict(getattr(PIPELINES, "_e4_entries", {}))
            current.pop(mint, None)
            PIPELINES._e4_entries = MappingProxyType(current)
    getattr(PIPELINES, "_learning", {}).pop(mint, None)
    v6._PROFILE_BY_MINT.pop(mint, None)
    context = _context(mint)
    for key in tuple(context):
        if key.startswith("v12_role_model_") or key.startswith("e4_copy_"):
            context.pop(key, None)


# Correct the manager's E4-copy time semantics while preserving all creator,
# social/narrative and authenticated-intent behaviour.
_PREVIOUS_DECIDE_LAUNCH = type(PIPELINES).decide_launch


def _decide_launch_role_model_v12(
    self: Any,
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
) -> manager_module.PipelineDecision:
    started = time.perf_counter_ns()
    now = int(now_ns or time.time_ns())
    evidence: dict[str, Any] = {}

    plain = self._plain_intent(creator, mint, now)
    signed = self.intents.match(creator, mint, now)
    if plain is not None or signed is not None:
        if signed is not None:
            self.intents.consume(signed.intent_id)
        return manager_module.PipelineDecision(
            True,
            "authorized_prearmed_launch",
            0.97,
            0.10,
            "authorized_prearmed_launch identity authority",
            evidence,
            time.perf_counter_ns() - started,
        )

    profile = self.creators.lookup(creator)
    if profile is not None:
        evidence["creator_tier"] = profile.tier.name
        evidence["creator_score"] = profile.score
        if profile.negative:
            return manager_module.PipelineDecision(
                False,
                "negative_creator",
                0.0,
                0.0,
                "negative creator history identity veto",
                evidence,
                time.perf_counter_ns() - started,
            )
        if profile.tier.name == "ELITE":
            return manager_module.PipelineDecision(
                True,
                "elite_recurring_creator",
                max(0.94, profile.score),
                0.05,
                "elite_recurring_creator identity fast path",
                evidence,
                time.perf_counter_ns() - started,
            )
        if profile.approved:
            fraction = 0.03 if profile.trades >= 3 else 0.0185
            family = "proven_repeat_creator" if profile.trades >= 3 else "prior_e4_winning_creator"
            return manager_module.PipelineDecision(
                True,
                family,
                max(0.82, profile.score),
                fraction,
                f"{family} identity fast path",
                evidence,
                time.perf_counter_ns() - started,
            )

    if sell_count == 0 and (not fdv_usd or fdv_usd <= 15_000):
        for signal in self._social_by_ca.get(mint, ()):
            age = now - int(signal.created_ns)
            if (
                signal.created_ns >= launch_ns
                and 0 <= age <= self.direct_ca_max_age_ns
                and signal.authority >= 0.90
            ):
                evidence["social_authority"] = signal.authority
                evidence["social_signal_id"] = signal.signal_id
                return manager_module.PipelineDecision(
                    True,
                    "exact_ca_social_launch",
                    0.93,
                    0.03,
                    "exact_ca_social_launch identity-linked social authority",
                    evidence,
                    time.perf_counter_ns() - started,
                )

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
        return manager_module.PipelineDecision(
            True,
            "preannounced_social_community_launch",
            max(0.86, match.score),
            0.03,
            "preannounced social identity/narrative authority",
            evidence,
            time.perf_counter_ns() - started,
        )

    source = self.e4_signal(mint)
    if source is not None:
        e4_confirmed = not source.fully_exited
        e4_observed_ns = source.observed_ns
        e4_entry_price = source.entry_price_sol
    if e4_confirmed:
        age = now - int(e4_observed_ns or now)
        drift = 0.0
        if e4_entry_price > 0 and price_sol > 0:
            drift = max(0.0, price_sol / e4_entry_price - 1.0)
        max_copy_age_ns = max(
            int(getattr(self, "copy_max_age_ns", 0) or 0),
            int(_config_float("limits", "copy_max_age_ms", _DEFAULT_COPY_MAX_AGE_MS) * 1_000_000),
        )
        price_known_or_same_event = e4_entry_price > 0 or age <= 100_000_000
        if (
            0 <= age <= max_copy_age_ns
            and drift <= 0.08
            and sell_count == 0
            and price_known_or_same_event
        ):
            evidence["e4_age_ms"] = age / 1e6
            evidence["e4_price_drift"] = drift
            evidence["e4_primary_event"] = bool(source and source.entry_sol > 0)
            return manager_module.PipelineDecision(
                True,
                ROLE_MODEL_FAMILY,
                0.94,
                _copy_fraction(source.entry_sol if source is not None else 0.0),
                "copy-safe E4 role-model confirmation",
                evidence,
                time.perf_counter_ns() - started,
            )

    return manager_module.PipelineDecision(
        False,
        "identity_only_reject",
        0.0,
        0.0,
        "identity-only gate: no approved creator, prelaunch narrative, or fresh E4 confirmation",
        evidence,
        time.perf_counter_ns() - started,
    )


type(PIPELINES).decide_launch = _decide_launch_role_model_v12
PIPELINES.copy_max_age_ns = max(
    int(getattr(PIPELINES, "copy_max_age_ns", 0) or 0),
    int(_config_float("limits", "copy_max_age_ms", _DEFAULT_COPY_MAX_AGE_MS) * 1_000_000),
)


# The historical creator path remains selective, but its decision horizon must
# include the profitable next-slot E4 entries observed around 180-400ms.
_limits = getattr(v12, "_CFG", {}).setdefault("limits", {})
for _key in ("elite_max_age_ms", "proven_max_age_ms", "social_max_age_ms"):
    _limits[_key] = max(_finite(_limits.get(_key), 0.0), _DEFAULT_CREATOR_MAX_AGE_MS)
_limits["copy_max_age_ms"] = max(
    _finite(_limits.get("copy_max_age_ms"), 0.0),
    _DEFAULT_COPY_MAX_AGE_MS,
)


_PREVIOUS_ENTRY = core.E4Policy.entry


def _entry_role_model_v12(self: Any, state: Any):
    if state.complete or state.migrated or state.wallet_touched or state.created_ns is None:
        return _PREVIOUS_ENTRY(self, state)

    features = dict(v8._identity_features(state))
    fdv = _finite(features.get("fdv_usd") or getattr(state, "fdv_usd", 0.0))
    age_ms = _finite(features.get("age_ms"))
    sell_count = _integer(features.get("sell_count"))
    sell_sol = _finite(features.get("sell_sol"))
    max_fdv = min(
        _finite(getattr(self.settings, "max_entry_fdv_usd", 8500.0), 8500.0),
        _config_float("limits", "max_entry_fdv_usd", 8500.0),
    )

    source = PIPELINES.e4_signal(state.mint)
    if source is not None and not source.fully_exited:
        now_ns = int(getattr(state, "latest_ns", 0) or time.time_ns())
        source_age_ns = now_ns - int(source.observed_ns or now_ns)
        drift = 0.0
        current_price = _finite(getattr(state, "price_sol", 0.0))
        if source.entry_price_sol > 0 and current_price > 0:
            drift = max(0.0, current_price / source.entry_price_sol - 1.0)
        max_copy_age_ns = int(
            _config_float("limits", "copy_max_age_ms", _DEFAULT_COPY_MAX_AGE_MS) * 1_000_000
        )
        if (
            0 <= source_age_ns <= max_copy_age_ns
            and 0.0 < fdv <= max_fdv
            and sell_count == 0
            and sell_sol <= 0.0
            and drift <= 0.08
            and (source.entry_price_sol > 0 or source_age_ns <= 100_000_000)
        ):
            score = min(0.985, 0.94 + 0.04 * min(1.0, max(0.0, source.entry_sol) / 6.0))
            fraction = min(_copy_fraction(source.entry_sol), self.settings.max_position_fraction)
            features.update(
                {
                    "v12_role_model_copy": 1.0,
                    "v12_role_model_age_ms": source_age_ns / 1e6,
                    "v12_role_model_entry_sol": max(0.0, source.entry_sol),
                    "v12_role_model_price_drift": drift,
                }
            )
            return v12._make_profile(
                self,
                state,
                features,
                family=ROLE_MODEL_FAMILY,
                score=score,
                fraction=fraction,
                reason="direct E4 role-model BUY observed on primary event path",
            )

    previous = _PREVIOUS_ENTRY(self, state)
    if previous[0]:
        return previous

    creator = str(
        _context(state.mint).get("creator")
        or getattr(state, "creator", "")
        or ""
    )
    profile = PIPELINES.creators.lookup(creator) if creator else None
    recent = _config_section("recent_e4_repeat")
    profile_source = str(getattr(profile, "source", "") or "").lower()
    profile_updated_ns = int(getattr(profile, "updated_ns", 0) or 0)
    now_ns = int(getattr(state, "latest_ns", 0) or time.time_ns())
    profile_age_seconds = (
        max(0.0, (now_ns - profile_updated_ns) / 1e9)
        if profile_updated_ns > 0 and now_ns >= profile_updated_ns
        else float("inf")
    )
    wins = int(getattr(profile, "wins", 0) or 0)
    trades = max(int(getattr(profile, "trades", 0) or 0), wins + int(getattr(profile, "losses", 0) or 0))
    win_rate = _finite(getattr(profile, "gross_win_rate", 0.0))
    gross_pnl = _finite(getattr(profile, "gross_pnl_sol", 0.0))
    creator_seed = _finite(features.get("creator_buy_sol"))

    live_source = profile_source.startswith("live-e4")
    recent_profile = profile_age_seconds <= _finite(
        recent.get("max_profile_age_seconds"), _DEFAULT_RECENT_PROFILE_SECONDS
    )
    if (
        profile is not None
        and live_source
        and recent_profile
        and wins >= _integer(recent.get("min_wins"), 1)
        and trades >= _integer(recent.get("min_trades"), 1)
        and win_rate >= _finite(recent.get("min_win_rate"), 0.50)
        and gross_pnl > _finite(recent.get("min_gross_pnl_sol"), 0.0)
        and creator_seed >= _finite(recent.get("min_creator_seed_sol"), 1.0)
        and age_ms <= _finite(recent.get("max_age_ms"), _DEFAULT_CREATOR_MAX_AGE_MS)
        and fdv >= _finite(recent.get("min_entry_fdv_usd"), 3000.0)
        and fdv <= min(max_fdv, _finite(recent.get("max_entry_fdv_usd"), 8500.0))
        and sell_count == 0
        and sell_sol <= 0.0
    ):
        quality = min(
            1.0,
            0.40 * win_rate
            + 0.25 * min(1.0, creator_seed / 3.0)
            + 0.20 * v6._fdv_fit(fdv)
            + 0.15 * min(1.0, wins / 3.0),
        )
        features.update(
            {
                "v12_recent_e4_repeat": 1.0,
                "v12_recent_e4_profile_age_seconds": profile_age_seconds,
                "v12_recent_e4_wins": float(wins),
                "v12_recent_e4_trades": float(trades),
                "v12_recent_e4_win_rate": win_rate,
                "v12_recent_e4_gross_pnl_sol": gross_pnl,
                "v12_recent_e4_quality": quality,
            }
        )
        return v12._make_profile(
            self,
            state,
            features,
            family="v12_recent_e4_repeat_launch",
            score=min(0.965, 0.84 + 0.12 * quality),
            fraction=min(0.0185, self.settings.max_position_fraction),
            reason="recent live-E4 creator outcome + strong current creator seed",
        )

    return previous


core.E4Policy.entry = _entry_role_model_v12


_PREVIOUS_EXIT = core.E4Policy.exit


def _exit_role_model_v12(self: Any, position: Any, state: Any):
    profile = v6._PROFILE_BY_MINT.get(position.mint)
    family = str(getattr(profile, "family", "") or "")
    if family not in {ROLE_MODEL_FAMILY, LEGACY_ROLE_MODEL_FAMILY}:
        return _PREVIOUS_EXIT(self, position, state)

    source = PIPELINES.e4_signal(position.mint)
    if source is not None and source.entry_tokens > 0:
        target_sold = min(
            1.0,
            max(0.0, 1.0 - max(0.0, source.remaining_tokens) / source.entry_tokens),
        )
        original_tokens = max(0.0, _finite(getattr(position, "tokens", 0.0)))
        remaining_tokens = max(0.0, _finite(getattr(position, "remaining", 0.0)))
        gambit_sold = (
            min(1.0, max(0.0, 1.0 - remaining_tokens / original_tokens))
            if original_tokens > 0
            else 0.0
        )
        if source.fully_exited or target_sold >= 0.985:
            return "SELL_ALL", 1.0, "E4 V12 role-model fully exited"
        additional_original = max(0.0, target_sold - gambit_sold)
        remaining_original = max(1e-12, 1.0 - gambit_sold)
        fraction_of_remaining = min(1.0, additional_original / remaining_original)
        if fraction_of_remaining >= 0.01:
            return (
                "SELL_PARTIAL",
                fraction_of_remaining,
                "E4 V12 role-model cumulative sell mirror "
                f"target={target_sold:.2%} gambit={gambit_sold:.2%}",
            )

    # Independent failure protection remains active before/alongside E4's sells.
    return _PREVIOUS_EXIT(self, position, state)


core.E4Policy.exit = _exit_role_model_v12


def _csv_env(name: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(part.strip() for part in os.getenv(name, "").split(",") if part.strip()))


def _http_to_ws(url: str) -> str:
    text = str(url or "").strip()
    if not text:
        return ""
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"}:
        return text if parsed.scheme in {"ws", "wss"} else ""
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return urlunparse(parsed._replace(scheme=scheme))


_PREVIOUS_RUNTIME_INIT = pipeline_runtime.PipelineRuntime.__init__


def _runtime_init_role_model_v12(self: Any) -> None:
    _PREVIOUS_RUNTIME_INIT(self)
    rpc_urls = (
        _csv_env("E4_PIPELINE_SOLANA_RPC_URLS")
        + _csv_env("E4_PRIMARY_RPC_URL")
        + _csv_env("E4_FALLBACK_RPC_URLS")
        + _csv_env("HELIUS_RPC_URL")
        + _csv_env("SOLANA_RPC_URL")
        + tuple(getattr(self, "rpc_urls", ()))
    )
    self.rpc_urls = tuple(dict.fromkeys(url for url in rpc_urls if url))

    explicit_ws = _csv_env("E4_PIPELINE_SOLANA_WS_URLS")
    if explicit_ws:
        self.ws_urls = explicit_ws
    elif not tuple(getattr(self, "ws_urls", ())):
        derived = tuple(
            filter(
                None,
                (
                    _http_to_ws(url)
                    for url in (
                        _csv_env("E4_PRIMARY_RPC_URL")
                        + _csv_env("E4_FALLBACK_RPC_URLS")
                        + _csv_env("HELIUS_RPC_URL")
                        + _csv_env("SOLANA_RPC_URL")
                    )
                ),
            )
        )
        self.ws_urls = tuple(
            dict.fromkeys(
                derived
                + (
                    "wss://api.mainnet-beta.solana.com",
                    "wss://solana-rpc.publicnode.com",
                )
            )
        )


pipeline_runtime.PipelineRuntime.__init__ = _runtime_init_role_model_v12

_PREVIOUS_RUNTIME_RUN = pipeline_runtime.PipelineRuntime.run


async def _runtime_run_role_model_v12(self: Any) -> None:
    self._v12_owner_loop = asyncio.get_running_loop()
    try:
        await _PREVIOUS_RUNTIME_RUN(self)
    finally:
        self._v12_owner_loop = None


pipeline_runtime.PipelineRuntime.run = _runtime_run_role_model_v12

_PREVIOUS_RUNTIME_SNAPSHOT = pipeline_runtime.runtime_snapshot


def _runtime_snapshot_role_model_v12() -> dict[str, Any]:
    payload = dict(_PREVIOUS_RUNTIME_SNAPSHOT())
    runtime = getattr(pipeline_runtime, "_RUNTIME", None)
    payload["role_model_copy"] = {
        "wallet": E4_WALLET,
        "logs_ws_count": len(tuple(getattr(runtime, "ws_urls", ()))) if runtime else 0,
        "transaction_ws_count": len(tuple(getattr(runtime, "transaction_ws_urls", ()))) if runtime else 0,
        "copy_ready": bool(
            runtime
            and (
                tuple(getattr(runtime, "ws_urls", ()))
                or tuple(getattr(runtime, "transaction_ws_urls", ()))
            )
        ),
    }
    return payload


pipeline_runtime.runtime_snapshot = _runtime_snapshot_role_model_v12


def stop_background_supervisor() -> None:
    runtime = getattr(pipeline_runtime, "_RUNTIME", None)
    thread = getattr(pipeline_runtime, "_THREAD", None)
    if runtime is not None:
        loop = getattr(runtime, "_v12_owner_loop", None)
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(runtime.stop_event.set)
        else:
            try:
                runtime.stop()
            except Exception:
                LOGGER.exception("Could not stop E4 V12 pipeline runtime")
    if thread is not None:
        thread.join(timeout=2.0)
    pipeline_runtime._RUNTIME = None
    pipeline_runtime._THREAD = None
