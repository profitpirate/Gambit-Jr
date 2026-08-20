from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def format_status(stats: dict[str, Any]) -> str:
    started = datetime.fromisoformat(stats["started_at"])
    uptime = datetime.now(timezone.utc) - started
    hours, remainder = divmod(int(uptime.total_seconds()), 3600)
    minutes = remainder // 60
    providers = "\n".join(
        f"{row['provider']}: {'OK' if row['healthy'] else 'DEGRADED'}"
        for row in stats.get("provider_status", [])
    ) or f"Providers healthy: {stats['providers_healthy']}/{stats['providers_total']}"
    return (
        "Gambit Jr — ONLINE\n"
        f"Uptime: {hours}h {minutes}m\n"
        f"Tokens discovered: {stats['tokens_discovered']:,}\n"
        f"Tokens evaluated: {stats['tokens_evaluated']:,}\n"
        f"Hard rejected: {stats['hard_rejected']:,}\n"
        f"Pending evidence: {stats['pending_evidence']:,}\n"
        f"Candidates watching: {stats['candidates_watching']:,}\n"
        f"Early radar flagged: {stats['early_radar']:,}\n"
        f"Expired: {stats['expired']:,}\n"
        f"Signals: {stats['signals']:,} (WATCH {stats['watch']}, STRONG {stats['strong']}, HIGH {stats['high_conviction']})\n"
        f"Discovered outcomes tracked: {stats['outcomes_tracked']:,}\n"
        f"Active signals: {stats['active_signals']:,}\n"
        f"Signals today: {stats['signals_today']:,}\n"
        f"{providers}\n"
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
            f"${row['symbol'] or 'UNKNOWN'} [{str(row['chain']).upper()}] | MC ${float(row['current_market_cap_usd'] or 0):,.0f} | "
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


def format_missed(rows: list[Any], hours: int = 24) -> str:
    if not rows:
        return f"Gambit Jr — Missed Runners, {hours}h\nNo runners met the configured threshold."
    blocks = [f"Gambit Jr — Missed Runners, {hours}h"]
    for row in rows:
        radar = "YES" if row["radar_before_hit"] else "NO"
        blocks.append(
            f"${row['symbol'] or 'UNKNOWN'} | {str(row['chain']).upper()}\n"
            f"Discovery MC: ${float(row['discovery_market_cap_usd'] or 0):,.0f}\n"
            f"Peak: ${float(row['peak_market_cap_usd'] or 0):,.0f} | "
            f"Multiple: {float(row['max_multiple_from_discovery'] or 0):.1f}x\n"
            f"Radar before move: {radar} | Qualified signal before move: NO\n"
            f"Why missed: {row['non_signal_reason'] or row['reason'] or 'unknown'}"
        )
    return "\n\n---\n\n".join(blocks)[:1990]


def format_performance(p: dict[str, Any]) -> str:
    rate = lambda key: "N/A" if p[key] is None else f"{p[key]:.1f}%"
    coverage = p.get("coverage") or {}
    coverage_text = (
        f"\nOpportunity coverage ({coverage.get('major_runner_multiple', 10):g}X runners): "
        f"{coverage.get('major_runners_discovered', 0)} discovered | "
        f"{coverage.get('major_runners_radar', 0)} radar | "
        f"{coverage.get('major_runners_signalled', 0)} signalled | "
        f"{coverage.get('major_runners_completely_missed', 0)} completely missed"
    )
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
        f"{coverage_text}"
    )
