from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def format_status(stats: dict[str, Any]) -> str:
    started = datetime.fromisoformat(stats["started_at"])
    uptime = datetime.now(timezone.utc) - started
    hours, remainder = divmod(int(uptime.total_seconds()), 3600)
    minutes = remainder // 60
    return (
        "Scanner: ONLINE\n"
        f"Uptime: {hours}h {minutes}m\n"
        f"Tokens discovered: {stats['tokens_discovered']:,}\n"
        f"Tokens evaluated: {stats['tokens_evaluated']:,}\n"
        f"Hard rejected: {stats['hard_rejected']:,}\n"
        f"Active signals: {stats['active_signals']:,}\n"
        f"Signals today: {stats['signals_today']:,}\n"
        f"Providers healthy: {stats['providers_healthy']}/{stats['providers_total']}\n"
        f"Database: {stats['database']}"
    )


def format_performance(p: dict[str, Any]) -> str:
    rate = lambda key: "N/A" if p[key] is None else f"{p[key]:.1f}%"
    return (
        f"Performance — scoring {p['scoring_version']}\n"
        f"Signals: {p['total_signals']} "
        f"(WATCH {p['watch']}, STRONG {p['strong']}, HIGH {p['high_conviction']})\n"
        f"2X: {p['2x_count']} ({rate('2x_rate')}) | 3X: {p['3x_count']} ({rate('3x_rate')})\n"
        f"5X: {p['5x_count']} ({rate('5x_rate')}) | 10X: {p['10x_count']} ({rate('10x_rate')})\n"
        f"Failed: {p['failed']}\n"
        f"Median max multiple: {p['median_max_multiple'] or 'N/A'}\n"
        f"Median drawdown: {p['median_drawdown'] if p['median_drawdown'] is not None else 'N/A'}"
    )
