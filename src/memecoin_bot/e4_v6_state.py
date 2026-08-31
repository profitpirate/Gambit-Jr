from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from . import e4_hardening
from . import e4_hardening_v5

core = e4_hardening_v5.core
final = e4_hardening_v5.final

MAX_POSITION_FRACTION = 0.115
RUNNER_EMERGENCY_HORIZON_MS = 300_000
TOKEN_2022_PROGRAM_ID = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
SIZE_LADDER = (
    (0.960, "ELITE", 0.1000),
    (0.910, "VERY_HIGH", 0.0480),
    (0.860, "HIGH", 0.0300),
    (0.800, "STRONG", 0.0185),
    (0.740, "NORMAL", 0.0125),
    (0.000, "BASE", 0.0075),
)
HIGH_CONVICTION = {"HIGH", "VERY_HIGH", "ELITE"}
POLICY_BY_MINT: dict[str, dict[str, Any]] = {}
CREATOR_PROFILES: dict[str, dict[str, Any]] = {}
FUNDER_BY_CREATOR: dict[str, str] = {}


def size_tier(score: float) -> tuple[str, float]:
    for minimum, name, fraction in SIZE_LADDER:
        if score >= minimum:
            return name, fraction
    return "BASE", 0.0075


def _pick(payload: dict[str, Any], *keys: str) -> Any:
    lower = {str(k).lower(): v for k, v in payload.items()}
    for key in keys:
        value = lower.get(key.lower())
        if value not in (None, ""):
            return value
    return None


def load_identity_cache(path: Path) -> None:
    CREATOR_PROFILES.clear()
    FUNDER_BY_CREATOR.clear()
    if not path.exists():
        return
    try:
        conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True, timeout=1.0)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error:
        return
    try:
        tables = {str(r[0]) for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )}
        if "creator_profiles_v14" in tables:
            for row in conn.execute(
                "SELECT creator_address,quality,launches,survived,rugs,runners FROM creator_profiles_v14"
            ):
                creator = str(row["creator_address"] or "")
                if not creator:
                    continue
                launches = int(row["launches"] or 0)
                runners = int(row["runners"] or 0)
                rugs = int(row["rugs"] or 0)
                CREATOR_PROFILES[creator] = {
                    "quality": str(row["quality"] or "UNKNOWN").upper(),
                    "launches": launches,
                    "survived": int(row["survived"] or 0),
                    "runners": runners,
                    "rugs": rugs,
                    "runner_rate": runners / launches if launches else 0.0,
                    "rug_rate": rugs / launches if launches else 0.0,
                }
        if "canonical_events" in tables:
            try:
                rows = conn.execute(
                    "SELECT canonical_token,payload_json FROM canonical_events "
                    "WHERE event_type='FUNDER_RELATIONSHIP' ORDER BY rowid DESC LIMIT 50000"
                )
                for row in rows:
                    try:
                        payload = json.loads(row["payload_json"] or "{}")
                    except (TypeError, json.JSONDecodeError):
                        continue
                    if not isinstance(payload, dict):
                        continue
                    creator = str(_pick(payload, "creator", "wallet", "child", "destination") or row["canonical_token"] or "")
                    funder = str(_pick(payload, "funder", "source", "source_wallet", "parent", "funding_wallet") or "")
                    if creator and funder and creator != funder:
                        FUNDER_BY_CREATOR.setdefault(creator, funder)
            except sqlite3.Error:
                pass
    finally:
        conn.close()


def creator_signal(creator: str | None) -> tuple[float, dict[str, Any]]:
    profile = CREATOR_PROFILES.get(str(creator or ""))
    if not profile:
        return 0.0, {"creator_known": False, "creator_quality": "UNKNOWN"}
    quality = str(profile["quality"])
    base = {"PROVEN": 1.0, "POSITIVE": 0.80, "NEUTRAL": 0.20,
            "SUSPICIOUS": -0.75, "TOXIC": -1.0}.get(quality, 0.0)
    score = max(-1.0, min(1.0,
        base + min(0.25, float(profile["runner_rate"]) * 0.5)
        - min(0.50, float(profile["rug_rate"]) * 0.75)))
    return score, {"creator_known": True, "creator_quality": quality, **profile}


def funder_signal(creator: str | None) -> tuple[float, dict[str, Any]]:
    funder = FUNDER_BY_CREATOR.get(str(creator or ""))
    if not funder:
        return 0.0, {"funder_known": False}
    profile = CREATOR_PROFILES.get(funder)
    if not profile:
        return 0.05, {"funder_known": True, "funder": funder, "funder_quality": "UNKNOWN"}
    quality = str(profile["quality"])
    score = {"PROVEN": 0.40, "POSITIVE": 0.25, "NEUTRAL": 0.0,
             "SUSPICIOUS": -0.55, "TOXIC": -0.85}.get(quality, 0.0)
    return score, {"funder_known": True, "funder": funder, "funder_quality": quality}


def microstructure(state: Any) -> dict[str, float]:
    if state.created_ns is None:
        return {}
    buys, sells = [], []
    first_price = None
    for event in state.events:
        if event.source_ns < state.created_ns:
            continue
        if first_price is None and event.price_sol:
            first_price = event.price_sol
        if event.kind in {core.EventKind.BUY, core.EventKind.PUMPSWAP_BUY}:
            buys.append(event)
        elif event.kind in {core.EventKind.SELL, core.EventKind.PUMPSWAP_SELL}:
            sells.append(event)
    creator = state.creator
    public = [e for e in buys if not creator or e.trader != creator]
    creator_buys = [e for e in buys if creator and e.trader == creator]
    total = sum(max(0.0, e.sol_amount) for e in buys)
    price_multiple = ((state.price_sol or 0.0) / first_price) if first_price and state.price_sol else 1.0
    return {
        "token_age_ms": max(0.0, (state.latest_ns - state.created_ns) / 1_000_000),
        "buy_count": float(len(buys)),
        "sell_count": float(len(sells)),
        "creator_buy_sol": sum(max(0.0, e.sol_amount) for e in creator_buys),
        "public_buy_sol": sum(max(0.0, e.sol_amount) for e in public),
        "total_buy_sol": total,
        "public_buyers": float(len({e.trader for e in public if e.trader})),
        "price_multiple": price_multiple,
        "capital_per_trade_sol": total / max(1, len(buys) + len(sells)),
    }


def curve_meta(state: Any) -> dict[str, Any] | None:
    virtual_sol = virtual_tokens = real_sol = real_tokens = None
    for event in reversed(state.events):
        virtual_sol = event.virtual_sol if virtual_sol is None and event.virtual_sol is not None else virtual_sol
        virtual_tokens = event.virtual_tokens if virtual_tokens is None and event.virtual_tokens is not None else virtual_tokens
        real_sol = event.real_sol if real_sol is None and event.real_sol is not None else real_sol
        real_tokens = event.real_tokens if real_tokens is None and event.real_tokens is not None else real_tokens
        if None not in (virtual_sol, virtual_tokens, real_sol, real_tokens):
            break
    if None in (virtual_sol, virtual_tokens, real_sol, real_tokens) or not state.creator:
        return None
    return {
        "virtual_sol_reserves": str(int(virtual_sol)),
        "virtual_token_reserves": str(int(virtual_tokens)),
        "real_sol_reserves": str(int(real_sol)),
        "real_token_reserves": str(int(real_tokens)),
        "token_total_supply": "1000000000000000",
        "creator": str(state.creator),
        "is_mayhem_mode": False,
        "is_cashback_coin": False,
        "complete": bool(state.complete or state.migrated),
    }
