from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

from memecoin_bot.models import (
    DiscoveryEvent,
    MarketSnapshot,
    RadarResult,
    SafetyAssessment,
    ScoreResult,
)


def _safe(value: Any, max_length: int = 200) -> str:
    text = re.sub(r"[`\r\n]+", " ", str(value or "UNKNOWN"))[:max_length]
    return text.replace("@everyone", "everyone").replace("@here", "here")


def _money(value: float | None) -> str:
    if value is None:
        return "UNKNOWN"
    if value >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"${value / 1_000:.1f}K"
    return f"${value:,.2f}"


def _number(value: float | None, suffix: str = "") -> str:
    return (
        "UNKNOWN"
        if value is None
        else f"{value:.2f}{suffix}"
        if isinstance(value, float)
        else f"{value}{suffix}"
    )


def signal_payload(
    discovery: DiscoveryEvent,
    market: MarketSnapshot,
    safety: SafetyAssessment,
    intelligence: dict[str, Any],
    score: ScoreResult,
    shadow: bool,
) -> dict[str, Any]:
    return {
        "classification": str(score.classification),
        "chain": discovery.chain,
        "score": score.total,
        "normalized_score": score.normalized_score,
        "available_weight": score.available_weight,
        "confidence": score.confidence,
        "symbol": market.symbol or discovery.symbol,
        "name": market.name or discovery.name,
        "token_address": discovery.token_address,
        "signal_market_cap_usd": market.market_cap_usd,
        "liquidity_usd": market.liquidity_usd,
        "volume_5m_usd": market.volume_5m_usd,
        "holders": safety.holder_count,
        "top10_percent": safety.top10_percent,
        "bundled_percent": safety.bundled_percent,
        "component_scores": score.component_scores,
        "component_maxima": score.component_maxima,
        "developer": intelligence["developer"],
        "narrative": intelligence["narrative"],
        "social": intelligence["social"],
        "onchain": intelligence["onchain"],
        "momentum": intelligence["momentum"],
        "risks": intelligence.get("risks", []),
        "thesis": intelligence.get("thesis", []),
        "pillars": intelligence.get("pillars", {}),
        "convergence": intelligence.get("convergence", {}),
        "setup_quality": intelligence.get("setup_quality", {}),
        "entry_quality": intelligence.get("entry_quality", "UNKNOWN"),
        "narrative_context": intelligence.get("narrative_context", {}),
        "signal_timestamp": market.captured_at,
        "pair_address": market.pair_address,
        "shadow": shadow,
        "scoring_version": score.scoring_version,
    }


def radar_payload(
    discovery: DiscoveryEvent, market: MarketSnapshot, radar: RadarResult, snapshot_count: int
) -> dict[str, Any]:
    return {
        "classification": "EARLY_RADAR",
        "chain": discovery.chain,
        "symbol": market.symbol or discovery.symbol,
        "name": market.name or discovery.name,
        "token_address": discovery.token_address,
        "pair_address": market.pair_address,
        "market_cap_usd": market.market_cap_usd,
        "price_usd": market.price_usd,
        "liquidity_usd": market.liquidity_usd,
        "volume_5m_usd": market.volume_5m_usd,
        "buys_5m": market.buys_5m,
        "sells_5m": market.sells_5m,
        "pair_created_at": market.pair_created_at,
        "radar_score": radar.score,
        "reasons": radar.reasons,
        "penalties": radar.penalties,
        "snapshot_count": snapshot_count,
        "triggered_at": market.captured_at,
        "shadow": True,
    }


def _chain_label(chain: str | None) -> str:
    return "BNB CHAIN" if chain == "bsc" else "SOLANA"


def _buttons(p: dict[str, Any]) -> list[dict[str, Any]]:
    chain = p.get("chain") or "solana"
    address = str(p.get("token_address") or "")
    pair = str(p.get("pair_address") or address)
    if not address:
        return []
    links = [
        ("DexScreener", f"https://dexscreener.com/{quote(chain)}/{quote(pair)}"),
        (
            "Open GMGN",
            f"https://gmgn.ai/{'sol' if chain == 'solana' else 'bsc'}/token/{quote(address)}",
        ),
        (
            ("BscScan" if chain == "bsc" else "Solscan"),
            f"https://bscscan.com/token/{quote(address)}"
            if chain == "bsc"
            else f"https://solscan.io/token/{quote(address)}",
        ),
    ]
    return [
        {
            "type": 1,
            "components": [
                {"type": 2, "style": 5, "label": label, "url": url} for label, url in links
            ],
        }
    ]


