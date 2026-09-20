"""Enrich the V12 unknown-creator apprenticeship with prior E4 creator history.

This is research-only. It never edits the frozen V12 model, creator registry,
selector, or production execution paths.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from collections.abc import Mapping
from typing import Any


def load(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n")
    tmp.replace(path)


def finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def index_history(
    expectancy: Mapping[str, Any],
    winners: Mapping[str, Any],
    discovered: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in expectancy.get("top_creators", []) or []:
        if not isinstance(row, Mapping):
            continue
        creator = str(row.get("creator") or "")
        if not creator:
            continue
        out.setdefault(creator, {})["expectancy"] = dict(row)
    for creator, row in (winners.get("creators") or {}).items():
        if isinstance(row, Mapping):
            out.setdefault(str(creator), {})["winner_registry"] = dict(row)
    for creator, row in (discovered.get("creators") or {}).items():
        if isinstance(row, Mapping):
            out.setdefault(str(creator), {})["discovered_registry"] = dict(row)
    return out


def history_metrics(history: Mapping[str, Any]) -> dict[str, Any]:
    exp = history.get("expectancy") or {}
    winreg = history.get("winner_registry") or {}
    wins = int(exp.get("wins") or winreg.get("e4_observed_wins") or 0)
    losses = int(exp.get("losses") or 0)
    trades = int(exp.get("trades") or (wins + losses))
    winner_mints = list(exp.get("winner_mints") or winreg.get("winner_mints") or [])
    loser_mints = list(exp.get("loser_mints") or [])
    wr = wins / trades if trades else 0.0
    winning_pnl = finite(exp.get("winning_pnl_sol"), finite(winreg.get("e4_gross_pnl_sol")))
    return {
        "trades": trades,
        "wins": wins,
        "losses": losses,
        "win_rate": wr,
        "winning_pnl_sol": winning_pnl,
        "winner_mints": winner_mints,
        "loser_mints": loser_mints,
        "repeat_winner": wins >= 2,
        "pure_repeat_winner": wins >= 2 and losses == 0,
        "strong_history": bool(wins >= 2 and wr >= 2.0 / 3.0 and winning_pnl > 0),
        "instant_repeat_candidate": bool(winreg.get("instant_repeat_candidate")),
        "discovered_registry": history.get("discovered_registry"),
    }


def fresh_metrics(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "status": row.get("status"),
        "launches_seen": int(row.get("launches_seen") or 0),
        "scout_fills": int(row.get("scout_fills") or 0),
        "scout_wins": int(row.get("scout_wins") or 0),
        "scout_losses": int(row.get("scout_losses") or 0),
        "scout_net_pnl_sol": finite(row.get("scout_net_pnl_sol")),
        "v12_compatible_fills": int(row.get("v12_compatible_fills") or 0),
        "v12_compatible_wins": int(row.get("wins") or 0),
        "v12_compatible_losses": int(row.get("losses") or 0),
        "v12_compatible_net_pnl_sol": finite(row.get("net_pnl_sol")),
        "runner_2x_count": int(row.get("runner_2x_count") or 0),
        "runner_3x_count": int(row.get("runner_3x_count") or 0),
        "fresh_winning_mints": list(row.get("distinct_scout_winning_mints") or []),
        "fresh_v12_winning_mints": list(row.get("distinct_winning_mints") or []),
    }


def classify(history: Mapping[str, Any], fresh: Mapping[str, Any]) -> tuple[str, bool]:
    h = history_metrics(history)
    f = fresh_metrics(fresh)
    fresh_scout_positive = f["scout_wins"] >= 1 and f["scout_net_pnl_sol"] > 0
    fresh_v12_positive = (
        f["v12_compatible_wins"] >= 1
        and f["v12_compatible_net_pnl_sol"] > 0
    )
    if h["strong_history"] and fresh_v12_positive:
        return "PROMOTION_CANDIDATE_HISTORICAL_PLUS_FRESH", True
    if h["pure_repeat_winner"] and (fresh_scout_positive or f["runner_2x_count"] > 0):
        return "SHORTLISTED_PURE_REPEAT_HISTORY_PLUS_FRESH_SIGNAL", False
    if h["repeat_winner"] and fresh_scout_positive:
        return "SHORTLISTED_REPEAT_HISTORY_PLUS_FRESH_WIN", False
    if h["strong_history"]:
        return "HISTORICALLY_STRONG_REOBSERVED", False
    if h["trades"] > 0 and fresh_scout_positive:
        return "SHORTLISTED_PRIOR_HISTORY_PLUS_FRESH_WIN", False
    if fresh.get("status") in {
        "SHORTLISTED",
        "CONFIRMED_REPEAT_WINNER",
        "PROMOTION_CANDIDATE",
    }:
        return str(fresh.get("status")), bool(fresh.get("promotion_ready"))
    return str(fresh.get("status") or "DISCOVERED"), False


def render(result: Mapping[str, Any]) -> str:
    counts = result["counts"]
    lines = [
        "# V12 unknown-creator historical apprenticeship backfill",
        "",
        "**Research-only:** no frozen creator registry or live selector was changed.",
        "",
        "## Counts",
        "",
        f"- Fresh unknown launches analysed: {counts['fresh_unknown_launches']}",
        f"- Unique unknown creators: {counts['unique_unknown_creators']}",
        f"- Creators with prior E4 history: {counts['historical_overlap_creators']}",
        f"- Prior repeat winners rediscovered: {counts['historical_repeat_winners']}",
        f"- Prior pure repeat winners rediscovered: {counts['historical_pure_repeat_winners']}",
        f"- Fresh scout shortlists: {counts['fresh_shortlisted_creators']}",
        f"- Historical + fresh promotion candidates: {counts['promotion_candidates']}",
        "",
        "## Promotion candidates",
        "",
    ]
    for row in result["promotion_candidates"][:50]:
        h = row["history"]
        f = row["fresh"]
        lines.append(
            f"- {row['creator']}: prior {h['wins']}W/{h['losses']}L "
            f"({h['win_rate']:.1%}), fresh V12-compatible "
            f"{f['v12_compatible_wins']}W/{f['v12_compatible_losses']}L, "
            f"fresh net {f['v12_compatible_net_pnl_sol']:.4f} SOL; "
            f"prior winners={','.join(h['winner_mints'][:5])}"
        )
    lines += ["", "## Strong historical overlaps not promoted", ""]
    for row in result["strong_historical_overlaps"][:50]:
        h = row["history"]
        f = row["fresh"]
        lines.append(
            f"- {row['creator']}: {row['classification']} | "
            f"prior {h['wins']}W/{h['losses']}L ({h['win_rate']:.1%}); "
            f"fresh scout wins={f['scout_wins']}, runners2x={f['runner_2x_count']}"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--apprentice-state", type=Path, required=True)
    p.add_argument("--expectancy", type=Path, required=True)
    p.add_argument("--winning-creators", type=Path, required=True)
    p.add_argument("--discovered-creators", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--report", type=Path, required=True)
    args = p.parse_args()

    state = load(args.apprentice_state, {}) or {}
    history = index_history(
        load(args.expectancy, {}) or {},
        load(args.winning_creators, {}) or {},
        load(args.discovered_creators, {}) or {},
    )
    rows = []
    for creator, fresh in (state.get("creators") or {}).items():
        prior = history.get(str(creator), {})
        classification, promotion = classify(prior, fresh)
        rows.append(
            {
                "creator": str(creator),
                "classification": classification,
                "promotion_ready": promotion,
                "history": history_metrics(prior),
                "fresh": fresh_metrics(fresh),
            }
        )
    rows.sort(
        key=lambda row: (
            not row["promotion_ready"],
            -row["history"]["wins"],
            row["history"]["losses"],
            -row["fresh"]["scout_wins"],
            -row["fresh"]["runner_2x_count"],
        )
    )
    promotions = [row for row in rows if row["promotion_ready"]]
    strong = [
        row for row in rows
        if row["history"]["strong_history"] and not row["promotion_ready"]
    ]
    overlap = [row for row in rows if row["history"]["trades"] > 0]
    counts = {
        "fresh_unknown_launches": int((state.get("counts") or {}).get("unknown_launch_observations") or 0),
        "unique_unknown_creators": len(rows),
        "historical_overlap_creators": len(overlap),
        "historical_repeat_winners": sum(row["history"]["repeat_winner"] for row in overlap),
        "historical_pure_repeat_winners": sum(row["history"]["pure_repeat_winner"] for row in overlap),
        "fresh_shortlisted_creators": sum(
            row["fresh"]["status"] in {
                "SHORTLISTED",
                "CONFIRMED_REPEAT_WINNER",
                "PROMOTION_CANDIDATE",
            }
            for row in rows
        ),
        "promotion_candidates": len(promotions),
    }
    result = {
        "version": "v12-creator-history-backfill-v1",
        "shadow_only": True,
        "automatic_whitelist_mutation": False,
        "counts": counts,
        "promotion_candidates": promotions,
        "strong_historical_overlaps": strong,
        "historical_overlaps": overlap,
    }
    write(args.output, result)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(render(result), encoding="utf-8")
    print(json.dumps(counts, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
