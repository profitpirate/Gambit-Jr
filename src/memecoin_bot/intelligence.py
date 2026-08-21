from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def reconcile_field(values: Iterable[dict[str, Any]], tolerance: float = 0.10) -> dict[str, Any]:
    known = [v for v in values if v.get("value") is not None]
    if not known:
        return {"value": None, "status": "UNKNOWN", "sources": list(values)}
    numeric = [_number(v["value"]) for v in known]
    numeric = [v for v in numeric if v is not None]
    conflict = (
        len(numeric) > 1
        and min(numeric) > 0
        and (max(numeric) - min(numeric)) / min(numeric) > tolerance
    )
    return {
        "value": known[0]["value"],
        "status": "DATA_CONFLICT" if conflict else "HIGH_CONFIDENCE",
        "sources": known,
    }


def entry_quality(radar_mc: float | None, current_mc: float | None) -> str:
    if not radar_mc or not current_mc or radar_mc <= 0:
        return "UNKNOWN"
    multiple = current_mc / radar_mc
    return (
        "VERY_EARLY"
        if multiple < 1.10
        else "EARLY"
        if multiple < 1.35
        else "ACCEPTABLE"
        if multiple < 1.8
        else "EXTENDED"
        if multiple < 2.5
        else "CHASING"
        if multiple < 5
        else "LATE"
    )


def intelligence_pillar(
    score: float | None,
    confidence: float,
    evidence: list[str] | None = None,
    unknowns: list[str] | None = None,
    risks: list[str] | None = None,
    freshness: str = "CURRENT",
) -> dict[str, Any]:
    """A common truthful shape for every V1.3.1 intelligence pillar."""
    return {
        "score": score,
        "confidence": max(0.0, min(1.0, confidence)),
        "evidence": evidence or [],
        "unknowns": unknowns or [],
        "risks": risks or [],
        "freshness": freshness,
    }