def format_discord_event(event_type: str, p: dict[str, Any]) -> dict[str, Any]:
    text = format_event(event_type, p)
    colors = {
        "EARLY_RADAR": 0x5865F2,
        "RADAR_MILESTONE": 0x22C55E,
        "RADAR_RISK": 0xDC2626,
        "SIGNAL": 0xF59E0B,
        "MILESTONE": 0xEF4444,
        "UPGRADE": 0xF97316,
        "DETERIORATION": 0xEAB308,
        "FAILED": 0xDC2626,
    }
    title = {
        "EARLY_RADAR": "🛰️ GAMBIT JR — EARLY RADAR",
        "RADAR_MILESTONE": "🔥 GAMBIT JR — RADAR OUTCOME",
        "RADAR_RISK": "⚠️ GAMBIT JR — RADAR RISK",
        "SIGNAL": f"GAMBIT JR — {str(p.get('classification', 'SIGNAL')).replace('_', ' ')}",
        "MILESTONE": "🔥 GAMBIT JR — MILESTONE",
        "UPGRADE": "GAMBIT JR — SIGNAL UPGRADE",
        "DETERIORATION": "⚠️ GAMBIT JR — DETERIORATION",
        "FAILED": "🔴 GAMBIT JR — FAILED",
    }.get(event_type, f"GAMBIT JR — {event_type}")
    address = str(p.get("token_address") or "UNKNOWN")
    fields = [
        {"name": "Chain", "value": _chain_label(p.get("chain")), "inline": True},
        {"name": "Contract address", "value": address, "inline": False},
    ]
    if p.get("market_cap_usd") is not None or p.get("signal_market_cap_usd") is not None:
        fields += [
            {
                "name": "Market cap",
                "value": _money(p.get("market_cap_usd", p.get("signal_market_cap_usd"))),
                "inline": True,
            },
            {"name": "Liquidity", "value": _money(p.get("liquidity_usd")), "inline": True},
        ]
    if event_type == "EARLY_RADAR":
        fields.append(
            {
                "name": "Status",
                "value": "LOWER CONFIDENCE — NOT A QUALIFIED SIGNAL",
                "inline": False,
            }
        )
        fields.append(
            {
                "name": "Qualification",
                "value": "Evidence and safety gates still apply; Radar is not a buy instruction.",
                "inline": False,
            }
        )
    if event_type == "RADAR_MILESTONE":
        fields.append(
            {
                "name": "Observed outcome",
                "value": f"{float(p.get('milestone', 0)):g}x from Radar",
                "inline": False,
            }
        )
    if event_type == "RADAR_RISK":
        fields.append(
            {
                "name": "Observed risk",
                "value": str(p.get("risk") or "LIQUIDITY COLLAPSE"),
                "inline": False,
            }
        )
    return {
        "content": f"{title}\n`{address}`",
        "embeds": [
            {
                "title": title,
                "description": text[:3500],
                "color": colors.get(event_type, 0x64748B),
                "fields": fields,
            }
        ],
        "components": _buttons(p),
        "allowed_mentions": {"parse": []},
    }


