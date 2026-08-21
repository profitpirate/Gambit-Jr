from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Iterable


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def reconcile_field(values: Iterable[dict[str, Any]], tolerance: float = .10) -> dict[str, Any]:
    known = [v for v in values if v.get("value") is not None]
    if not known:
        return {"value": None, "status": "UNKNOWN", "sources": list(values)}
    numeric = [_number(v["value"]) for v in known]
    numeric = [v for v in numeric if v is not None]
    conflict = len(numeric) > 1 and min(numeric) > 0 and (max(numeric) - min(numeric)) / min(numeric) > tolerance
    return {"value": known[0]["value"], "status": "DATA_CONFLICT" if conflict else "HIGH_CONFIDENCE",
            "sources": known}


def entry_quality(radar_mc: float | None, current_mc: float | None) -> str:
    if not radar_mc or not current_mc or radar_mc <= 0:
        return "UNKNOWN"
    multiple = current_mc / radar_mc
    return "EARLY" if multiple < 1.35 else "ACCEPTABLE" if multiple < 1.8 else "EXTENDED" if multiple < 3 else "CHASING"


def constant_product_impact(liquidity_usd: float | None, trade_usd: float) -> dict[str, Any]:
    if liquidity_usd is None or liquidity_usd <= 0:
        return {"trade_usd": trade_usd, "buy_impact_percent": None, "sell_impact_percent": None, "quality": "UNKNOWN"}
    reserve = liquidity_usd / 2
    impact = trade_usd / (reserve + trade_usd) * 100
    quality = "GOOD" if impact < 1 else "ACCEPTABLE" if impact < 3 else "POOR"
    return {"trade_usd": trade_usd, "buy_impact_percent": round(impact, 3),
            "sell_impact_percent": round(impact, 3), "quality": quality, "estimate_only": True}


def wallet_intelligence(holders: list[dict[str, Any]] | None,
                        traders: list[dict[str, Any]] | None,
                        info: dict[str, Any] | None = None) -> dict[str, Any]:
    tag_stats = (info or {}).get("wallet_tags_stat") or {}
    if holders is None and traders is None and not tag_stats:
        return {"smart_money": "SMART_MONEY_UNKNOWN", "buyer_diversity": "UNKNOWN",
                "activity_quality": "UNKNOWN", "raw": None}
    rows = (holders or []) + (traders or [])
    labels: dict[str, set[str]] = {k: set() for k in ("smart", "sniper", "insider", "bundler", "dev", "whale")}
    funders: dict[str, int] = {}
    sizes: dict[float, int] = {}
    for row in rows:
        wallet = str(row.get("wallet_address") or row.get("address") or "")
        raw_labels = row.get("labels") or row.get("tags") or row.get("tag") or []
        if isinstance(raw_labels, str): raw_labels = [raw_labels]
        lowered = " ".join(str(x).lower() for x in raw_labels)
        for label in labels:
            if label in lowered or (label == "dev" and "creator" in lowered): labels[label].add(wallet)
        funder = row.get("funder") or row.get("funding_source")
        if funder: funders[str(funder)] = funders.get(str(funder), 0) + 1
        size = _number(row.get("buy_amount") or row.get("amount"))
        if size is not None: sizes[round(size, 6)] = sizes.get(round(size, 6), 0) + 1
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
    diversity = "BUYER_DIVERSITY_LOW" if cluster else "BUYER_DIVERSITY_HIGH" if len(rows) >= 10 else "BUYER_DIVERSITY_MEDIUM"
    activity = "SUSPICIOUS" if cluster else "ORGANIC_LIKELY" if len(rows) >= 10 else "MIXED"
    return {"smart_money": "SMART_MONEY_CONVERGENCE" if smart >= 3 else "SMART_MONEY_BUYING" if smart else "NO_SMART_MONEY_DATA",
            "counts": counts, "buyer_diversity": diversity,
            "possible_wallet_cluster": cluster, "activity_quality": activity, "raw": rows}


def social_presence(info: dict[str, Any] | None, discovered_at: str) -> dict[str, Any]:
    info = info or {}
    result: dict[str, Any] = {"observed_at": discovered_at}
    for key in ("twitter", "telegram", "website", "discord", "logo", "banner"):
        value = info.get(key) or info.get(f"{key}_url")
        result[key] = {"value": value, "status": "PRESENT" if value else "UNKNOWN"}
    result["evidence"] = "SOCIALS_PRESENT_AT_DISCOVERY" if any(v["status"] == "PRESENT" for k,v in result.items() if isinstance(v, dict)) else "SOCIAL_AGE_UNKNOWN"
    return result


def priority(radar_score: float, confidence: float, terminal_risk: bool,
             smart_wallets: int = 0, organic: bool = False, social_only: bool = False) -> str:
    if terminal_risk: return "STANDARD"
    independent = int(smart_wallets >= 3) + int(organic)
    if radar_score >= 85 and confidence >= .60 and independent >= 2 and not social_only: return "PRIORITY"
    if radar_score >= 75 and confidence >= .45 and independent >= 1 and not social_only: return "HOT"
    return "STANDARD"


def probable_rug(liquidity_baseline: float | None, liquidity_now: float | None,
                 ath: float | None, current: float | None, terminal_evidence: bool = False) -> dict[str, Any]:
    liq_drop = None if not liquidity_baseline or liquidity_now is None else 1 - liquidity_now / liquidity_baseline
    drawdown = None if not ath or current is None else 1 - current / ath
    rug = bool(terminal_evidence or ((liq_drop or 0) >= .9 and (drawdown or 0) >= .8))
    warning = bool((liq_drop or 0) >= .5)
    return {"probable_rug": rug, "liquidity_warning": warning, "liquidity_drop": liq_drop,
            "drawdown_from_ath": drawdown, "ordinary_pullback": not rug and not warning}


def stale_alert(evaluated_at: str, delivered_at: str, max_age_seconds: float) -> dict[str, Any]:
    latency = (datetime.fromisoformat(delivered_at.replace("Z", "+00:00")) -
               datetime.fromisoformat(evaluated_at.replace("Z", "+00:00"))).total_seconds()
    return {"latency_seconds": latency, "status": "STALE_SNAPSHOT" if latency > max_age_seconds else "FRESH"}
