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


def _confidence(value: float | None) -> str:
    if value is None:
        return "UNKNOWN"
    numeric = float(value)
    return f"{numeric * 100:.0f}%" if 0 <= numeric <= 1 else f"{numeric:.1f}%"


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
    components = [
        {
            "type": 2,
            "style": 1,
            "label": "Copy CA",
            "custom_id": "gambit:token:copy_ca",
        },
    ]
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
    components.extend({"type": 2, "style": 5, "label": label, "url": url} for label, url in links)
    components.append(
        {
            "type": 2,
            "style": 2,
            "label": "Watch",
            "custom_id": "gambit:token:watch",
        }
    )
    return [{"type": 1, "components": components}]


def format_discord_event(event_type: str, p: dict[str, Any]) -> dict[str, Any]:
    text = format_event(event_type, p)
    colors = {
        "GENESIS_RADAR": 0xD96B1D,
        "EARLY_RADAR": 0xD96B1D,
        "HOT_RADAR": 0xD96B1D,
        "PRIORITY_RADAR": 0xD96B1D,
        "RADAR_MILESTONE": 0xB45F16,
        "RADAR_RISK": 0xA63D2F,
        "SIGNAL": 0xD96B1D,
        "MILESTONE": 0xD96B1D,
        "UPGRADE": 0xD96B1D,
        "DETERIORATION": 0xD96B1D,
        "FAILED": 0xA63D2F,
    }
    title = {
        "GENESIS_RADAR": "GAMBIT JR — GENESIS RADAR",
        "EARLY_RADAR": "GAMBIT JR — STANDARD RADAR",
        "HOT_RADAR": "GAMBIT JR — HOT RADAR",
        "PRIORITY_RADAR": "GAMBIT JR — PRIORITY RADAR",
        "RADAR_MILESTONE": "GAMBIT JR — RADAR OUTCOME",
        "RADAR_RISK": "GAMBIT JR — RADAR RISK",
        "SIGNAL": (
            f"{_safe(p.get('v15_signal_tier') or p.get('classification') or 'SIGNAL')} • "
            f"{_safe(p.get('name') or p.get('symbol'))} (${_safe(p.get('symbol'))})"
        ),
        "MILESTONE": "GAMBIT JR — MILESTONE",
        "UPGRADE": "GAMBIT JR — SIGNAL UPGRADE",
        "DETERIORATION": "GAMBIT JR — DETERIORATION",
        "FAILED": "GAMBIT JR — FAILED",
    }.get(event_type, f"GAMBIT JR — {event_type}")
    address = str(p.get("token_address") or "UNKNOWN")
    if event_type == "SIGNAL":
        why_now = p.get("why_now") or p.get("thesis") or []
        risks = p.get("failure_reasons") or p.get("risks") or []
        historical = p.get("historical_context") or {}
        fields = [
            {
                "name": "Tier",
                "value": _safe(p.get("v15_signal_tier")),
                "inline": True,
            },
            {"name": "Chain", "value": _chain_label(p.get("chain")), "inline": True},
            {"name": "Contract address", "value": address, "inline": False},
            {
                "name": "Market cap",
                "value": _money(p.get("market_cap_usd", p.get("signal_market_cap_usd"))),
                "inline": True,
            },
            {"name": "Liquidity", "value": _money(p.get("liquidity_usd")), "inline": True},
            {
                "name": "Entry",
                "value": _safe(p.get("entry_status")),
                "inline": True,
            },
            {
                "name": "Runner potential",
                "value": _number(p.get("runner_score")),
                "inline": True,
            },
            {
                "name": "Failure risk",
                "value": _number(p.get("failure_score")),
                "inline": True,
            },
            {
                "name": "Confidence / evidence coverage",
                "value": (
                    f"{_confidence(p.get('confidence'))} / "
                    f"{_number(p.get('evidence_coverage'), '%')}"
                ),
                "inline": True,
            },
            {
                "name": "Why now",
                "value": _safe(" • ".join(why_now) or "No additional verified catalyst", 700),
                "inline": False,
            },
            {
                "name": "Historical context",
                "value": (
                    f"{_safe(historical.get('state'))} • "
                    f"{len(historical.get('features') or [])} approved features"
                    if historical
                    else "UNKNOWN • no approved point-in-time context"
                ),
                "inline": False,
            },
            {
                "name": "Risks",
                "value": _safe(" • ".join(risks) or "No additional known risk", 700),
                "inline": False,
            },
        ]
    else:
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
                {
                    "name": "Liquidity",
                    "value": _money(p.get("liquidity_usd")),
                    "inline": True,
                },
            ]
    if event_type in {"GENESIS_RADAR", "EARLY_RADAR"}:
        fields.append(
            {
                "name": "Status",
                "value": (
                    "EXTREMELY EARLY • HIGH UNCERTAINTY • NOT A QUALIFIED SIGNAL"
                    if event_type == "GENESIS_RADAR"
                    else "LOWER CONFIDENCE • NOT A QUALIFIED SIGNAL"
                ),
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
                "footer": {"text": "GAMBIT JR • READ-ONLY INTELLIGENCE • NO EXECUTION"},
            }
        ],
        "components": _buttons(p),
        "allowed_mentions": {"parse": []},
    }


