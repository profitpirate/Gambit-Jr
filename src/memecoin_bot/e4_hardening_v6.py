from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from . import e4_hardening_v5

core = e4_hardening_v5.core
final = e4_hardening_v5.final
v4 = e4_hardening_v5.e4_hardening_v4
LOGGER = logging.getLogger("gambit.e4.hardening.v6")


@dataclass(frozen=True, slots=True)
class EntryProfile:
    family: str
    tier: str
    fraction: float
    score: float
    first_partial_fraction: float
    features: Mapping[str, float]


# Fractions are reconstructed from the exactly balance-reconciled E4 sample.
# They approximate the median entry/pre-trade-liquid-balance ratio in each
# observed absolute-size band. The engine never invents a larger continuous
# 5%-20% stake simply because a score moved by a few points.
_TIER_ORDER = ("probe", "standard", "strong", "high", "elite", "exceptional")
_TIER_FRACTIONS = {
    "probe": 0.0075,
    "standard": 0.0125,
    "strong": 0.0185,
    "high": 0.0300,
    "elite": 0.0500,
    "exceptional": 0.1000,
}
_TIER_MIN_SCORE = {
    "probe": 0.72,
    "standard": 0.76,
    "strong": 0.82,
    "high": 0.88,
    "elite": 0.93,
    "exceptional": 0.975,
}
_PROFILE_BY_MINT: dict[str, EntryProfile] = {}
_CONTEXT_BY_MINT: dict[str, dict[str, Any]] = {}
_CURVE_BY_MINT: dict[str, dict[str, Any]] = {}