def format_event(event_type: str, p: dict[str, Any]) -> str:
    if event_type == "EARLY_RADAR":
        ratio = None
        if p.get("sells_5m") not in (None, 0) and p.get("buys_5m") is not None:
            ratio = float(p["buys_5m"]) / float(p["sells_5m"])
        reasons = "; ".join(_safe(x) for x in p.get("reasons") or []) or "abnormal early activity"
        return (
            f"🛰️ EARLY RADAR — LOWER CONFIDENCE — NOT A QUALIFIED SIGNAL\n"
            f"{_safe(p.get('name'))} (${_safe(p.get('symbol'))}) | {_chain_label(p.get('chain'))}\n"
            f"CA: `{p.get('token_address')}`\n"
            f"MC: {_money(p.get('market_cap_usd'))} | Liquidity: {_money(p.get('liquidity_usd'))} | "
            f"5m Volume: {_money(p.get('volume_5m_usd'))}\n"
            f"Buy/Sell: {_number(ratio)} | Radar score: {_number(p.get('radar_score'))}\n"
            f"Why flagged: {_safe(reasons, 700)}\nStatus: BUILDING EVIDENCE"
        )[:1990]
    if event_type == "SIGNAL":
        shadow = "[SHADOW TEST — REAL DATA, NO TRADING]\n" if p.get("shadow") else ""
        scores = p["component_scores"]
        maxima = p["component_maxima"]
        developer = p.get("developer") or {}
        narrative = p.get("narrative") or {}
        momentum = p.get("momentum") or {}
        social_display = (
            "UNKNOWN"
            if (p.get("social") or {}).get("score") is None
            else f"{scores['social']:.1f}/{maxima['social']:.0f}"
        )
        developer_display = (
            "UNKNOWN"
            if developer.get("score") is None
            else f"{scores['developer']:.1f}/{maxima['developer']:.0f}"
        )
        thesis = (
            "; ".join(_safe(x) for x in p.get("thesis") or [])
            or "Evidence met configured deterministic thresholds."
        )
        risks = (
            "; ".join(_safe(x) for x in p.get("risks") or [])
            or "Very young, highly volatile asset; limited history."
        )
        return (
            f"{shadow}🚨 GAMBIT JR SHADOW — {_safe(p['classification']).replace('_', ' ')}\n\n"
            f"{_safe(p.get('name'))} (${_safe(p.get('symbol'))})\n"
            f"CA: `{_safe(p['token_address'], 80)}`\n\n"
            f"Signal MC: {_money(p.get('signal_market_cap_usd'))}\n"
            f"Liquidity: {_money(p.get('liquidity_usd'))}\n"
            f"Holders: {_number(p.get('holders'))}\n"
            f"5m Volume: {_money(p.get('volume_5m_usd'))}\n"
            f"Score: {p.get('normalized_score', 0):.1f} | Confidence: {p.get('confidence', 0):.0%}\n\n"
            f"Narrative: {scores['narrative']:.1f}/{maxima['narrative']:.0f} ({_safe(narrative.get('label'))})\n"
            f"Social Velocity: {social_display}\n"
            f"On-chain: {scores['onchain']:.1f}/{maxima['onchain']:.0f}\n"
            f"Developer: {developer_display}\n"
            f"Momentum: {scores['momentum']:.1f}/{maxima['momentum']:.0f}\n"
            f"Safety evidence: {scores['safety']:.1f}/{maxima['safety']:.0f}\n\n"
            f"Dev: {_safe(developer.get('classification'))}\n"
            f"Top 10: {_number(p.get('top10_percent'), '%')}\n"
            f"Bundlers: {_number(p.get('bundled_percent'), '%')}\n"
            f"Buy/Sell: {_number(momentum.get('buy_sell_ratio'))}\n\n"
            f"THESIS: {_safe(thesis, 450)}\n\nRISKS: {_safe(risks, 350)}\n\n"
            f"Signal timestamp: {_safe(p.get('signal_timestamp'))} UTC\n"
            f"Scoring: {_safe(p.get('scoring_version'))}\nREAD-ONLY SHADOW SIGNAL — NO TRADE EXECUTED"
        )[:1990]
    if event_type == "MILESTONE":
        multiple = p["milestone"]
        minutes = float(p["seconds_to_hit"]) / 60
        return (
            f"🔥 {multiple:g}X HIT — ${_safe(p.get('symbol'))}\n\n"
            f"CA: `{_safe(p.get('token_address'), 80)}`\n"
            f"Signal MC: {_money(p.get('signal_market_cap_usd'))}\n"
            f"Current MC: {_money(p.get('market_cap_usd'))}\n"
            f"Time to {multiple:g}X: {minutes:.1f}m\n"
            f"ATH since signal: {float(p.get('max_multiple', multiple)):.2f}X"
        )
    if event_type == "FAILED":
        return (
            f"🔴 SIGNAL FAILED — ${_safe(p.get('symbol'))}\n\n"
            f"CA: `{_safe(p.get('token_address'), 80)}`\n"
            f"Signal MC: {_money(p.get('signal_market_cap_usd'))}\n"
            f"Peak: {float(p.get('max_multiple', 0)):.2f}X\n"
            f"Current: {float(p.get('current_multiple', 0)):.2f}X\n"
            f"Max Drawdown: {float(p.get('max_drawdown', 0)):.1%}\n"
            "Observation: configured failure drawdown reached before 2X."
        )
    if event_type == "UPGRADE":
        return (
            f"🟢 GAMBIT JR SHADOW — SIGNAL UPGRADE\n"
            f"${_safe(p.get('symbol'))}: {_safe(p.get('previous_class'))} → {_safe(p.get('new_class'))}\n"
            f"Score: {_number(p.get('normalized_score'))} | "
            f"Confidence: {_number(float(p.get('confidence', 0)) * 100, '%')}\n"
            "READ-ONLY SHADOW UPDATE — NO TRADE EXECUTED"
        )
    if event_type == "DETERIORATION":
        return (
            f"🟠 GAMBIT JR SHADOW — DETERIORATION\n${_safe(p.get('symbol'))}\n"
            f"Score: {_number(p.get('normalized_score'))} | "
            f"Reasons: {_safe('; '.join(p.get('reasons') or []), 500)}\n"
            "Original signal baseline remains unchanged."
        )
    return _safe(p, 1900)
