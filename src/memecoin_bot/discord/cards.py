from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

BRAND = {
    "near_black": 0x0B0B0C,
    "charcoal": 0x1B1B1E,
    "burnt_orange": 0xD96B1D,
    "off_white": 0xF4F0E8,
    "muted_grey": 0x77777E,
    "risk": 0xA63D2F,
    "positive": 0xB45F16,
}
COLORS = {
    "orange": BRAND["burnt_orange"],
    "blue": BRAND["burnt_orange"],
    "green": BRAND["positive"],
    "amber": BRAND["burnt_orange"],
    "red": BRAND["risk"],
    "grey": BRAND["muted_grey"],
    "purple": BRAND["burnt_orange"],
}


def _money(value: Any) -> str:
    if value is None:
        return "UNKNOWN"
    value = float(value)
    return (
        f"${value / 1_000_000:.2f}M"
        if value >= 1_000_000
        else f"${value / 1_000:.1f}K"
        if value >= 1_000
        else f"${value:,.2f}"
    )


def _value(value: Any, fallback: str = "UNKNOWN") -> str:
    return fallback if value is None or value == "" else str(value)


def _field(name: str, value: Any, inline: bool = True) -> dict[str, Any]:
    return {"name": name, "value": _value(value)[:1024], "inline": inline}


def card(
    title: str,
    description: str = "",
    color: str = "orange",
    fields: Iterable[dict[str, Any]] = (),
    footer: str = "GAMBIT JR • READ-ONLY INTELLIGENCE • NO EXECUTION",
    links: Iterable[tuple[str, str]] = (),
) -> dict[str, Any]:
    return {
        "embed": {
            "title": title[:256],
            "description": description[:4096],
            "color": COLORS[color],
            "fields": list(fields)[:25],
            "footer": {"text": footer[:2048]},
            "timestamp": datetime.now(UTC).isoformat(),
        },
        "links": list(links)[:5],
    }


def status_card(stats: dict[str, Any]) -> dict[str, Any]:
    providers = (
        "\n".join(
            f"{'🟢' if p['state'] == 'HEALTHY' else '⚪' if p['state'] == 'DISABLED' else '🟠' if p['state'] in {'DEGRADED', 'RATE_LIMITED', 'CIRCUIT_OPEN'} else '🔴'} "
            f"{str(p['provider']).upper()} — {str(p['state']).replace('_', ' ')}"
            for p in stats.get("provider_status", [])
        )
        or "No provider observations yet"
    )
    reconciliation = stats.get("state_reconciliation", {})
    v14 = stats.get("v14") or {}
    queue = stats.get("event_queue") or {}
    live = (
        f"Watching: **{stats.get('candidates_watching', 0)}**\n"
        f"Pending: **{stats.get('pending_evidence', 0)}**\n"
        f"Radar: **{stats.get('early_radar', 0)}**\n"
        f"Active signals: **{stats.get('active_signals', 0)}**"
    )
    pipeline = (
        f"Pending >1h: **{stats.get('pending_over_1h', 0)}**\n"
        f"Pending >3h: **{stats.get('pending_over_3h', 0)}**\n"
        f"Stale beyond TTL: **{stats.get('stale_beyond_ttl', 0)}**"
    )
    lifetime = (
        f"Discovered: **{stats.get('tokens_discovered', 0)}**\n"
        f"Evaluated: **{stats.get('tokens_evaluated', 0)}**\n"
        f"Signals: **{stats.get('signals', 0)}**\nExpired: **{stats.get('expired', 0)}**"
    )
    return card(
        "GAMBIT JR • SYSTEM STATUS",
        "Discord-native shadow intelligence status",
        "green",
        [
            _field("LIVE", live, False),
            _field("PIPELINE / PENDING", pipeline, False),
            _field("PROVIDERS", providers, False),
            _field("LIFETIME", lifetime, False),
            _field(
                "DISCORD DELIVERY",
                f"Outbox pending: **{stats.get('outbox_pending', 0)}**\n"
                f"Delivery pending/failed: **{stats.get('discord_deliveries_pending', 0)} / {stats.get('discord_deliveries_failed', 0)}**\n"
                f"Last error: {_value(stats.get('last_alert_error'), 'NONE')}",
                False,
            ),
            _field(
                "STATE RECONCILIATION",
                "OK"
                if reconciliation.get("difference") == 0
                else f"DIFFERENCE {reconciliation.get('difference')}",
                False,
            ),
            _field(
                "ALPHA ENGINE",
                f"Event queue: **{queue.get('size', 0)} / {queue.get('maxsize', 0)}**\n"
                f"Persisted events: **{v14.get('event_queue_persisted', 0)}**\n"
                f"Wallet clusters: **{v14.get('wallet_clusters', 0)}**",
                False,
            ),
        ],
    )