def signal_convergence(pillars: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Grade independent-pillar agreement; one hot input can never produce a top tier."""
    known: list[tuple[str, float, float]] = []
    for name, pillar in pillars.items():
        score = _number(pillar.get("score"))
        confidence = _number(pillar.get("confidence")) or 0
        if score is not None:
            known.append((name, score / 100 if score > 1 else score, confidence))
    weighted = (
        (
            sum(score * max(conf, 0.1) for _, score, conf in known)
            / sum(max(conf, 0.1) for _, _, conf in known)
        )
        if known
        else 0
    )
    strong_pillars = sum(score >= 0.65 and conf >= 0.45 for _, score, conf in known)
    if weighted >= 0.85 and strong_pillars >= 5:
        grade = "EXCEPTIONAL"
    elif weighted >= 0.72 and strong_pillars >= 4:
        grade = "STRONG"
    elif weighted >= 0.58 and strong_pillars >= 3:
        grade = "GOOD"
    elif weighted >= 0.40 and len(known) >= 2:
        grade = "DEVELOPING"
    else:
        grade = "WEAK"
    return {
        "class": grade,
        "score": round(weighted * 100, 2),
        "known_pillars": len(known),
        "strong_pillars": strong_pillars,
        "pillar_scores": {n: round(s * 100, 2) for n, s, _ in known},
        "rule": "higher tiers require independent pillar diversity",
    }


def setup_quality(pillars: dict[str, dict[str, Any]], entry_state: str) -> dict[str, Any]:
    scores = [
        float(p["score"]) * (100 if float(p["score"]) <= 1 else 1)
        for p in pillars.values()
        if p.get("score") is not None
    ]
    base = sum(scores) / len(scores) if scores else 0
    penalty = {
        "ACCEPTABLE": 0,
        "EARLY": 0,
        "VERY_EARLY": 0,
        "EXTENDED": 8,
        "CHASING": 20,
        "LATE": 30,
    }.get(entry_state, 5)
    final = max(0, base - penalty)
    grade = (
        "A+"
        if final >= 88 and len(scores) >= 5
        else "A"
        if final >= 78
        else "B+"
        if final >= 68
        else "B"
        if final >= 55
        else "C"
    )
    return {
        "grade": grade,
        "score": round(final, 2),
        "base_score": round(base, 2),
        "entry_penalty": penalty,
        "entry_state": entry_state,
        "components": {name: pillar.get("score") for name, pillar in pillars.items()},
        "explanation": f"mean of {len(scores)} known pillars minus {penalty} entry-timing points",
    }


def narrative_context(
    identity: str | None,
    discovered_at: str,
    peer_count: int = 0,
    copycat_count: int = 0,
    velocity: float | None = None,
) -> dict[str, Any]:
    discovered = datetime.fromisoformat(discovered_at)
    if discovered.tzinfo is None:
        discovered = discovered.replace(tzinfo=UTC)
    age_minutes = max(0, (datetime.now(UTC) - discovered).total_seconds() / 60)
    freshness = (
        "FRESH"
        if age_minutes <= 30
        else "EMERGING"
        if age_minutes <= 180
        else "MATURE"
        if age_minutes <= 720
        else "DECAYING"
    )
    saturation = (
        "SATURATED"
        if peer_count >= 20 or copycat_count >= 10
        else "CROWDED"
        if peer_count >= 8
        else "OPEN"
    )
    first_mover = peer_count <= 2 and copycat_count == 0
    quality = (
        "STRONG"
        if identity and freshness in {"FRESH", "EMERGING"} and saturation == "OPEN"
        else "DEVELOPING"
        if identity
        else "UNKNOWN"
    )
    return {
        "identity": identity or "UNKNOWN",
        "cluster": identity or "UNCLASSIFIED",
        "freshness": freshness,
        "velocity": velocity,
        "saturation": saturation,
        "decay": freshness == "DECAYING",
        "first_mover": first_mover,
        "copycat": copycat_count > 0,
        "quality": quality,
        "peer_count": peer_count,
        "provenance": "internal token/discovery history",
    }


def catalyst_timing(discovered_at: str, published_at: str | None) -> str:
    if not published_at:
        return "CATALYST_UNKNOWN"
    discovered = datetime.fromisoformat(discovered_at)
    published = datetime.fromisoformat(published_at)
    delta = (discovered - published).total_seconds() / 60
    return (
        "BEFORE_CATALYST"
        if delta < -5
        else "AT_CATALYST"
        if delta <= 5
        else "EARLY_AFTER_CATALYST"
        if delta <= 60
        else "LATE_AFTER_CATALYST"
    )


def constant_product_impact(liquidity_usd: float | None, trade_usd: float) -> dict[str, Any]:
    if liquidity_usd is None or liquidity_usd <= 0:
        return {
            "trade_usd": trade_usd,
            "buy_impact_percent": None,
            "sell_impact_percent": None,
            "quality": "UNKNOWN",
        }
    reserve = liquidity_usd / 2
    impact = trade_usd / (reserve + trade_usd) * 100
    quality = "GOOD" if impact < 1 else "ACCEPTABLE" if impact < 3 else "POOR"
    return {
        "trade_usd": trade_usd,
        "buy_impact_percent": round(impact, 3),
        "sell_impact_percent": round(impact, 3),
        "quality": quality,
        "estimate_only": True,
    }


def wallet_intelligence(
    holders: list[dict[str, Any]] | None,
    traders: list[dict[str, Any]] | None,
    info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    tag_stats = (info or {}).get("wallet_tags_stat") or {}
    if holders is None and traders is None and not tag_stats:
        return {
            "smart_money": "SMART_MONEY_UNKNOWN",
            "buyer_diversity": "UNKNOWN",
            "activity_quality": "UNKNOWN",
            "raw": None,
        }
    rows = (holders or []) + (traders or [])
    labels: dict[str, set[str]] = {
        k: set() for k in ("smart", "sniper", "insider", "bundler", "dev", "whale")
    }
    funders: dict[str, int] = {}
    sizes: dict[float, int] = {}
    for row in rows:
        wallet = str(row.get("wallet_address") or row.get("address") or "")
        raw_labels = row.get("labels") or row.get("tags") or row.get("tag") or []
        if isinstance(raw_labels, str):
            raw_labels = [raw_labels]
        lowered = " ".join(str(x).lower() for x in raw_labels)
        for label, addresses in labels.items():
            if label in lowered or (label == "dev" and "creator" in lowered):
                addresses.add(wallet)
        funder = row.get("funder") or row.get("funding_source")
        if funder:
            funders[str(funder)] = funders.get(str(funder), 0) + 1
        size = _number(row.get("buy_amount") or row.get("amount"))
        if size is not None:
            sizes[round(size, 6)] = sizes.get(round(size, 6), 0) + 1
    smart = len(labels["smart"])
    reported = {
        "smart": int(tag_stats.get("smart_wallets") or 0),
        "sniper": int(tag_stats.get("sniper_wallets") or 0),
        "bundler": int(tag_stats.get("bundler_wallets") or 0),
        "whale": int(tag_stats.get("whale_wallets") or 0),
        "dev": int(tag_stats.get("creator_wallets") or 0),
    }
    counts = {k: max(len(v), reported.get(k, 0)) for k, v in labels.items()}
    smart = counts["smart"]
    cluster = max(funders.values(), default=0) >= 3 or max(sizes.values(), default=0) >= 4
    diversity = (
        "BUYER_DIVERSITY_LOW"
        if cluster
        else "BUYER_DIVERSITY_HIGH"
        if len(rows) >= 10
        else "BUYER_DIVERSITY_MEDIUM"
    )
    activity = "SUSPICIOUS" if cluster else "ORGANIC_LIKELY" if len(rows) >= 10 else "MIXED"
    return {
        "smart_money": "SMART_MONEY_CONVERGENCE"
        if smart >= 3
        else "SMART_MONEY_BUYING"
        if smart
        else "NO_SMART_MONEY_DATA",
        "counts": counts,
        "buyer_diversity": diversity,
        "possible_wallet_cluster": cluster,
        "activity_quality": activity,
        "raw": rows,
    }


def social_presence(info: dict[str, Any] | None, discovered_at: str) -> dict[str, Any]:
    info = info or {}
    result: dict[str, Any] = {"observed_at": discovered_at}
    for key in ("twitter", "telegram", "website", "discord", "logo", "banner"):
        value = info.get(key) or info.get(f"{key}_url")
        result[key] = {"value": value, "status": "PRESENT" if value else "UNKNOWN"}
    result["evidence"] = (
        "SOCIALS_PRESENT_AT_DISCOVERY"
        if any(v["status"] == "PRESENT" for k, v in result.items() if isinstance(v, dict))
        else "SOCIAL_AGE_UNKNOWN"
    )
    return result


def priority(
    radar_score: float,
    confidence: float,
    terminal_risk: bool,
    smart_wallets: int = 0,
    organic: bool = False,
    social_only: bool = False,
    entry_state: str | None = None,
) -> str:
    if terminal_risk:
        return "STANDARD"
    if entry_state in {"CHASING", "LATE"}:
        return "STANDARD"
    independent = int(smart_wallets >= 3) + int(organic)
    if radar_score >= 85 and confidence >= 0.60 and independent >= 2 and not social_only:
        return "PRIORITY"
    if radar_score >= 75 and confidence >= 0.45 and independent >= 1 and not social_only:
        return "HOT"
    return "STANDARD"


def probable_rug(
    liquidity_baseline: float | None,
    liquidity_now: float | None,
    ath: float | None,
    current: float | None,
    terminal_evidence: bool = False,
) -> dict[str, Any]:
    liq_drop = (
        None
        if not liquidity_baseline or liquidity_now is None
        else 1 - liquidity_now / liquidity_baseline
    )
    drawdown = None if not ath or current is None else 1 - current / ath
    rug = bool(terminal_evidence or ((liq_drop or 0) >= 0.9 and (drawdown or 0) >= 0.8))
    warning = bool((liq_drop or 0) >= 0.5)
    return {
        "probable_rug": rug,
        "liquidity_warning": warning,
        "liquidity_drop": liq_drop,
        "drawdown_from_ath": drawdown,
        "ordinary_pullback": not rug and not warning,
    }


def stale_alert(evaluated_at: str, delivered_at: str, max_age_seconds: float) -> dict[str, Any]:
    latency = (
        datetime.fromisoformat(delivered_at) - datetime.fromisoformat(evaluated_at)
    ).total_seconds()
    return {
        "latency_seconds": latency,
        "status": "STALE_SNAPSHOT" if latency > max_age_seconds else "FRESH",
    }