def format_event(event_type: str, p: dict[str, Any]) -> str:
    if event_type == "GENESIS_RADAR":
        reasons = "; ".join(_safe(x) for x in p.get("reasons") or []) or "verified launch event"
        unknowns = ", ".join(_safe(x) for x in p.get("unknowns") or []) or "none recorded"
        return (
            "GENESIS RADAR — EXTREMELY EARLY / HIGH UNCERTAINTY / NOT A QUALIFIED SIGNAL\n"
            f"{_chain_label(p.get('chain'))} • {_safe(p.get('launchpad'))}\n"
            f"CA: `{_safe(p.get('token_address'), 80)}`\n"
            f"Entry: {_safe(p.get('entry_state'))} • Confidence: {float(p.get('confidence') or 0):.0%}\n"
            f"Why now: {_safe(reasons, 700)}\nUnknown: {_safe(unknowns, 500)}\n"
            "READ-ONLY SHADOW INTELLIGENCE — NO EXECUTION"
        )[:1990]
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
        scores = p.get("component_scores") or {}
        maxima = p.get("component_maxima") or {}

        def component(name: str) -> str:
            score = scores.get(name)
            maximum = maxima.get(name)
            return (
                "UNKNOWN"
                if score is None or maximum is None
                else f"{float(score):.1f}/{float(maximum):.0f}"
            )

        developer = p.get("developer") or {}
        narrative = p.get("narrative") or {}
        momentum = p.get("momentum") or {}
        social_display = (
            "UNKNOWN" if (p.get("social") or {}).get("score") is None else component("social")
        )
        developer_display = "UNKNOWN" if developer.get("score") is None else component("developer")
        thesis = (
            "; ".join(_safe(x) for x in p.get("thesis") or [])
            or "Evidence met configured deterministic thresholds."
        )
        risks = (
            "; ".join(_safe(x) for x in p.get("risks") or [])
            or "Very young, highly volatile asset; limited history."
        )
        return (
            f"{shadow}🚨 GAMBIT JR SHADOW — {_safe(p.get('classification') or p.get('v15_signal_tier')).replace('_', ' ')}\n\n"
            f"{_safe(p.get('name'))} (${_safe(p.get('symbol'))})\n"
            f"CA: `{_safe(p.get('token_address'), 80)}`\n\n"
            f"Signal MC: {_money(p.get('signal_market_cap_usd'))}\n"
            f"Liquidity: {_money(p.get('liquidity_usd'))}\n"
            f"Holders: {_number(p.get('holders'))}\n"
            f"5m Volume: {_money(p.get('volume_5m_usd'))}\n"
            f"Score: {p.get('normalized_score', 0):.1f} | Confidence: {p.get('confidence', 0):.0%}\n\n"
            f"V1.5 Tier: {_safe(p.get('v15_signal_tier'))} | "
            f"Runner: {_number(p.get('runner_score'))} | Failure: {_number(p.get('failure_score'))}\n"
            f"Coverage: {_number(p.get('evidence_coverage'), '%')} | "
            f"Entry: {_safe(p.get('entry_status'))} | Survival: {_safe(p.get('survival_grade'))}\n\n"
            f"Narrative: {component('narrative')} ({_safe(narrative.get('label'))})\n"
            f"Social Velocity: {social_display}\n"
            f"On-chain: {component('onchain')}\n"
            f"Developer: {developer_display}\n"
            f"Momentum: {component('momentum')}\n"
            f"Safety evidence: {component('safety')}\n\n"
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
