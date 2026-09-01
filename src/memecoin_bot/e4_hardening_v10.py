from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from . import e4_hardening_v9 as v9
from .e4_pipeline import ActiveNarrativeCache, AtomicCreatorRegistry, PipelineCoordinator, SocialPost, V10Runtime
from .e4_pipelines_v10 import manager as PIPELINES

core = v9.core
final = v9.final
v8 = v9.v8
v6 = v9.v6
LOGGER = logging.getLogger("gambit.e4.hardening.v10")


class _NullTeacher:
    def copy_signal(self, *_: Any, **__: Any) -> None:
        return None


_FALLBACK_REGISTRY = AtomicCreatorRegistry(
    Path(os.getenv("E4_CREATOR_EXPECTANCY_PATH", "models/e4/e4-creator-expectancy.json")),
    Path(os.getenv("E4_DISCOVERED_CREATORS_PATH", "models/e4/e4-discovered-creators.json")),
)
_FALLBACK_NARRATIVES = ActiveNarrativeCache()
_FALLBACK_COORDINATOR = PipelineCoordinator(
    registry=_FALLBACK_REGISTRY,
    narratives=_FALLBACK_NARRATIVES,
    teacher=_NullTeacher(),
    fraction_resolver=v6.relative_fraction_for_score,
)


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _merged(row: Mapping[str, Any]) -> dict[str, Any]:
    output = dict(row)
    for key in ("payload_json", "event_json", "raw_json", "data_json", "payload", "metadata"):
        for nested_key, nested_value in _mapping(output.get(key)).items():
            output.setdefault(str(nested_key), nested_value)
    return output


def _first(row: Mapping[str, Any], *keys: str) -> Any:
    lowered = {str(key).lower(): value for key, value in row.items()}
    for key in keys:
        value = lowered.get(key.lower())
        if value not in (None, ""):
            return value
    return None