def _finite(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


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


def _host(uri: Any) -> str:
    text = str(uri or "").strip()
    if not text:
        return "unknown"
    parsed = urlparse(text)
    return (parsed.netloc or parsed.path.split("/", 1)[0] or "unknown").lower()


def _score01(value: Any) -> float:
    result = _finite(value)
    if result is None:
        return 0.0
    if result > 1.0:
        result /= 100.0
    return min(1.0, max(0.0, result))


def _selection_config() -> dict[str, Any]:
    path = Path(os.getenv("E4_SELECTION_V2_PATH", "models/e4/e4-selection-v2.json"))
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        LOGGER.exception("Could not load E4 selection V2 configuration path=%s", path)
        return {}
    return dict(payload) if isinstance(payload, Mapping) else {}


_SELECTION = _selection_config()
_CREATOR_SCORES = {
    str(key): _score01(value)
    for key, value in dict(_SELECTION.get("creator_scores") or {}).items()
}
_SOURCE_SCORES = {
    str(key).lower(): _score01(value)
    for key, value in dict(_SELECTION.get("source_scores") or {}).items()
}
for _creator in (part.strip() for part in os.getenv("E4_TRUSTED_CREATORS", "").split(",")):
    if _creator:
        _CREATOR_SCORES[_creator] = 1.0


def _minimum_tier(tier: str, minimum: str) -> str:
    return _TIER_ORDER[max(_TIER_ORDER.index(tier), _TIER_ORDER.index(minimum))]


def _tier_for_score(score: float, minimum: str = "probe") -> str:
    selected = "probe"
    for tier in _TIER_ORDER:
        if score >= _TIER_MIN_SCORE[tier]:
            selected = tier
    return _minimum_tier(selected, minimum)


def relative_fraction_for_score(score: float, minimum_tier: str = "probe") -> tuple[str, float]:
    tier = _tier_for_score(min(0.999, max(0.0, score)), minimum_tier)
    return tier, _TIER_FRACTIONS[tier]


def _profile_partial(tier: str) -> float:
    # 308/308 modern observed 20%/30% first partials are separated by E4's
    # position-size/confidence family. High and larger relative tiers map to the
    # 20% runner family; ordinary tiers bank 30% first.
    return 0.20 if _TIER_ORDER.index(tier) >= _TIER_ORDER.index("high") else 0.30


def _merge_context(row: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(row)
    for key in ("payload_json", "event_json", "raw_json", "data_json", "payload"):
        for nested_key, nested_value in _mapping(merged.get(key)).items():
            merged.setdefault(str(nested_key), nested_value)
    return merged


def _first(mapping: Mapping[str, Any], *keys: str) -> Any:
    lowered = {str(key).lower(): value for key, value in mapping.items()}
    for key in keys:
        value = lowered.get(key.lower())
        if value not in (None, ""):
            return value
    return None


_previous_from_row = core.Event.from_row.__func__


def _from_row_v6(cls: type[core.Event], row: Mapping[str, Any]) -> core.Event:
    event = _previous_from_row(cls, row)
    merged = _merge_context(row)
    uri = _first(merged, "uri", "metadata_uri", "metadata_url")
    source = str(
        _first(
            merged,
            "launch_source",
            "source_provider",
            "provider",
            "source",
            "origin",
        )
        or "unknown"
    ).lower()
    explicit_creator_score = _score01(
        _first(
            merged,
            "creator_score",
            "developer_score",
            "creator_success_score",
            "creator_reputation_score",
        )
    )
    current = _CONTEXT_BY_MINT.setdefault(event.mint, {})
    current.update(
        {
            "metadata_host": _host(uri) if uri else current.get("metadata_host", "unknown"),
            "launch_source": source if source != "unknown" else current.get("launch_source", "unknown"),
            "prearmed": bool(
                current.get("prearmed")
                or _truthy(
                    _first(
                        merged,
                        "prearmed",
                        "authorized_sniper",
                        "launch_intent",
                        "prelaunch_intent",
                        "sniper_authorized",
                    )
                )
            ),
            "creator_score": max(float(current.get("creator_score") or 0.0), explicit_creator_score),
            "token_program": str(
                _first(merged, "token_program", "base_token_program")
                or current.get("token_program")
                or ""
            ),
            "mayhem": bool(current.get("mayhem") or _truthy(_first(merged, "mayhem", "is_mayhem_mode"))),
        }
    )
    if event.creator:
        current["creator"] = event.creator
    return event


core.Event.from_row = classmethod(_from_row_v6)


_previous_state_apply = core.TokenState.apply


def _state_apply_v6(self: core.TokenState, event: core.Event, wallet: str | None) -> None:
    _previous_state_apply(self, event, wallet)
    context = _CONTEXT_BY_MINT.setdefault(self.mint, {})
    if event.creator:
        context["creator"] = event.creator
    curve = _CURVE_BY_MINT.setdefault(self.mint, {})
    for name, value in (
        ("virtual_sol_reserves", event.virtual_sol),
        ("virtual_token_reserves", event.virtual_tokens),
        ("real_sol_reserves", event.real_sol),
        ("real_token_reserves", event.real_tokens),
    ):
        if value is not None and value > 0:
            curve[name] = value
    if self.creator:
        curve["creator"] = self.creator
    curve["price_sol"] = self.price_sol
    curve["fdv_usd"] = self.fdv_usd
    curve["complete"] = self.complete
    curve["migrated"] = self.migrated


core.TokenState.apply = _state_apply_v6


def _entry_features(state: core.TokenState) -> dict[str, float]:
    created_ns = state.created_ns or state.latest_ns
    events = [
        event
        for event in state.events
        if event.source_ns >= created_ns
        and event.kind
        in {
            core.EventKind.BUY,
            core.EventKind.SELL,
            core.EventKind.PUMPSWAP_BUY,
            core.EventKind.PUMPSWAP_SELL,
        }
    ]
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
    creator = state.creator or str(_CONTEXT_BY_MINT.get(state.mint, {}).get("creator") or "")
    creator_buys = [event for event in buys if creator and event.trader == creator]
    noncreator = [event for event in buys if not creator or event.trader != creator]
    signatures: dict[str, int] = {}
    for event in buys:
        if event.signature:
            signatures[event.signature] = signatures.get(event.signature, 0) + 1
    bundled = sum(count for count in signatures.values() if count > 1)
    first_price = next((event.price_sol for event in state.events if event.price_sol and event.price_sol > 0), None)
    price_multiple = (
        (state.price_sol or 0.0) / first_price
        if first_price and state.price_sol and state.price_sol > 0
        else 0.0
    )
    context = _CONTEXT_BY_MINT.get(state.mint, {})
    creator_score = max(
        _CREATOR_SCORES.get(creator, 0.0),
        _score01(context.get("creator_score")),
    )
    metadata_host = str(context.get("metadata_host") or "unknown").lower()
    launch_source = str(context.get("launch_source") or "unknown").lower()
    source_score = max(
        _SOURCE_SCORES.get(metadata_host, 0.0),
        _SOURCE_SCORES.get(launch_source, 0.0),
    )
    return {
        "age_ms": max(0.0, (state.latest_ns - created_ns) / 1_000_000),
        "fdv_usd": state.fdv_usd or 0.0,
        "buy_sol": sum(max(0.0, event.sol_amount) for event in buys),
        "sell_sol": sum(max(0.0, event.sol_amount) for event in sells),
        "buy_count": float(len(buys)),
        "sell_count": float(len(sells)),
        "unique_buyers": float(len({event.trader for event in buys if event.trader})),
        "noncreator_buyers": float(len({event.trader for event in noncreator if event.trader})),
        "noncreator_buy_sol": sum(max(0.0, event.sol_amount) for event in noncreator),
        "creator_buy_sol": sum(max(0.0, event.sol_amount) for event in creator_buys),
        "unique_signatures": float(len(signatures)),
        "bundled_buys": float(bundled),
        "max_same_signature_buys": float(max(signatures.values(), default=0)),
        "price_multiple": price_multiple,
        "creator_score": creator_score,
        "source_score": source_score,
        "prearmed": 1.0 if context.get("prearmed") else 0.0,
    }


def _fdv_fit(fdv: float) -> float:
    target = float(os.getenv("E4_V6_TARGET_FDV_USD", "4878"))
    if fdv <= 0 or target <= 0:
        return 0.0
    return max(0.0, 1.0 - abs(fdv - target) / max(target, 1.0))


def _entry_v6(
    self: core.E4Policy,
    state: core.TokenState,
) -> tuple[bool, float, float, str, dict[str, float]]:
    if state.complete or state.migrated or state.wallet_touched:
        return False, 0.0, 0.0, "not an untouched live Pump curve", {}
    if state.created_ns is None:
        return False, 0.0, 0.0, "creation event not observed", {}

    features = _entry_features(state)
    max_age_ms = float(os.getenv("E4_V6_MAX_ENTRY_AGE_MS", "350"))
    max_fdv = min(
        self.settings.max_entry_fdv_usd,
        float(os.getenv("E4_V6_MAX_ENTRY_FDV_USD", "8500")),
    )
    if features["age_ms"] > max_age_ms:
        return False, 0.0, 0.0, "outside E4 launch decision horizon", features
    if features["fdv_usd"] <= 0 or features["fdv_usd"] > max_fdv:
        return False, 0.0, 0.0, "outside observed E4 entry FDV", features
    if features["sell_count"] > 0 or features["sell_sol"] > 0:
        return False, 0.0, 0.0, "sell appeared before E4 confirmation", features
    # Every same-window E4 selection in the holdout had a creator seed. Requiring
    # one also prevents generic unbundled-buyer bursts from becoming false
    # positives merely because many bots sprayed the launch.
    if features["creator_buy_sol"] < float(os.getenv("E4_V6_MIN_CREATOR_BUY_SOL", "0.025")):
        return False, 0.0, 0.0, "creator seed not observed", features

    fdv_score = _fdv_fit(features["fdv_usd"])
    creator_seed_score = min(1.0, features["creator_buy_sol"] / 3.0)
    buyer_score = min(1.0, features["noncreator_buyers"] / 6.0)
    capital_score = min(1.0, features["noncreator_buy_sol"] / 12.0)
    acceleration_score = min(1.0, max(0.0, features["price_multiple"] - 1.0) / 0.70)
    bundle_score = min(1.0, features["bundled_buys"] / 6.0)
    identity_score = max(features["creator_score"], features["source_score"])

    candidates: list[tuple[float, str, str]] = []

    # Public-flow family: the E4 entry can be justified entirely from capital
    # already visible on chain. This is deliberately much less brittle than the
    # old requirement for six same-signature buys, but remains highly selective.
    if (
        features["age_ms"] <= 300
        and features["noncreator_buyers"] >= 3
        and features["buy_sol"] >= 8.0
        and features["noncreator_buy_sol"] >= 5.0
        and features["price_multiple"] >= 1.15
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
        candidates.append((score, "public_capital_burst", "standard"))

    # Coordinated/bundled family remains useful evidence, but is no longer a
    # universal gate. Open-source snipers commonly use curve threshold and flow
    # triggers; E4's actual holdout proves multi-buy structure is only one route.
    if (
        features["age_ms"] <= 120
        and features["unique_buyers"] >= 5
        and features["buy_sol"] >= 10.0
        and features["bundled_buys"] >= 3
        and features["price_multiple"] >= 1.25
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
        candidates.append((score, "coordinated_capital_burst", "high"))

    # Creator/launch identity family: public flow does not explain the 29-36ms
    # E4 entries. Only cached evidence is allowed here; there is no HTTP, SQL or
    # social lookup in the hot path.
    if (
        features["age_ms"] <= 100
        and identity_score >= 0.55
        and (
            features["creator_buy_sol"] >= 2.0
            or features["creator_score"] >= 0.72
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
        candidates.append((score, "known_creator_or_launch_source", "high"))

    # Explicit prearmed launch intent is the only path allowed to act on tiny
    # public flow. Metadata-host enrichment alone is not treated as permission.
    if features["age_ms"] <= 80 and features["prearmed"] >= 1.0:
        score = min(
            0.995,
            0.82
            + 0.06 * identity_score
            + 0.05 * fdv_score
            + 0.04 * creator_seed_score
            + 0.03 * acceleration_score,
        )
        candidates.append((score, "authorized_prearmed_launch", "elite"))

    if not candidates:
        return False, 0.0, 0.0, "no observed E4 entry family matched", features

    score, family, minimum_tier = max(candidates, key=lambda item: item[0])
    tier, fraction = relative_fraction_for_score(score, minimum_tier)
    fraction = min(fraction, self.settings.max_position_fraction)
    profile = EntryProfile(
        family=family,
        tier=tier,
        fraction=fraction,
        score=score,
        first_partial_fraction=_profile_partial(tier),
        features=dict(features),
    )
    _PROFILE_BY_MINT[state.mint] = profile
    numeric = dict(features)
    numeric.update(
        {
            "e4_v6_score": score,
            "e4_v6_fraction": fraction,
            "e4_v6_tier_index": float(_TIER_ORDER.index(tier)),
            "e4_v6_first_partial": profile.first_partial_fraction,
        }
    )
    return (
        True,
        score,
        fraction,
        f"E4_V6 family={family} tier={tier}",
        numeric,
    )


core.E4Policy.entry = _entry_v6


# Use the pre-cooldown economic exit policy and apply cooldown only after the
# confidence family has selected its observed 20%/30% first partial.
_base_exit = v4._previous_exit
_v6_partial_requested_ns: dict[tuple[str, int], int] = {}


def _exit_v6(
    self: core.E4Policy,
    position: core.Position,
    state: core.TokenState,
) -> tuple[str, float, str]:
    action, fraction, reason = _base_exit(self, position, state)
    profile = _PROFILE_BY_MINT.get(position.mint)

    # A confirmed E4 runner is flow/drawdown managed. The base 60-second timer
    # is an obsolete safety ceiling and would have cut observed 63s, 142s and
    # 185s E4 winners. V6's guardian supplies a much longer emergency fuse.
    if position.first_partial_done and "observed hold horizon" in reason.lower():
        return "HOLD", 0.0, "E4 V6 confirmed runner beyond legacy horizon"

    if action == "SELL_PARTIAL" and not position.first_partial_done and profile is not None:
        markout = position.markout_bps(state.price_sol or position.last_price)
        flow250 = state.flow(250)
        target = profile.first_partial_fraction
        if target <= 0.20 + 1e-9:
            if markout < self.settings.acceleration_partial_markout_bps or flow250.net <= 0:
                return "HOLD", 0.0, "E4 high-conviction runner awaiting 20% partial trigger"
            fraction = 0.20
            reason = "E4 confidence-tier 20% first partial"
        else:
            if markout < self.settings.normal_partial_markout_bps:
                return "HOLD", 0.0, "E4 standard runner awaiting 30% partial trigger"
            fraction = 0.30
            reason = "E4 confidence-tier 30% first partial"

    if action != "SELL_PARTIAL":
        return action, fraction, reason

    key = (position.position_id, id(position))
    now_ns = time.time_ns()
    cooldown_ms = max(25, int(os.getenv("E4_PARTIAL_COOLDOWN_MS", "200")))
    previous_ns = _v6_partial_requested_ns.get(key, 0)
    if now_ns - previous_ns < cooldown_ms * 1_000_000:
        return "HOLD", 0.0, "E4 V6 partial request cooldown"
    _v6_partial_requested_ns[key] = now_ns
    return action, fraction, reason


core.E4Policy.exit = _exit_v6


def _profile_payload(profile: EntryProfile) -> str:
    return json.dumps(
        {
            "family": profile.family,
            "tier": profile.tier,
            "fraction": profile.fraction,
            "score": profile.score,
            "first_partial_fraction": profile.first_partial_fraction,
            "features": dict(profile.features),
        },
        separators=(",", ":"),
        default=str,
    )


def _ensure_profile_table(engine: core.Engine) -> None:
    connection = getattr(getattr(engine, "store", None), "conn", None)
    if connection is None:
        return
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS e4_entry_profiles(
          mint TEXT PRIMARY KEY,
          family TEXT NOT NULL,
          tier TEXT NOT NULL,
          fraction REAL NOT NULL,
          score REAL NOT NULL,
          first_partial_fraction REAL NOT NULL,
          payload_json TEXT NOT NULL,
          updated_ns INTEGER NOT NULL
        )
        """
    )
    for row in connection.execute(
        "SELECT mint,family,tier,fraction,score,first_partial_fraction,payload_json FROM e4_entry_profiles"
    ):
        try:
            payload = json.loads(row["payload_json"] or "{}")
            _PROFILE_BY_MINT[str(row["mint"])] = EntryProfile(
                family=str(row["family"]),
                tier=str(row["tier"]),
                fraction=float(row["fraction"]),
                score=float(row["score"]),
                first_partial_fraction=float(row["first_partial_fraction"]),
                features=dict(payload.get("features") or {}),
            )
        except Exception:
            LOGGER.exception("Could not restore E4 entry profile mint=%s", row["mint"])


def _persist_profile(engine: core.Engine, mint: str) -> None:
    profile = _PROFILE_BY_MINT.get(mint)
    connection = getattr(getattr(engine, "store", None), "conn", None)
    if profile is None or connection is None:
        return
    connection.execute(
        """
        INSERT INTO e4_entry_profiles(
          mint,family,tier,fraction,score,first_partial_fraction,payload_json,updated_ns
        ) VALUES(?,?,?,?,?,?,?,?)
        ON CONFLICT(mint) DO UPDATE SET
          family=excluded.family,tier=excluded.tier,fraction=excluded.fraction,
          score=excluded.score,first_partial_fraction=excluded.first_partial_fraction,
          payload_json=excluded.payload_json,updated_ns=excluded.updated_ns
        """,
        (
            mint,
            profile.family,
            profile.tier,
            profile.fraction,
            profile.score,
            profile.first_partial_fraction,
            _profile_payload(profile),
            time.time_ns(),
        ),
    )


_previous_engine_init = core.Engine.__init__


def _engine_init_v6(self: core.Engine, settings: core.Settings) -> None:
    _previous_engine_init(self, settings)
    _ensure_profile_table(self)


core.Engine.__init__ = _engine_init_v6


_previous_execute_buy = core.Engine.execute_buy


async def _execute_buy_v6(
    self: core.Engine,
    state: core.TokenState,
    score: float,
    fraction: float,
    reason: str,
) -> None:
    _persist_profile(self, state.mint)
    await _previous_execute_buy(self, state, score, fraction, reason)


core.Engine.execute_buy = _execute_buy_v6


# Attach cached launch/curve facts to every builder request. The local builder
# can construct Pump instructions without a remote trade-local round trip when
# these values are complete, while retaining a safe fallback when they are not.
_previous_execute = core.Engine.execute


async def _execute_v6(
    self: core.Engine,
    request_id: str,
    request: Mapping[str, Any],
) -> tuple[str, bool, int | None, str | None]:
    enriched = dict(request)
    mint = str(enriched.get("mint") or "")
    if mint:
        metadata = dict(enriched.get("metadata") or {})
        context = _CONTEXT_BY_MINT.get(mint, {})
        curve = _CURVE_BY_MINT.get(mint, {})
        profile = _PROFILE_BY_MINT.get(mint)
        metadata.update(
            {
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
            }
        )
        enriched["metadata"] = metadata
    return await _previous_execute(self, request_id, enriched)


core.Engine.execute = _execute_v6


async def _guardian_v6(self: core.Engine) -> None:
    interval = max(0.005, float(os.getenv("E4_GUARDIAN_INTERVAL_SECONDS", "0.01")))
    runner_emergency_ms = max(
        240_000,
        int(os.getenv("E4_RUNNER_EMERGENCY_HORIZON_MS", "300000")),
    )
    runner_quiet_ms = max(1_000, int(os.getenv("E4_RUNNER_QUIET_MS", "5000")))

    while not self.stop_event.is_set():
        now_ns = time.time_ns()
        for mint, position in tuple(self.positions.items()):
            if mint in self.pending_exits:
                continue
            age_ms = max(0.0, (now_ns - position.opened_ns) / 1_000_000)
            if not position.first_partial_done:
                if age_ms >= self.settings.failure_window_ms:
                    await e4_hardening_v5._schedule_exit(
                        self,
                        position,
                        1.0,
                        "E4 V6 independent confirmation-window liquidation",
                        payload={"guardian": True, "age_ms": age_ms, "guardian_version": "v6"},
                    )
                continue

            state = self.tokens.get(mint)
            if age_ms >= runner_emergency_ms:
                await e4_hardening_v5._schedule_exit(
                    self,
                    position,
                    1.0,
                    "E4 V6 emergency runner horizon",
                    payload={"guardian": True, "age_ms": age_ms, "guardian_version": "v6"},
                )
                continue
            if state is None:
                continue

            price = state.price_sol or position.last_price
            if price and price > 0:
                position.last_price = price
                position.max_price = max(position.max_price, price)
                flow250 = v4._wall_flow(state, 250, now_ns)
                flow1s = v4._wall_flow(state, 1_000, now_ns)
                broken = flow250.net < 0 or flow1s.ratio < 0.85
                if broken and position.drawdown_bps(price) >= 350:
                    await e4_hardening_v5._schedule_exit(
                        self,
                        position,
                        1.0,
                        "E4 V6 independent runner flow-break liquidation",
                        payload={"guardian": True, "age_ms": age_ms, "guardian_version": "v6"},
                    )
                    continue
                if position.drawdown_bps(price) >= self.settings.runner_drawdown_bps:
                    await e4_hardening_v5._schedule_exit(
                        self,
                        position,
                        1.0,
                        "E4 V6 independent runner peak-drawdown liquidation",
                        payload={"guardian": True, "age_ms": age_ms, "guardian_version": "v6"},
                    )
                    continue

            last_trade_ns = v4._latest_trade_ns(state)
            quiet_for_ms = (
                float("inf")
                if last_trade_ns is None
                else max(0.0, (now_ns - last_trade_ns) / 1_000_000)
            )
            if quiet_for_ms >= runner_quiet_ms:
                await e4_hardening_v5._schedule_exit(
                    self,
                    position,
                    1.0,
                    "E4 V6 confirmed runner lost live flow",
                    payload={
                        "guardian": True,
                        "age_ms": age_ms,
                        "quiet_for_ms": quiet_for_ms,
                        "guardian_version": "v6",
                    },
                )
                continue

            await e4_hardening_v5._evaluate_current_position(
                self,
                position,
                "E4 V6 guardian catch-up",
            )
        try:
            await asyncio.wait_for(self.stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


final._guardian = _guardian_v6