def menu_card() -> dict[str, Any]:
    return card(
        "GAMBIT JR • COMMAND CENTER",
        "Ultra-early, read-only intelligence from launch through measured outcome.",
        fields=[
            _field(
                "START HERE",
                "`/scan` any supported CA • `/compare` two tokens • `/watch` a token",
                False,
            ),
            _field(
                "LIVE INTELLIGENCE",
                "`/radar` `/runners` `/failed` `/token` `/smartmoney`",
                False,
            ),
            _field(
                "GRAPHS & CONTEXT",
                "`/wallet` `/clusters` `/creator` `/narrative`",
                False,
            ),
            _field(
                "OPERATIONS",
                "`/status` `/performance` `/watchlist` `/server-settings`",
                False,
            ),
            _field(
                "HOW JR THINKS",
                "ATTENTION → LAUNCH → T0 → GRAPH → SURVIVAL → ASYMMETRY → GENESIS → QUALIFIED",
                False,
            ),
        ],
    )


def scan_card(data: dict[str, Any]) -> dict[str, Any]:
    market = data.get("market") or {}
    survival = data.get("survival") or {}
    payoff = data.get("payoff") or {}
    providers = "\n".join(
        f"{name.upper()} — {value.get('state', 'UNKNOWN')}"
        for name, value in (data.get("providers") or {}).items()
    )
    return card(
        f"SCAN • {_value(market.get('symbol') or data.get('token_address'))}",
        f"`{_value(data.get('token_address'))}`\nManual intelligence only; this scan creates no Radar or signal.",
        fields=[
            _field("Chain", str(data.get("chain", "UNKNOWN")).upper()),
            _field("Market state", data.get("state")),
            _field("Entry", data.get("entry_state")),
            _field("Market cap", _money(market.get("market_cap_usd"))),
            _field("Liquidity", _money(market.get("liquidity_usd"))),
            _field("5m volume", _money(market.get("volume_5m_usd"))),
            _field("Survival", survival.get("grade")),
            _field("Payoff", payoff.get("grade")),
            _field("Latency", f"{float(data.get('latency_ms') or 0):.0f} ms"),
            _field("Providers", providers or "No provider evidence", False),
            _field("Unknowns", ", ".join(data.get("unknowns") or []) or "None", False),
        ],
        links=token_links(data | {"pair_address": market.get("pair_address")}),
    )


