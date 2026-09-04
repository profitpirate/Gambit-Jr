#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Mapping

E4_WALLET = "E4EzXdwf7NNdqM2XGswWaWHfxgucVCo24PTCcrimTKBz"
FOCUS_CREATOR = "4devFPRkWUTknomCHr1uMbfJLn111nKB3GDjH811JP4L"


def finite(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def load_history(path: Path) -> dict[str, dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    output = {}
    for row in data.get("top_creators") or []:
        creator = str(row.get("creator") or "")
        if not creator:
            continue
        wins = integer(row.get("wins"))
        losses = integer(row.get("losses"))
        trades = max(integer(row.get("trades")), wins + losses)
        rate = finite(row.get("gross_win_rate"), wins / trades if trades else 0.0)
        output[creator] = {
            "wins": wins,
            "losses": losses,
            "trades": trades,
            "win_rate": rate,
            "winning_pnl_sol": finite(row.get("winning_pnl_sol")),
        }
    return output


def band(profile: Mapping[str, Any]) -> list[str]:
    wins = integer(profile.get("wins"))
    trades = integer(profile.get("trades"))
    rate = finite(profile.get("win_rate"))
    output = []
    if trades >= 1 and wins >= 1:
        output.append("ANY_PRIOR_WIN")
    if trades >= 3 and wins >= 2 and rate >= 0.75:
        output.append("PROVEN_3T_75")
    if trades >= 5 and wins >= 4 and rate >= 0.80:
        output.append("ELITE_5T_80")
    if trades >= 8 and wins >= 7 and rate >= 0.85:
        output.append("ULTRA_8T_85")
    if trades >= 10 and wins >= 9 and rate >= 0.90:
        output.append("ULTRA_10T_90")
    if trades >= 3 and wins == trades:
        output.append("PURE_REPEAT")
    return output


def read_capture(events_path: Path, history: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    launches: dict[str, dict[str, Any]] = {}
    selected: set[str] = set()
    with events_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            mint = str(row.get("mint") or "")
            kind = str(row.get("kind") or "").upper()
            trader = str(row.get("trader") or "")
            raw = row.get("raw") if isinstance(row.get("raw"), Mapping) else {}
            if kind == "CREATE" and mint:
                creator = str(row.get("creator") or raw.get("creator") or trader or "")
                launches.setdefault(mint, {
                    "mint": mint,
                    "creator": creator,
                    "created_ns": integer(row.get("received_ns")),
                })
            if trader == E4_WALLET and kind in {"BUY", "PUMPSWAP_BUY"} and mint:
                selected.add(mint)
    rows = []
    for mint, launch in launches.items():
        creator = str(launch.get("creator") or "")
        profile = dict(history.get(creator) or {})
        rows.append({
            **launch,
            "e4_selected": mint in selected,
            "history": profile,
            "bands": band(profile),
        })
    return {"launches": rows, "e4_selected_mints": sorted(selected)}


def summarize(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    selected_total = sum(bool(row.get("e4_selected")) for row in rows)
    bands: dict[str, dict[str, Any]] = {}
    all_names = sorted({name for row in rows for name in row.get("bands") or []})
    for name in all_names:
        eligible = [row for row in rows if name in (row.get("bands") or [])]
        selected = [row for row in eligible if row.get("e4_selected")]
        bands[name] = {
            "launches": len(eligible),
            "e4_selected": len(selected),
            "selection_precision": len(selected) / len(eligible) if eligible else 0.0,
            "e4_entry_recall": len(selected) / selected_total if selected_total else 0.0,
            "unique_creators": len({str(row.get("creator") or "") for row in eligible}),
        }
    creators: dict[str, dict[str, Any]] = {}
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        creator = str(row.get("creator") or "")
        if creator in row.get("history", {}):
            pass
        if creator and row.get("history"):
            grouped[creator].append(row)
    for creator, launches in grouped.items():
        selected = sum(bool(row.get("e4_selected")) for row in launches)
        profile = dict(launches[0].get("history") or {})
        creators[creator] = {
            **profile,
            "captured_launches": len(launches),
            "captured_e4_selected": selected,
            "captured_selection_rate": selected / len(launches),
            "bands": band(profile),
            "mints": [
                {"mint": row.get("mint"), "e4_selected": bool(row.get("e4_selected"))}
                for row in launches
            ],
        }
    return {
        "launches": len(rows),
        "e4_selected": selected_total,
        "bands": bands,
        "whitelist_creator_launches": sum(bool(row.get("history")) for row in rows),
        "whitelist_creator_selected": sum(bool(row.get("history")) and bool(row.get("e4_selected")) for row in rows),
        "top_captured_whitelist_creators": dict(sorted(
            creators.items(),
            key=lambda item: (item[1]["captured_launches"], item[1]["trades"]),
            reverse=True,
        )[:50]),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit whitelist creator launches vs actual E4 selections")
    parser.add_argument("--events", action="append", default=[])
    parser.add_argument("--history", type=Path, default=Path("models/e4/e4-creator-expectancy.json"))
    parser.add_argument("--focus-creator", default=FOCUS_CREATOR)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    history = load_history(args.history)
    rows = []
    captures = []
    for raw_path in args.events:
        capture = read_capture(Path(raw_path), history)
        rows.extend(capture["launches"])
        captures.append({
            "events": raw_path,
            "launches": len(capture["launches"]),
            "e4_selected": len(capture["e4_selected_mints"]),
        })
    summary = summarize(rows)
    focus = [row for row in rows if str(row.get("creator") or "") == str(args.focus_creator)]
    payload = {
        "version": "e4-v12-whitelist-launch-audit-v1",
        "methodology": {
            "question": "If the whitelist had immediate launch authority, how often would it agree with E4?",
            "important": "History is the whitelist snapshot already present before these frozen captures; this does not retroactively justify the historical trades that created the whitelist.",
        },
        "captures": captures,
        "summary": summary,
        "focus_creator": {
            "creator": args.focus_creator,
            "history": history.get(str(args.focus_creator), {}),
            "captured_launches": len(focus),
            "captured_e4_selected": sum(bool(row.get("e4_selected")) for row in focus),
            "launches": [
                {"mint": row.get("mint"), "e4_selected": bool(row.get("e4_selected"))}
                for row in focus
            ],
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({
        "summary": {
            "launches": summary["launches"],
            "e4_selected": summary["e4_selected"],
            "bands": summary["bands"],
            "whitelist_creator_launches": summary["whitelist_creator_launches"],
            "whitelist_creator_selected": summary["whitelist_creator_selected"],
        },
        "focus_creator": payload["focus_creator"],
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
