from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def format_status(stats: dict[str, Any]) -> str:
    started = datetime.fromisoformat(stats["started_at"])
    uptime = datetime.now(timezone.utc) - started
    hours, remainder = divmod(int(uptime.total_seconds()), 3600)
    minutes = remainder // 60
    return (
        "Gambit Jr — ONLINE\n"
        f"Uptime: {hours}h {minutes}m\n"
        f"Tokens discovered: {stats['tokens_discovered']:,}\n"
        f"Tokens evaluated: {stats['tokens_evaluated']:,}\n"
        f"Hard rejected: {stats['hard_rejected']:,}\n"
        f"Pending evidence: {stats['pending_evidence']:,}\n"
        f"Candidates watching: {stats['candidates_watching']:,}\n"
        f"Expired: {stats['expired']:,}\n"
        f"Signals: {stats['signals']:,} (WATCH {stats['watch']}, STRONG {stats['strong']}, HIGH {stats['high_conviction']})\n"
        f"Active signals: {stats['active_signals']:,}\n"
        f"Signals today: {stats['signals_today']:,}\n"
        f"Providers healthy: {stats['providers_healthy']}/{stats['providers_total']}\n"
        f"Database: {stats['database']}"
    )


def format_candidates(rows: list[Any]) -> str:
    if not rows:
        return "No active pre-signal candidates."
    now = datetime.now(timezone.utc)
    blocks = ["Strongest active candidates"]
    for row in rows:
        age = max(0, int((now - datetime.fromisoformat(row["first_discovered_at"])).total_seconds() / 60))
        score = "UNKNOWN" if row["normalized_score"] is None else f"{row['normalized_score']:.1f}"
        confidence = "UNKNOWN" if row["confidence"] is None else f"{row['confidence']:.0%}"
        blocks.append(
            f"${row['symbol'] or 'UNKNOWN'} | MC ${float(row['current_market_cap_usd'] or 0):,.0f} | "
            f"Liq ${float(row['current_liquidity_usd'] or 0):,.0f}\n"
            f"Age {age}m | Snapshots {row['snapshot_count']} | Score {score} | Confidence {confidence}\n"
            f"State {row['state']} — {row['reason'] or 'collecting evidence'}"
        )
    return "\n\n".join(blocks)[:1990]


def format_rejections(report: dict[str, Any]) -> str:
    lines = ["Rejections — last 24h", "", "Hard / terminal"]
    lines += [f"{reason}: {count}" for reason, count in report["hard"][:10]] or ["None"]
    lines += ["", "Temporary / still monitored"]
    lines += [f"{reason}: {count}" for reason, count in report["temporary"][:10]] or ["None"]
    return "\n".join(lines)[:1990]


def format_performance(p: dict[str, Any]) -> str:
    rate = lambda key: "N/A" if p[key] is None else f"{p[key]:.1f}%"
    return (
        f"Performance — scoring {p['scoring_version']}\n"
        f"Signals: {p['total_signals']} "
        f"(WATCH {p['watch']}, STRONG {p['strong']}, HIGH {p['high_conviction']})\n"
        f"1.5X: {p['1.5x_count']} ({rate('1.5x_rate')}) | "
        f"2X: {p['2x_count']} ({rate('2x_rate')}) | 3X: {p['3x_count']} ({rate('3x_rate')})\n"
        f"5X: {p['5x_count']} ({rate('5x_rate')}) | 10X: {p['10x_count']} ({rate('10x_rate')})\n"
        f"Failed: {p['failed']}\n"
        f"Median max multiple: {p['median_max_multiple'] or 'N/A'}\n"
        f"Median drawdown: {p['median_drawdown'] if p['median_drawdown'] is not None else 'N/A'}"
    )