def _host(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "unknown"
    parsed = urlparse(text)
    return (parsed.netloc or parsed.path.split("/", 1)[0] or "unknown").lower()


def observe_launch_context(mint: str, row: Mapping[str, Any]) -> dict[str, Any]:
    if not mint:
        return {}
    merged = _merged(row)
    context = v6._CONTEXT_BY_MINT.setdefault(mint, {})
    creator = _first(merged, "creator", "creator_wallet", "deployer", "user")
    if creator:
        context["creator"] = str(creator)
    name = _first(merged, "name", "token_name")
    symbol = _first(merged, "symbol", "ticker", "token_symbol")
    description = _first(merged, "description", "token_description")
    uri = _first(merged, "uri", "metadata_uri", "metadata_url")
    if name:
        context["name"] = str(name)
    if symbol:
        context["symbol"] = str(symbol)
    if description:
        context["description"] = str(description)
    if uri:
        context["uri"] = str(uri)
        context["metadata_host"] = _host(uri)
    for source_key in ("launch_source", "source_provider", "provider", "origin"):
        source = _first(merged, source_key)
        if source:
            context["launch_source"] = str(source).lower()
            break
    for key in ("prearmed", "authorized_sniper", "sniper_authorized", "launch_intent", "prelaunch_intent", "j7_sniper_authorized"):
        value = _first(merged, key)
        if value not in (None, ""):
            context["prearmed"] = bool(context.get("prearmed") or (value if isinstance(value, bool) else str(value).lower() in {"1", "true", "yes", "on"}))
    return context


_previous_from_row = core.Event.from_row.__func__


def _from_row_v10(cls: type[core.Event], row: Mapping[str, Any]) -> core.Event:
    event = _previous_from_row(cls, row)
    observe_launch_context(event.mint, row)
    return event


core.Event.from_row = classmethod(_from_row_v10)
_previous_state_apply = core.TokenState.apply


def _state_apply_v10(self: core.TokenState, event: core.Event, wallet: str | None) -> None:
    _previous_state_apply(self, event, wallet)
    context = v6._CONTEXT_BY_MINT.setdefault(self.mint, {})
    received_ns = int(event.received_ns or time.time_ns())
    context["last_received_ns"] = received_ns
    if event.kind == core.EventKind.CREATE:
        context.setdefault("create_received_ns", received_ns)
    if event.creator:
        context["creator"] = event.creator
    elif self.creator:
        context["creator"] = self.creator


core.TokenState.apply = _state_apply_v10


def ingest_social_post(post: SocialPost, runtime: V10Runtime | None = None) -> int:
    target = runtime.narratives if runtime is not None else _FALLBACK_NARRATIVES
    return target.observe(post)


_previous_policy_init = core.E4Policy.__init__


def _policy_init_v10(self: core.E4Policy, settings: core.Settings) -> None:
    _previous_policy_init(self, settings)
    self.v10_runtime = None


core.E4Policy.__init__ = _policy_init_v10


def _entry_v10(self: core.E4Policy, state: core.TokenState) -> tuple[bool, float, float, str, dict[str, float]]:
    context = v6._CONTEXT_BY_MINT.setdefault(state.mint, {})
    features = dict(v8._identity_features(state))
    # Keep the observed V4/V5 burst telemetry even though public flow no
    # longer has entry authority. These metrics are useful for forensics and
    # regression testing without reintroducing the failed flow-only strategy.
    buy_events = [
        event for event in state.events
        if event.kind in {core.EventKind.BUY, core.EventKind.PUMPSWAP_BUY}
    ]
    features["microburst_buyers"] = float(len({event.trader for event in buy_events if event.trader}))
    signature_counts: dict[str, int] = {}
    for event in buy_events:
        if event.signature:
            signature_counts[event.signature] = signature_counts.get(event.signature, 0) + 1
    features["microburst_bundled_buys"] = float(sum(
        count for count in signature_counts.values() if count > 1
    ))
    creator = str(context.get("creator") or getattr(state, "creator", "") or "")
    launch_ns = int(context.get("create_received_ns") or getattr(state, "first_ns", 0) or getattr(state, "latest_ns", 0) or time.time_ns())
    now_ns = int(context.get("last_received_ns") or getattr(state, "latest_ns", 0) or time.time_ns())
    creator_seed = float(features.get("creator_buy_sol", 0.0) or 0.0)
    current_fdv = float(getattr(state, "fdv_usd", 0.0) or 0.0)
    features["entry_fdv_usd"] = current_fdv
    if current_fdv > 0 and current_fdv > self.settings.max_entry_fdv_usd:
        return False, 0.0, 0.0, "entry FDV above observed E4 envelope", features

    # A cooperating/prearmed launch is an explicit authority path and must be
    # evaluated before any historical negative record.
    if bool(context.get("prearmed")):
        decision_family = "authorized_prearmed_launch"
        decision_score = 0.97
        decision_fraction = 0.10
        decision_reason = "authorized_prearmed_launch identity authority"
        decision_ns = 0
        decision_evidence: dict[str, Any] = {"prearmed": True}
    else:
        # Preserve the causal V9 test/model interface while V11's registry is
        # the production source of truth. This also lets older model snapshots
        # remain valid during a rolling deployment.
        legacy = v9._EXPECTANCY_CREATORS.get(creator) if creator else None
        if isinstance(legacy, Mapping):
            wins = int(legacy.get("wins") or 0)
            losses = int(legacy.get("losses") or 0)
            trades = max(int(legacy.get("trades") or 0), wins + losses)
            rate = float(legacy.get("gross_win_rate") if legacy.get("gross_win_rate") is not None else (wins / trades if trades else 0.0))
            features["creator_prior_wins"] = float(wins)
            features["creator_prior_losses"] = float(losses)
            if trades >= 3 and rate <= 0.25:
                return False, 0.0, 0.0, "negative creator history identity veto", features
            if trades >= 3 and wins >= 2 and rate >= 0.75:
                decision_family = "proven_repeat_e4_creator"
                decision_score = max(0.93, min(0.99, 0.90 + rate * 0.08))
                decision_fraction = 0.05 if rate >= 0.90 else 0.03
                decision_reason = "proven_repeat_e4_creator identity fast path"
                decision_ns = 0
                decision_evidence = {"creator_tier": "ELITE"}
            elif trades >= 3 and wins >= 2 and rate >= 0.60:
                decision_family = "prior_e4_winning_creator"
                decision_score = max(0.84, min(0.93, 0.82 + rate * 0.08))
                decision_fraction = 0.0185
                decision_reason = "prior_e4_winning_creator identity fast path"
                decision_ns = 0
                decision_evidence = {"creator_tier": "APPROVED"}
            else:
                legacy = None
        if not isinstance(legacy, Mapping) or 'decision_family' not in locals():
            if creator_seed <= 0 and not bool(context.get("e4_confirmed")):
                return False, 0.0, 0.0, "creator seed required before identity evaluation", features
            decision = PIPELINES.decide_launch(
                mint=state.mint,
                creator=creator,
                name=str(context.get("name") or ""),
                symbol=str(context.get("symbol") or ""),
                metadata_uri=str(context.get("uri") or ""),
                launch_ns=launch_ns,
                now_ns=now_ns,
                fdv_usd=float(getattr(state, "fdv_usd", 0.0) or 0.0),
                creator_buy_sol=creator_seed,
                sell_count=int(features.get("sell_count", features.get("sells", 0.0)) or 0),
                price_sol=float(getattr(state, "price_sol", 0.0) or 0.0),
                e4_confirmed=bool(context.get("e4_confirmed")),
                e4_observed_ns=int(context.get("e4_observed_ns") or 0),
                e4_entry_price=float(context.get("e4_entry_price") or 0.0),
            )
            decision_family = decision.family
            decision_score = decision.score
            decision_fraction = decision.fraction
            decision_reason = decision.reason
            decision_ns = decision.decision_ns
            decision_evidence = dict(decision.evidence)
            if not decision.accepted:
                context["v10_decision_duration_ns"] = decision_ns
                features["e4_v10_decision_latency_ns"] = float(decision_ns)
                for key, value in decision_evidence.items():
                    if isinstance(value, bool):
                        features[f"v10_{key}"] = float(value)
                    elif isinstance(value, (int, float)):
                        features[f"v10_{key}"] = float(value)
                return False, 0.0, 0.0, decision_reason, features

    context["v10_decision_completed_ns"] = time.time_ns()
    context["v10_decision_duration_ns"] = int(decision_ns)
    features["e4_v10_decision_latency_ns"] = float(decision_ns)
    features["v10_decision_duration_ms"] = float(decision_ns) / 1_000_000.0
    for key, value in decision_evidence.items():
        if isinstance(value, bool):
            features[f"v10_{key}"] = float(value)
        elif isinstance(value, (int, float)):
            features[f"v10_{key}"] = float(value)

    tier, ladder_fraction = v6.relative_fraction_for_score(float(decision_score))
    fraction = min(float(decision_fraction or ladder_fraction), self.settings.max_position_fraction)
    for candidate in v6._TIER_ORDER:
        if abs(v6._TIER_FRACTIONS[candidate] - fraction) < 1e-12:
            tier = candidate
            break
    profile = v6.EntryProfile(
        family=str(decision_family),
        tier=tier,
        fraction=fraction,
        score=float(decision_score),
        first_partial_fraction=v6._profile_partial(tier),
        features=dict(features),
    )
    v6._PROFILE_BY_MINT[state.mint] = profile
    features.update({
        "e4_v10_score": profile.score,
        "e4_v10_fraction": profile.fraction,
        "e4_v10_tier_index": float(v6._TIER_ORDER.index(tier)),
        "e4_v10_first_partial": profile.first_partial_fraction,
    })
    return True, profile.score, profile.fraction, f"E4_V11 family={profile.family} tier={tier}: {decision_reason}", features


core.E4Policy.entry = _entry_v10

core.E4Policy.entry = _entry_v10
_previous_engine_init = core.Engine.__init__


def _runtime_for(engine: core.Engine) -> V10Runtime:
    runtime = getattr(engine, "v10_runtime", None)
    if runtime is None:
        settings = engine.settings
        runtime = V10Runtime(
            oracle_wallet=settings.oracle_wallet,
            fraction_resolver=v6.relative_fraction_for_score,
            execution_db=settings.execution_db,
        )
        engine.v10_runtime = runtime
        if getattr(engine, "policy", None) is not None:
            engine.policy.v10_runtime = runtime
        engine.v10_runtime_started = False
    return runtime



def _engine_init_v10(self: core.Engine, settings: core.Settings) -> None:
    _previous_engine_init(self, settings)
    runtime = V10Runtime(oracle_wallet=settings.oracle_wallet, fraction_resolver=v6.relative_fraction_for_score, execution_db=settings.execution_db)
    self.v10_runtime = runtime
    self.policy.v10_runtime = runtime
    self.v10_runtime_started = False


core.Engine.__init__ = _engine_init_v10
_previous_on_event = core.Engine.on_event


async def _on_event_v10(self: core.Engine, event: core.Event) -> None:
    context = v6._CONTEXT_BY_MINT.setdefault(event.mint, {})
    runtime: V10Runtime = _runtime_for(self)
    runtime.pre_event(event, context)
    await _previous_on_event(self, event)
    state = self.tokens.get(event.mint)
    if state is not None:
        runtime.post_event(event, state)


core.Engine.on_event = _on_event_v10
_previous_run = core.Engine.run


async def _run_v10(self: core.Engine) -> None:
    runtime: V10Runtime = _runtime_for(self)
    await runtime.start(self)
    self.v10_runtime_started = True
    try:
        await _previous_run(self)
    finally:
        status_path = os.getenv("E4_V10_STATUS_PATH", "").strip()
        if status_path:
            try:
                runtime.write_status(Path(status_path))
            except Exception:
                LOGGER.exception("Could not write E4 V10 status")
        await runtime.close()


core.Engine.run = _run_v10


def _enrich_request(request: Mapping[str, Any]) -> dict[str, Any]:
    enriched = dict(request)
    mint = str(enriched.get("mint") or "")
    if not mint:
        return enriched
    metadata = dict(enriched.get("metadata") or {})
    context = v6._CONTEXT_BY_MINT.get(mint, {})
    curve = v6._CURVE_BY_MINT.get(mint, {})
    profile = v6._PROFILE_BY_MINT.get(mint)
    metadata.update({
        "creator": curve.get("creator") or context.get("creator"),
        "metadata_host": context.get("metadata_host"),
        "launch_source": context.get("launch_source"),
        "token_program": context.get("token_program"),
        "mayhem": bool(context.get("mayhem")),
        "virtual_sol_reserves": curve.get("virtual_sol_reserves"),
        "virtual_token_reserves": curve.get("virtual_token_reserves"),
        "real_sol_reserves": curve.get("real_sol_reserves"),
        "real_token_reserves": curve.get("real_token_reserves"),
        "e4_family": profile.family if profile else None,
        "e4_tier": profile.tier if profile else None,
        "e4_relative_fraction": profile.fraction if profile else None,
        "e4_first_partial_fraction": profile.first_partial_fraction if profile else None,
        "launch_received_ns": context.get("create_received_ns"),
        "decision_completed_ns": context.get("v10_decision_completed_ns"),
    })
    enriched["metadata"] = metadata
    return enriched


async def _execute_v10(self: core.Engine, request_id: str, request: Mapping[str, Any]) -> tuple[str, bool, int | None, str | None]:
    enriched = _enrich_request(request)
    runtime: V10Runtime = _runtime_for(self)
    build_started = time.time_ns()
    unsigned = await self.builder.build(enriched)
    build_completed = time.time_ns()
    runtime.latency.build_done(request_id, build_started, build_completed)
    sign_started = time.time_ns()
    signed, signature = await self.signer.sign(unsigned)
    sign_completed = time.time_ns()
    runtime.latency.sign_done(request_id, sign_started, sign_completed)
    submit_started = time.time_ns()
    runtime.latency.submit_started(request_id, submit_started)
    route, confirmed, slot, error, results = await self.sender.submit(signed, signature)
    route_completed = time.time_ns()
    runtime.latency.route_done(request_id, route_completed)
    mapped = {item.name: item.result if item.accepted else (item.error or "rejected") for item in results}
    self.store.receipt(request_id, signature, route, confirmed, slot, error, mapped)
    for item in results:
        self.store.route_metric(request_id, item.name, item.submitted_ns, item.completed_ns, item.result, item.error)
    return signature, confirmed, slot, error


core.Engine.execute = _execute_v10


async def _execute_buy_v10(self: core.Engine, state: core.TokenState, score: float, fraction: float, reason: str) -> None:
    mint = state.mint
    reserved = 0.0
    runtime: V10Runtime = _runtime_for(self)
    try:
        if self.store.has_entered(mint):
            return
        cache = runtime.balance_cache
        balance = await cache.available(float(os.getenv("E4_BALANCE_CACHE_MAX_STALENESS_MS", "1000"))) if cache is not None else await self.rpc.balance(self.signer.wallet)
        async with self.allocation_lock:
            priority, tip = self.fee_bid(balance * fraction, score)
            deployable = balance - self.settings.reserve_sol - self.reserved_sol - priority - tip
            amount = min(max(0.0, balance * min(fraction, self.settings.max_position_fraction)), max(0.0, deployable), self.settings.max_position_sol)
            if amount < self.settings.min_position_sol:
                return
            reserved = amount + priority + tip
            self.reserved_sol += reserved
            if not self.store.mark_entry(mint, score, reason):
                self.reserved_sol = max(0.0, self.reserved_sol - reserved)
                reserved = 0.0
                return
        request_id = str(uuid.uuid4())
        context = v6._CONTEXT_BY_MINT.get(mint, {})
        launch_received_ns = int(context.get("create_received_ns") or context.get("last_received_ns") or time.time_ns())
        decision_completed_ns = int(context.get("v10_decision_completed_ns") or time.time_ns())
        runtime.latency.begin(request_id, mint=mint, launch_received_ns=launch_received_ns, decision_completed_ns=decision_completed_ns)
        request = {
            "request_id": request_id, "side": "BUY", "mint": mint,
            "public_key": self.signer.wallet, "amount": amount,
            "denominated_in_sol": True, "slippage_bps": self.settings.buy_slippage_bps,
            "priority_fee_sol": priority, "tip_sol": tip, "pool": "pump",
            "metadata": {"score": score, "reason": reason, "fdv_usd": state.fdv_usd, "launch_received_ns": launch_received_ns, "decision_completed_ns": decision_completed_ns},
        }
        self.store.order(request_id, mint, "BUY", amount, None, reason)
        signature, confirmed, _, error = await self.execute(request_id, request)
        if cache is not None:
            cache.apply_estimated_delta(-reserved)
        if not confirmed:
            LOGGER.error("E4 V10 buy failed mint=%s signature=%s error=%s", mint, signature, error)
            if cache is not None:
                try:
                    await cache.refresh()
                except Exception:
                    pass
            return
        after_tokens = await final._token_balance_after_change(self.rpc, self.signer.wallet, mint, 0.0, "up")
        received = max(0.0, after_tokens)
        if received <= 0:
            raise RuntimeError("E4 V10 buy landed but token balance did not become observable")
        entry_price = amount / received
        position = core.Position(
            position_id=str(uuid.uuid4()), mint=mint, status=core.PositionStatus.OPEN,
            opened_ns=time.time_ns(), entry_sol=amount, tokens=received, remaining=received,
            entry_price=entry_price, max_price=state.price_sol or entry_price,
            last_price=state.price_sol or entry_price, entry_signature=signature,
        )
        self.positions[mint] = position
        self.store.save_position(position)
        v6._persist_profile(self, mint)
        LOGGER.info("E4 V10 position opened mint=%s amount_sol=%.9f signature=%s", mint, amount, signature)
    except Exception:
        LOGGER.exception("E4 V10 buy execution error mint=%s", mint)
    finally:
        if reserved:
            async with self.allocation_lock:
                self.reserved_sol = max(0.0, self.reserved_sol - reserved)
        self.pending_entries.discard(mint)


core.Engine.execute_buy = _execute_buy_v10


_previous_exit_v11 = core.E4Policy.exit


def _exit_v11(self: core.E4Policy, position: core.Position, state: core.TokenState):
    profile = v6._PROFILE_BY_MINT.get(position.mint)
    if profile is not None and str(getattr(profile, "family", "")) == "e4_confirmed_fast_copy":
        source = PIPELINES.e4_signal(position.mint)
        if source is not None:
            if source.fully_exited:
                return "SELL_FULL", 1.0, "E4 V11 copy source fully exited"
            if source.last_sell_ns > 0 and not position.first_partial_done:
                fraction = min(0.50, max(0.20, source.last_sell_fraction or profile.first_partial_fraction))
                return "SELL_PARTIAL", fraction, "E4 V11 copy source first partial"
            if source.sell_count >= 2 and position.first_partial_done:
                return "SELL_FULL", 1.0, "E4 V11 copy source second exit leg"
    return _previous_exit_v11(self, position, state)


core.E4Policy.exit = _exit_v11