def compare_card(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    def line(value: dict[str, Any]) -> str:
        market = value.get("market") or {}
        return (
            f"`{value.get('token_address')}`\n"
            f"MC {_money(market.get('market_cap_usd'))} • Liq {_money(market.get('liquidity_usd'))}\n"
            f"Entry {value.get('entry_state')} • Survival {(value.get('survival') or {}).get('grade')} • "
            f"Payoff {(value.get('payoff') or {}).get('grade')}"
        )

    return card(
        "COMPARE • READ-ONLY SETUPS",
        fields=[_field("TOKEN A", line(left), False), _field("TOKEN B", line(right), False)],
    )


def watchlist_card(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    return rows_card(
        "MY WATCHLIST",
        rows,
        "Your watchlist is empty. Use `/watch` to add a token.",
        lambda row: f"**{row['chain'].upper()}** • `{row['token_address']}`",
    )


def wallet_card(data: dict[str, Any]) -> dict[str, Any]:
    return card(
        "WALLET GRAPH",
        f"`{_value(data.get('wallet'))}`\nRelationships are informational, never an automatic verdict.",
        fields=[
            _field("Known chains", len(data.get("nodes") or [])),
            _field("Relationships", len(data.get("edges") or [])),
            _field("Clusters", len(data.get("clusters") or [])),
            _field(
                "Relationship types",
                ", ".join(
                    sorted({row.get("relationship", "UNKNOWN") for row in data.get("edges") or []})
                )
                or "UNKNOWN",
                False,
            ),
        ],
    )


def creator_card(data: dict[str, Any] | None, creator: str) -> dict[str, Any]:
    if not data:
        return card("CREATOR INTELLIGENCE", f"`{creator}`\nCreator history is UNKNOWN.", "grey")
    return card(
        "CREATOR INTELLIGENCE",
        f"`{creator}`",
        fields=[
            _field("Quality", data.get("quality")),
            _field("Launches", data.get("launches")),
            _field("Survived", data.get("survived")),
            _field("Runners", data.get("runners")),
            _field("Rugs / failures", data.get("rugs")),
            _field("Last observed", data.get("last_seen_at"), False),
        ],
    )


def narrative_card(rows: Iterable[dict[str, Any]], query: str | None = None) -> dict[str, Any]:
    return rows_card(
        f"NARRATIVES{f' • {query}' if query else ''}",
        rows,
        "No narrative evidence is available.",
        lambda row: (
            f"**{row.get('label')}** • {row.get('freshness')} • {row.get('saturation')} • "
            f"leader {row.get('leader_symbol') or row.get('leader_address') or 'UNKNOWN'}"
        ),
    )


def token_links(data: dict[str, Any]) -> list[tuple[str, str]]:
    chain = data.get("chain") or "solana"
    address = quote(str(data.get("token_address") or ""))
    pair = quote(str(data.get("pair_address") or data.get("token_address") or ""))
    if not address:
        return []
    return [
        ("DexScreener", f"https://dexscreener.com/{quote(chain)}/{pair}"),
        ("GMGN", f"https://gmgn.ai/{'sol' if chain == 'solana' else 'bsc'}/token/{address}"),
        (
            "Explorer",
            f"https://solscan.io/token/{address}"
            if chain == "solana"
            else f"https://bscscan.com/token/{address}",
        ),
    ]


def token_card(data: dict[str, Any]) -> dict[str, Any]:
    wallet = data.get("wallet_intelligence") or {}
    return card(
        f"TOKEN • {_value(data.get('name') or data.get('symbol'))}",
        f"`{_value(data.get('token_address'))}`",
        "purple",
        [
            _field("Chain", str(data.get("chain", "UNKNOWN")).upper()),
            _field("Lifecycle", data.get("state")),
            _field("Signal", data.get("signal_status")),
            _field("Radar score", data.get("radar_score")),
            _field("Score", data.get("normalized_score")),
            _field("Confidence", f"{float(data.get('confidence') or 0):.0%}"),
            _field("Market cap", _money(data.get("current_market_cap_usd"))),
            _field("Liquidity", _money(data.get("current_liquidity_usd"))),
            _field("Peak", f"{float(data.get('max_multiple') or 0):.2f}x"),
            _field("Smart money", wallet.get("smart_money"), False),
            _field(
                "Evidence gaps",
                ", ".join(data.get("unknown_fields") or []) or "None recorded",
                False,
            ),
        ],
        links=token_links(data),
    )


def smartmoney_card(data: dict[str, Any]) -> dict[str, Any]:
    wallet = data.get("wallet_intelligence") or {}
    counts = wallet.get("counts") or {}
    return card(
        f"SMART MONEY • {_value(data.get('symbol'))}",
        "Wallet evidence; labels are not guarantees.",
        "purple",
        [
            _field("State", wallet.get("smart_money")),
            _field("Buyer diversity", wallet.get("buyer_diversity")),
            _field("Activity quality", wallet.get("activity_quality")),
            _field("Smart wallets", counts.get("smart", "UNKNOWN")),
            _field("Snipers", counts.get("sniper", "UNKNOWN")),
            _field("Bundlers", counts.get("bundler", "UNKNOWN")),
            _field("Possible cluster", wallet.get("possible_wallet_cluster", "UNKNOWN"), False),
        ],
        links=token_links(data),
    )


def rows_card(
    title: str, rows: Iterable[Any], empty: str, formatter: Any, color: str = "blue"
) -> dict[str, Any]:
    lines = [formatter(dict(row) if not isinstance(row, dict) else row) for row in rows]
    return card(title, "\n".join(lines)[:4000] if lines else empty, color)


def performance_card(report: dict[str, Any]) -> dict[str, Any]:
    sample = int(report.get("total_signals") or 0)
    warning = "\n\n**SMALL SAMPLE • NOT YET RELIABLE**" if report.get("small_sample") else ""
    return card(
        "PERFORMANCE • MEASURED OUTCOMES",
        f"Historical shadow results, never promises.{warning}",
        "green",
        [
            _field("Signals", report.get("total_signals")),
            _field("Failed", report.get("failed")),
            _field("Median peak", f"{float(report.get('median_max_multiple') or 0):.2f}x"),
            _field("2x rate", f"{float(report.get('2x_rate') or 0):.1f}%"),
            _field("5x rate", f"{float(report.get('5x_rate') or 0):.1f}%"),
            _field("10x rate", f"{float(report.get('10x_rate') or 0):.1f}%"),
            _field("Mature sample", sample),
            _field(
                "Qualified 2x precision",
                _value((report.get("right_tail") or {}).get("qualified_2x_precision")),
            ),
        ],
    )


def settings_card(settings: dict[str, Any] | None) -> dict[str, Any]:
    if not settings:
        return card(
            "SERVER SETTINGS",
            "Automatic alerts are disabled until an administrator runs `/setup`.",
            "grey",
        )
    return card(
        "SERVER SETTINGS",
        "One shared scanner; guild-specific presentation and delivery.",
        "blue",
        [
            _field("Alerts", "ENABLED" if settings.get("alerts_enabled") else "DISABLED"),
            _field(
                "Alert channel",
                f"<#{settings.get('alert_channel_id')}>"
                if settings.get("alert_channel_id")
                else "NOT SET",
            ),
            _field("Alert tier", settings.get("alert_tier")),
            _field(
                "Daily report",
                "ENABLED" if settings.get("daily_report_enabled") else "DISABLED",
            ),
            _field("Chains", ", ".join(settings.get("enabled_chains") or []) or "NONE"),
            _field("Updated", settings.get("updated_at"), False),
        ],
    )


def test_alert_card() -> dict[str, Any]:
    return card(
        "GAMBIT JR • TEST ALERT",
        "TEST / NON-LIVE — Discord delivery and rich-card rendering succeeded.",
        "amber",
        [
            _field("Creates signal", "NO"),
            _field("Creates Radar", "NO"),
            _field("Trading", "DISABLED"),
        ],
        "TEST EVENT • NOT MARKET INTELLIGENCE • NO TRADE EXECUTED",
    )
