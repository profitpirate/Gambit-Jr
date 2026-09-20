"""Rebuild the canonical V12 creator library from E4 history + fresh apprenticeship.

The active library is intentionally small:
- every verified E4 repeat winner (>=2 wins) is promoted;
- every fresh apprenticeship promotion candidate is promoted;
- only the highest-potential non-promoted creators are shortlisted.

This module never edits the frozen 100-trade causal model.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

SCHEMA = "v12-creator-library-v2"
E4_SHORTLIST_LIMIT = 25
FRESH_SHORTLIST_LIMIT = 50


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n")
    tmp.replace(path)


def finite(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def e4_score(row: dict[str, Any]) -> float:
    wins = int(row.get("wins") or 0)
    losses = int(row.get("losses") or 0)
    trades = max(1, int(row.get("trades") or wins + losses))
    wr = wins / trades
    pnl = max(0.0, finite(row.get("winning_pnl_sol")))
    repeat = min(1.0, wins / 5.0)
    pnl_component = min(1.0, math.log1p(pnl) / math.log(11.0))
    purity = 1.0 if losses == 0 else max(0.0, 1.0 - losses / trades)
    return 0.40 * wr + 0.25 * repeat + 0.20 * pnl_component + 0.15 * purity


def fresh_score(row: dict[str, Any]) -> float:
    fills = int(row.get("v12_compatible_fills") or 0)
    wins = int(row.get("wins") or 0)
    wr = wins / fills if fills else 0.0
    scout_wins = int(row.get("scout_wins") or 0)
    runner2 = int(row.get("runner_2x_count") or 0)
    runner3 = int(row.get("runner_3x_count") or 0)
    net = max(0.0, finite(row.get("net_pnl_sol")))
    scout_net = max(0.0, finite(row.get("scout_net_pnl_sol")))
    pf = finite(row.get("profit_factor"))
    if pf <= 0 and wins and int(row.get("losses") or 0) == 0:
        pf = 10.0
    return (
        0.25 * min(1.0, wr)
        + 0.20 * min(1.0, scout_wins / 3.0)
        + 0.20 * min(1.0, (runner2 + 2 * runner3) / 4.0)
        + 0.15 * min(1.0, math.log1p((net + scout_net) * 10.0) / math.log(6.0))
        + 0.20 * min(1.0, pf / 4.0)
    )


def e4_record(row: dict[str, Any]) -> dict[str, Any]:
    wins = int(row.get("wins") or 0)
    losses = int(row.get("losses") or 0)
    trades = int(row.get("trades") or wins + losses)
    return {
        "creator": str(row["creator"]),
        "source": "E4_VERIFIED_HISTORY",
        "tier": (
            "ELITE"
            if (losses == 0 and wins >= 3) or (wins >= 4 and wins / max(trades, 1) >= 0.80)
            else "PROMOTED"
        ),
        "status": "PROMOTED",
        "e4": {
            "wins": wins,
            "losses": losses,
            "trades": trades,
            "win_rate": wins / max(trades, 1),
            "winning_pnl_sol": finite(row.get("winning_pnl_sol")),
            "winner_mints": list(row.get("winner_mints") or []),
            "loser_mints": list(row.get("loser_mints") or []),
        },
        "fresh": None,
        "quality_score": e4_score(row),
        "selection_authority": "RECOGNISED_CREATOR_ONLY",
        "auto_buy": False,
    }


def fresh_record(creator: str, row: dict[str, Any], *, promoted: bool) -> dict[str, Any]:
    fills = int(row.get("v12_compatible_fills") or 0)
    wins = int(row.get("wins") or 0)
    losses = int(row.get("losses") or 0)
    return {
        "creator": creator,
        "source": "FRESH_APPRENTICESHIP",
        "tier": "PROMOTED" if promoted else "SHORTLIST",
        "status": "PROMOTED" if promoted else "SHORTLISTED",
        "e4": None,
        "fresh": {
            "launches_seen": int(row.get("launches_seen") or 0),
            "runner_2x_count": int(row.get("runner_2x_count") or 0),
            "runner_3x_count": int(row.get("runner_3x_count") or 0),
            "scout_fills": int(row.get("scout_fills") or 0),
            "scout_wins": int(row.get("scout_wins") or 0),
            "scout_losses": int(row.get("scout_losses") or 0),
            "scout_net_pnl_sol": finite(row.get("scout_net_pnl_sol")),
            "v12_compatible_fills": fills,
            "wins": wins,
            "losses": losses,
            "win_rate": wins / fills if fills else 0.0,
            "net_pnl_sol": finite(row.get("net_pnl_sol")),
            "profit_factor": finite(row.get("profit_factor")),
            "winning_mints": list(row.get("distinct_winning_mints") or []),
            "scout_winning_mints": list(row.get("distinct_scout_winning_mints") or []),
        },
        "quality_score": fresh_score(row),
        "selection_authority": "RECOGNISED_CREATOR_ONLY" if promoted else "RESEARCH_ONLY",
        "auto_buy": False,
    }


def merge_promoted(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    result = dict(existing)
    sources = set(str(result.get("source") or "").split("+"))
    sources.add(str(incoming.get("source") or ""))
    result["source"] = "+".join(sorted(value for value in sources if value))
    result["quality_score"] = max(
        finite(result.get("quality_score")), finite(incoming.get("quality_score"))
    )
    if incoming.get("e4"):
        result["e4"] = incoming["e4"]
    if incoming.get("fresh"):
        result["fresh"] = incoming["fresh"]
    tiers = {str(result.get("tier")), str(incoming.get("tier"))}
    result["tier"] = "ELITE" if "ELITE" in tiers else "PROMOTED"
    result["status"] = "PROMOTED"
    result["selection_authority"] = "RECOGNISED_CREATOR_ONLY"
    result["auto_buy"] = False
    return result


def runner_gap(apprentice: dict[str, Any]) -> dict[str, Any]:
    observations = list(apprentice.get("observations") or [])
    creators = dict(apprentice.get("creators") or {})
    runner_rows = [
        row for row in observations if bool((row.get("runner") or {}).get("runner_2x_60s"))
    ]
    by_creator: dict[str, list[dict[str, Any]]] = {}
    for row in runner_rows:
        by_creator.setdefault(str(row.get("creator") or ""), []).append(row)

    shortlisted_statuses = {
        "SHORTLISTED",
        "CONFIRMED_REPEAT_WINNER",
        "PROMOTION_CANDIDATE",
    }
    shortlisted = {
        creator
        for creator, row in creators.items()
        if str(row.get("status")) in shortlisted_statuses
    }
    not_shortlisted = set(by_creator) - shortlisted

    reasons = {
        "no_profitable_scout_fill": 0,
        "all_scout_attempts_unfilled_or_rejected": 0,
        "profitable_runner_creator_shortlisted": 0,
    }
    for creator, rows in by_creator.items():
        scout = [row.get("scout") or {} for row in rows]
        profitable = any(
            item.get("status") == "FILLED" and finite(item.get("pnl_sol")) > 0
            for item in scout
        )
        any_fill = any(item.get("status") == "FILLED" for item in scout)
        if creator in shortlisted:
            reasons["profitable_runner_creator_shortlisted"] += 1
        elif not profitable:
            reasons["no_profitable_scout_fill"] += 1
            if not any_fill:
                reasons["all_scout_attempts_unfilled_or_rejected"] += 1

    return {
        "runner_2x_coin_observations": len(runner_rows),
        "unique_runner_2x_creators": len(by_creator),
        "runner_creators_already_shortlisted_or_promoted": len(set(by_creator) & shortlisted),
        "runner_creators_not_shortlisted": len(not_shortlisted),
        "explanation": (
            "A 2x path is an outcome label, not proof V12 could profit from its causal entry. "
            "The apprenticeship only shortlists a creator when the hypothetical scout trade "
            "actually fills and closes profitably after timing, output, chase and costs."
        ),
        "reason_counts_by_creator": reasons,
    }


def build(
    expectancy: dict[str, Any],
    apprentice: dict[str, Any],
    complete_history: dict[str, Any] | None = None,
) -> dict[str, Any]:
    complete_rows = (
        complete_history.get("creators", [])
        if isinstance(complete_history, dict)
        else []
    )
    use_complete = bool(
        complete_rows
        and int((complete_history.get("completeness") or {}).get("union_trades") or 0) >= 316
    )
    source_rows = complete_rows if use_complete else expectancy.get("top_creators", [])
    e4_rows = []
    for raw in source_rows or []:
        if not isinstance(raw, dict) or not raw.get("creator"):
            continue
        row = dict(raw)
        if str(row.get("creator")) == "UNKNOWN_CREATOR":
            continue
        if use_complete and float(row.get("minimum_resolution_confidence") or 1.0) < 0.85:
            continue
        if "winning_pnl_sol" not in row:
            row["winning_pnl_sol"] = max(
                0.0,
                finite(row.get("net_pnl_sol_observed")),
            )
        e4_rows.append(row)
    fresh = {
        str(key): dict(value)
        for key, value in (apprentice.get("creators") or {}).items()
        if isinstance(value, dict)
    }

    promoted: dict[str, dict[str, Any]] = {}
    for row in e4_rows:
        if int(row.get("wins") or 0) >= 2:
            record = e4_record(row)
            promoted[record["creator"]] = record

    for creator, row in fresh.items():
        if bool(row.get("promotion_ready")):
            record = fresh_record(creator, row, promoted=True)
            promoted[creator] = (
                merge_promoted(promoted[creator], record)
                if creator in promoted
                else record
            )

    e4_shortlist = []
    for row in e4_rows:
        creator = str(row["creator"])
        if creator in promoted:
            continue
        wins = int(row.get("wins") or 0)
        losses = int(row.get("losses") or 0)
        pnl = finite(row.get("winning_pnl_sol"))
        if wins < 1 or losses > 1 or pnl < 0.10:
            continue
        score = e4_score(row)
        if score < 0.55:
            continue
        record = e4_record(row)
        record["tier"] = "SHORTLIST"
        record["status"] = "SHORTLISTED"
        record["selection_authority"] = "RESEARCH_ONLY"
        record["quality_score"] = score
        e4_shortlist.append(record)
    e4_shortlist.sort(key=lambda row: row["quality_score"], reverse=True)
    e4_shortlist = e4_shortlist[:E4_SHORTLIST_LIMIT]

    fresh_shortlist = []
    for creator, row in fresh.items():
        if creator in promoted:
            continue
        scout_wins = int(row.get("scout_wins") or 0)
        scout_net = finite(row.get("scout_net_pnl_sol"))
        compatible_wins = int(row.get("wins") or 0)
        runner2 = int(row.get("runner_2x_count") or 0)
        distinct_scout = len(row.get("distinct_scout_winning_mints") or [])
        if distinct_scout < 2:
            continue
        if scout_wins < 2 or scout_net <= 0.03:
            continue
        if compatible_wins < 1 and runner2 < 2:
            continue
        score = fresh_score(row)
        if score < 0.58:
            continue
        fresh_shortlist.append(fresh_record(creator, row, promoted=False))
    fresh_shortlist.sort(key=lambda row: row["quality_score"], reverse=True)
    fresh_shortlist = fresh_shortlist[:FRESH_SHORTLIST_LIMIT]

    shortlist: dict[str, dict[str, Any]] = {}
    for row in [*e4_shortlist, *fresh_shortlist]:
        creator = row["creator"]
        previous = shortlist.get(creator)
        if previous is None or row["quality_score"] > previous["quality_score"]:
            shortlist[creator] = row

    promoted_rows = sorted(
        promoted.values(),
        key=lambda row: (
            row["tier"] != "ELITE",
            -finite(row["quality_score"]),
            row["creator"],
        ),
    )
    shortlist_rows = sorted(
        shortlist.values(),
        key=lambda row: (-finite(row["quality_score"]), row["creator"]),
    )

    return {
        "schema_version": SCHEMA,
        "activation": {
            "mode": "POST_CAUSAL_100_ONLY",
            "enabled_now": False,
            "frozen_100_trade_model_modified": False,
        },
        "history_source": (
            "COMPLETE_ONCHAIN_UNION"
            if use_complete
            else "LEGACY_316_TRADE_CORPUS"
        ),
        "policy": {
            "e4_promote_rule": "verified E4 creator with >=2 historical wins",
            "fresh_promote_rule": "fresh apprenticeship promotion_ready=true",
            "shortlist_is_selection_authority": False,
            "promoted_is_auto_buy": False,
            "recognition_only": True,
        },
        "counts": {
            "e4_history_creators_analysed": len(e4_rows),
            "e4_repeat_winner_promotions": sum(
                int(row.get("wins") or 0) >= 2 for row in e4_rows
            ),
            "fresh_creators_analysed": len(fresh),
            "fresh_launches_analysed": int(
                (apprentice.get("counts") or {}).get("unknown_launch_observations") or 0
            ),
            "fresh_promotion_ready": sum(bool(row.get("promotion_ready")) for row in fresh.values()),
            "promoted_unique": len(promoted_rows),
            "shortlisted_unique": len(shortlist_rows),
            "elite_unique": sum(row["tier"] == "ELITE" for row in promoted_rows),
        },
        "runner_2x_gap": runner_gap(apprentice),
        "promoted": promoted_rows,
        "shortlisted": shortlist_rows,
    }


def render(library: dict[str, Any]) -> str:
    c = library["counts"]
    g = library["runner_2x_gap"]
    lines = [
        "# V12 canonical creator library",
        "",
        "**Activation:** post-causal-100 only. Promoted means recognised creator, not auto-buy.",
        "",
        "## Coverage",
        "",
        f"- E4 historical creators analysed: {c['e4_history_creators_analysed']}",
        f"- E4 repeat-winner promotions: {c['e4_repeat_winner_promotions']}",
        f"- Fresh creators analysed: {c['fresh_creators_analysed']}",
        f"- Fresh launches analysed: {c['fresh_launches_analysed']}",
        f"- Unique promoted creators: {c['promoted_unique']}",
        f"- Elite promoted creators: {c['elite_unique']}",
        f"- Cream shortlist: {c['shortlisted_unique']}",
        "",
        "## Why 948 2x coins did not become 948 shortlisted creators",
        "",
        f"- 2x coin observations: {g['runner_2x_coin_observations']}",
        f"- Unique creators behind those runners: {g['unique_runner_2x_creators']}",
        f"- Runner creators already shortlisted/promoted: {g['runner_creators_already_shortlisted_or_promoted']}",
        f"- Runner creators not shortlisted: {g['runner_creators_not_shortlisted']}",
        f"- No profitable causal scout fill: {g['reason_counts_by_creator']['no_profitable_scout_fill']}",
        f"- Of those, all scout attempts unfilled/rejected: {g['reason_counts_by_creator']['all_scout_attempts_unfilled_or_rejected']}",
        "",
        g["explanation"],
        "",
        "## Promoted",
        "",
    ]
    for row in library["promoted"]:
        e4 = row.get("e4") or {}
        fresh = row.get("fresh") or {}
        lines.append(
            f"- {row['creator']} | {row['tier']} | score={row['quality_score']:.3f} | "
            f"E4={e4.get('wins',0)}W/{e4.get('losses',0)}L | "
            f"fresh={fresh.get('wins',0)}W/{fresh.get('losses',0)}L"
        )
    lines += ["", "## Shortlisted", ""]
    for row in library["shortlisted"]:
        lines.append(
            f"- {row['creator']} | {row['source']} | score={row['quality_score']:.3f}"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--e4-expectancy", type=Path, required=True)
    parser.add_argument("--apprentice", type=Path, required=True)
    parser.add_argument(
        "--e4-complete-history",
        type=Path,
        default=Path("models/e4/e4-complete-creator-history.json"),
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    complete = load(args.e4_complete_history) if args.e4_complete_history.exists() else None
    library = build(
        load(args.e4_expectancy),
        load(args.apprentice),
        complete,
    )
    write(args.output, library)
    args.report.write_text(render(library), encoding="utf-8")
    print(json.dumps(library["counts"], sort_keys=True))
    print(json.dumps(library["runner_2x_gap"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
