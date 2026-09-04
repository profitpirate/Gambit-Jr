#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlparse

E4_WALLET = "E4EzXdwf7NNdqM2XGswWaWHfxgucVCo24PTCcrimTKBz"
DEFAULT_FOCUS_CREATOR = "4devFPRkWUTknomCHr1uMbfJLn111nKB3GDjH811JP4L"


def finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def host(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = urlparse(text)
        return (parsed.hostname or "").lower()
    except Exception:
        return ""


def creator_history(path: Path) -> dict[str, dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = list(data.get("top_creators") or [])
    output: dict[str, dict[str, Any]] = {}
    for row in rows:
        creator = str(row.get("creator") or "")
        if not creator:
            continue
        wins = integer(row.get("wins"))
        losses = integer(row.get("losses"))
        trades = max(integer(row.get("trades")), wins + losses)
        rate = finite(row.get("gross_win_rate"), wins / trades if trades else 0.0)
        if trades >= 5 and wins >= 4 and rate >= 0.80:
            tier = "ELITE"
        elif trades >= 3 and wins >= 2 and rate >= 0.75:
            tier = "PROVEN"
        elif trades >= 3 and rate <= 0.25:
            tier = "NEGATIVE"
        else:
            tier = "WATCH"
        output[creator] = {
            "wins": wins,
            "losses": losses,
            "trades": trades,
            "win_rate": rate,
            "winning_pnl_sol": finite(row.get("winning_pnl_sol")),
            "tier": tier,
        }
    return output


def outcomes(batch_path: Path) -> dict[str, dict[str, Any]]:
    batch = json.loads(batch_path.read_text(encoding="utf-8"))
    rows = list((batch.get("actual_e4_fresh_sample") or {}).get("positions") or [])
    return {
        str(row.get("mint") or ""): {
            "won": finite(row.get("pnl_sol")) > 0,
            "pnl_sol": finite(row.get("pnl_sol")),
            "entry_sol": finite(row.get("cost_sol")),
            "exit_time": finite(row.get("exit_time")),
        }
        for row in rows
        if str(row.get("mint") or "")
    }


def load_events(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            if row.get("mint") and row.get("received_ns"):
                rows.append(row)
    rows.sort(key=lambda row: (
        integer(row.get("received_ns")),
        integer(row.get("slot")),
        integer(row.get("event_index")),
    ))
    return rows


def new_state(row: Mapping[str, Any]) -> dict[str, Any]:
    raw = row.get("raw") if isinstance(row.get("raw"), Mapping) else {}
    creator = str(row.get("creator") or raw.get("creator") or row.get("trader") or "")
    fdv = finite(row.get("fdv_usd") or raw.get("usd_market_cap") or raw.get("fdv_usd"))
    price = finite(row.get("price_sol"))
    return {
        "mint": str(row.get("mint") or ""),
        "creator": creator,
        "created_ns": integer(row.get("received_ns")),
        "create_slot": integer(row.get("slot")),
        "last_ns": integer(row.get("received_ns")),
        "last_slot": integer(row.get("slot")),
        "fdv_usd": fdv,
        "initial_fdv_usd": fdv,
        "price_sol": price,
        "initial_price_sol": price,
        "creator_seed_sol": 0.0,
        "buy_count": 0,
        "sell_count": 0,
        "total_buy_sol": 0.0,
        "noncreator_buy_sol": 0.0,
        "buyers": set(),
        "first_buyers": [],
        "buy_slots": Counter(),
        "token_program": str(raw.get("token_program") or ""),
        "quote_mint": str(raw.get("quote_mint") or ""),
        "mayhem": bool(raw.get("is_mayhem_mode")),
        "cashback": bool(raw.get("is_cashback_enabled")),
        "uri_host": host(raw.get("uri")),
        "bonding_curve": str(raw.get("bonding_curve") or ""),
    }


def apply_event(state: dict[str, Any], row: Mapping[str, Any]) -> None:
    kind = str(row.get("kind") or "").upper()
    raw = row.get("raw") if isinstance(row.get("raw"), Mapping) else {}
    state["last_ns"] = integer(row.get("received_ns"), state["last_ns"])
    state["last_slot"] = integer(row.get("slot"), state["last_slot"])
    creator = str(row.get("creator") or raw.get("creator") or "")
    if creator and not state["creator"]:
        state["creator"] = creator
    fdv = finite(row.get("fdv_usd") or raw.get("usd_market_cap") or raw.get("fdv_usd"))
    price = finite(row.get("price_sol"))
    if fdv > 0:
        state["fdv_usd"] = fdv
    if price > 0:
        state["price_sol"] = price
    if kind == "CREATE":
        if raw.get("uri") and not state["uri_host"]:
            state["uri_host"] = host(raw.get("uri"))
        return
    if kind in {"SELL", "PUMPSWAP_SELL"}:
        state["sell_count"] += 1
        return
    if kind not in {"BUY", "PUMPSWAP_BUY"}:
        return
    trader = str(row.get("trader") or raw.get("user") or "")
    sol = max(0.0, finite(row.get("sol_amount")))
    state["buy_count"] += 1
    state["total_buy_sol"] += sol
    state["buy_slots"][integer(row.get("slot"))] += 1
    if trader and trader == state["creator"]:
        state["creator_seed_sol"] += sol
    elif trader:
        state["noncreator_buy_sol"] += sol
        state["buyers"].add(trader)
        if trader not in state["first_buyers"] and len(state["first_buyers"]) < 8:
            state["first_buyers"].append(trader)


def snapshot(
    state: Mapping[str, Any],
    now_ns: int,
    history: Mapping[str, Mapping[str, Any]],
    prior_e4_creators: Mapping[str, int],
    prior_e4_buyers: Mapping[str, int],
) -> dict[str, Any]:
    creator = str(state.get("creator") or "")
    h = dict(history.get(creator) or {})
    price0 = finite(state.get("initial_price_sol"))
    price = finite(state.get("price_sol"))
    price_multiple = price / price0 if price0 > 0 and price > 0 else 1.0
    first_buyers = list(state.get("first_buyers") or [])
    prior_buyer_hits = sum(1 for buyer in first_buyers if prior_e4_buyers.get(buyer, 0) > 0)
    prior_buyer_weight = sum(integer(prior_e4_buyers.get(buyer, 0)) for buyer in first_buyers)
    last_slot = integer(state.get("last_slot"))
    return {
        "mint": str(state.get("mint") or ""),
        "creator": creator,
        "age_ms": max(0.0, (now_ns - integer(state.get("created_ns"))) / 1e6),
        "staleness_ms": max(0.0, (now_ns - integer(state.get("last_ns"))) / 1e6),
        "fdv_usd": finite(state.get("fdv_usd")),
        "creator_seed_sol": finite(state.get("creator_seed_sol")),
        "buy_count": integer(state.get("buy_count")),
        "sell_count": integer(state.get("sell_count")),
        "unique_noncreator_buyers": len(state.get("buyers") or ()),
        "noncreator_buy_sol": finite(state.get("noncreator_buy_sol")),
        "total_buy_sol": finite(state.get("total_buy_sol")),
        "price_multiple": price_multiple,
        "same_slot_buys": integer((state.get("buy_slots") or {}).get(last_slot, 0)),
        "slots_since_create": max(0, last_slot - integer(state.get("create_slot"))),
        "whitelist_wins": integer(h.get("wins")),
        "whitelist_losses": integer(h.get("losses")),
        "whitelist_trades": integer(h.get("trades")),
        "whitelist_win_rate": finite(h.get("win_rate")),
        "whitelist_pnl_sol": finite(h.get("winning_pnl_sol")),
        "whitelist_tier": str(h.get("tier") or "NONE"),
        "whitelist_any": bool(h),
        "whitelist_elite": str(h.get("tier") or "") == "ELITE",
        "prior_e4_creator_selections": integer(prior_e4_creators.get(creator, 0)),
        "prior_e4_buyer_hits": prior_buyer_hits,
        "prior_e4_buyer_weight": prior_buyer_weight,
        "first_buyers": first_buyers,
        "uri_host": str(state.get("uri_host") or ""),
        "token_program": str(state.get("token_program") or ""),
        "mayhem": bool(state.get("mayhem")),
        "cashback": bool(state.get("cashback")),
    }


def log_ratio(a: float, b: float, floor: float = 1e-9) -> float:
    return abs(math.log(max(floor, a) / max(floor, b)))


def distance(a: Mapping[str, Any], b: Mapping[str, Any]) -> float:
    # Match on the obvious public launch geometry. Identity/history variables are
    # deliberately excluded: they are the candidate discriminators we want to
    # measure after constructing near-twin controls.
    return (
        1.8 * log_ratio(finite(a.get("fdv_usd")), finite(b.get("fdv_usd")), 100.0)
        + 1.3 * abs(finite(a.get("creator_seed_sol")) - finite(b.get("creator_seed_sol"))) / 3.0
        + 0.9 * abs(integer(a.get("buy_count")) - integer(b.get("buy_count"))) / 4.0
        + 0.9 * abs(integer(a.get("unique_noncreator_buyers")) - integer(b.get("unique_noncreator_buyers"))) / 4.0
        + 0.8 * abs(finite(a.get("noncreator_buy_sol")) - finite(b.get("noncreator_buy_sol"))) / 5.0
        + 0.6 * abs(finite(a.get("price_multiple")) - finite(b.get("price_multiple"))) / 0.5
        + 0.8 * abs(finite(a.get("age_ms")) - finite(b.get("age_ms"))) / 250.0
        + 0.4 * abs(integer(a.get("same_slot_buys")) - integer(b.get("same_slot_buys"))) / 3.0
        + 1.5 * abs(integer(a.get("sell_count")) - integer(b.get("sell_count")))
    )


def mean(rows: Iterable[Mapping[str, Any]], key: str) -> float:
    values = [finite(row.get(key)) for row in rows]
    return sum(values) / len(values) if values else 0.0


def rate(rows: Iterable[Mapping[str, Any]], key: str) -> float:
    rows = list(rows)
    return sum(bool(row.get(key)) for row in rows) / len(rows) if rows else 0.0


def aggregate(selected: list[dict[str, Any]], controls: list[dict[str, Any]]) -> dict[str, Any]:
    numeric = (
        "whitelist_wins", "whitelist_losses", "whitelist_trades", "whitelist_win_rate",
        "whitelist_pnl_sol", "prior_e4_creator_selections", "prior_e4_buyer_hits",
        "prior_e4_buyer_weight", "fdv_usd", "creator_seed_sol", "buy_count",
        "unique_noncreator_buyers", "noncreator_buy_sol", "total_buy_sol", "age_ms",
        "price_multiple", "same_slot_buys", "slots_since_create",
    )
    differences = []
    for key in numeric:
        s = mean(selected, key)
        c = mean(controls, key)
        scale = max(abs(s), abs(c), 1e-9)
        differences.append({
            "feature": key,
            "selected_mean": s,
            "control_mean": c,
            "relative_difference": (s - c) / scale,
        })
    differences.sort(key=lambda row: abs(row["relative_difference"]), reverse=True)
    return {
        "selected": len(selected),
        "matched_control_rows": len(controls),
        "whitelist_any_rate": {
            "selected": rate(selected, "whitelist_any"),
            "controls": rate(controls, "whitelist_any"),
        },
        "whitelist_elite_rate": {
            "selected": rate(selected, "whitelist_elite"),
            "controls": rate(controls, "whitelist_elite"),
        },
        "prior_e4_creator_rate": {
            "selected": sum(integer(row.get("prior_e4_creator_selections")) > 0 for row in selected) / len(selected) if selected else 0.0,
            "controls": sum(integer(row.get("prior_e4_creator_selections")) > 0 for row in controls) / len(controls) if controls else 0.0,
        },
        "prior_e4_buyer_overlap_rate": {
            "selected": sum(integer(row.get("prior_e4_buyer_hits")) > 0 for row in selected) / len(selected) if selected else 0.0,
            "controls": sum(integer(row.get("prior_e4_buyer_hits")) > 0 for row in controls) / len(controls) if controls else 0.0,
        },
        "numeric_differences": differences,
        "selected_whitelist_tiers": dict(Counter(str(row.get("whitelist_tier") or "NONE") for row in selected)),
        "control_whitelist_tiers": dict(Counter(str(row.get("whitelist_tier") or "NONE") for row in controls)),
        "selected_uri_hosts": dict(Counter(str(row.get("uri_host") or "") for row in selected).most_common(15)),
        "control_uri_hosts": dict(Counter(str(row.get("uri_host") or "") for row in controls).most_common(15)),
    }


def analyse_pair(
    batch_path: Path,
    events_path: Path,
    history: Mapping[str, Mapping[str, Any]],
    prior_e4_creators: dict[str, int],
    prior_e4_buyers: dict[str, int],
    controls_per_trade: int,
    focus_creator: str,
) -> dict[str, Any]:
    event_rows = load_events(events_path)
    result_outcomes = outcomes(batch_path)
    selected_mints = {
        str(row.get("mint") or "")
        for row in event_rows
        if str(row.get("trader") or "") == E4_WALLET
        and str(row.get("kind") or "").upper() in {"BUY", "PUMPSWAP_BUY"}
    }
    creators_for_mint: dict[str, str] = {}
    all_launches_by_creator: Counter[str] = Counter()
    for row in event_rows:
        if str(row.get("kind") or "").upper() == "CREATE":
            raw = row.get("raw") if isinstance(row.get("raw"), Mapping) else {}
            creator = str(row.get("creator") or raw.get("creator") or row.get("trader") or "")
            mint = str(row.get("mint") or "")
            creators_for_mint[mint] = creator
            if creator:
                all_launches_by_creator[creator] += 1

    states: dict[str, dict[str, Any]] = {}
    selected_rows: list[dict[str, Any]] = []
    control_rows: list[dict[str, Any]] = []
    matches: list[dict[str, Any]] = []

    for row in event_rows:
        mint = str(row.get("mint") or "")
        kind = str(row.get("kind") or "").upper()
        trader = str(row.get("trader") or "")
        now_ns = integer(row.get("received_ns"))
        if kind == "CREATE" and mint not in states:
            states[mint] = new_state(row)
            continue
        state = states.get(mint)
        if state is None:
            state = new_state(row)
            states[mint] = state

        is_e4_buy = trader == E4_WALLET and kind in {"BUY", "PUMPSWAP_BUY"}
        if is_e4_buy:
            selected = snapshot(state, now_ns, history, prior_e4_creators, prior_e4_buyers)
            selected["e4_won"] = bool((result_outcomes.get(mint) or {}).get("won"))
            selected["e4_pnl_sol"] = finite((result_outcomes.get(mint) or {}).get("pnl_sol"))
            selected["e4_entry_sol"] = max(0.0, finite(row.get("sol_amount")) or finite((result_outcomes.get(mint) or {}).get("entry_sol")))
            selected["timestamp_ns"] = now_ns
            selected["slot"] = integer(row.get("slot"))
            selected["creator_launches_in_batch"] = all_launches_by_creator.get(selected["creator"], 0)
            selected["creator_ignored_launches_in_batch"] = max(
                0,
                sum(
                    1 for other_mint, creator in creators_for_mint.items()
                    if creator == selected["creator"] and other_mint not in selected_mints
                ),
            )

            candidates: list[tuple[float, dict[str, Any]]] = []
            for other_mint, other_state in states.items():
                if other_mint == mint or other_mint in selected_mints:
                    continue
                created = integer(other_state.get("created_ns"))
                if created <= 0 or created > now_ns:
                    continue
                other = snapshot(other_state, now_ns, history, prior_e4_creators, prior_e4_buyers)
                # A control should represent a genuinely contemporaneous launch,
                # not a token that has been dead for minutes but still exists in
                # the state dictionary.
                if other["age_ms"] > 1_500.0 or other["staleness_ms"] > 750.0:
                    continue
                if other["fdv_usd"] <= 0:
                    continue
                candidates.append((distance(selected, other), other))
            candidates.sort(key=lambda pair: pair[0])
            nearest = []
            for dist, control in candidates[:controls_per_trade]:
                annotated = dict(control)
                annotated["distance"] = dist
                annotated["matched_to"] = mint
                nearest.append(annotated)
                control_rows.append(annotated)

            selected_rows.append(selected)
            matches.append({
                "selected": selected,
                "nearest_ignored": nearest,
            })

            creator = selected["creator"]
            if creator:
                prior_e4_creators[creator] = prior_e4_creators.get(creator, 0) + 1
            for buyer in selected.get("first_buyers") or ():
                prior_e4_buyers[buyer] = prior_e4_buyers.get(buyer, 0) + 1
            # E4's own buy is not applied to the pre-impact market state. This
            # avoids allowing its price impact to leak into the selected snapshot.
            continue

        apply_event(state, row)

    focus = []
    for mint, creator in creators_for_mint.items():
        if creator == focus_creator:
            focus.append({
                "mint": mint,
                "selected_by_e4": mint in selected_mints,
            })
    return {
        "batch": batch_path.name,
        "selected_count": len(selected_rows),
        "selected": selected_rows,
        "controls": control_rows,
        "matches": matches,
        "aggregate": aggregate(selected_rows, control_rows),
        "focus_creator": {
            "creator": focus_creator,
            "launches_in_capture": focus,
            "launch_count": len(focus),
            "selected_count": sum(bool(row["selected_by_e4"]) for row in focus),
        },
    }


def combine(reports: list[Mapping[str, Any]], focus_creator: str) -> dict[str, Any]:
    selected = [row for report in reports for row in report.get("selected", [])]
    controls = [row for report in reports for row in report.get("controls", [])]
    focus = [row for report in reports for row in (report.get("focus_creator") or {}).get("launches_in_capture", [])]
    return {
        "version": "e4-v12-exact-timestamp-matched-controls-v1",
        "methodology": {
            "causal_snapshot": True,
            "selected_snapshot": "state immediately before each observed E4 BUY",
            "ignored_snapshot": "latest state at the exact same timestamp",
            "matching_excludes_identity_history": True,
            "matching_features": [
                "fdv", "creator seed", "buy count", "unique buyers", "noncreator SOL",
                "price multiple", "age", "same-slot buys", "sell count",
            ],
            "purpose": "hold obvious launch geometry constant, then measure identity/history/topology differences",
        },
        "selected_count": len(selected),
        "aggregate": aggregate(selected, controls),
        "focus_creator": {
            "creator": focus_creator,
            "launches_in_captures": focus,
            "launch_count": len(focus),
            "selected_count": sum(bool(row["selected_by_e4"]) for row in focus),
        },
        "batches": reports,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Exact-timestamp E4 selected-vs-ignored matched-control analysis")
    parser.add_argument("--pair", action="append", default=[], metavar="BATCH:EVENTS")
    parser.add_argument("--creator-history", type=Path, default=Path("models/e4/e4-creator-expectancy.json"))
    parser.add_argument("--controls-per-trade", type=int, default=25)
    parser.add_argument("--focus-creator", default=DEFAULT_FOCUS_CREATOR)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    history = creator_history(args.creator_history)
    prior_e4_creators: dict[str, int] = {}
    prior_e4_buyers: dict[str, int] = {}
    reports = []
    for item in args.pair:
        batch, events = item.split(":", 1)
        reports.append(analyse_pair(
            Path(batch), Path(events), history, prior_e4_creators, prior_e4_buyers,
            max(1, args.controls_per_trade), str(args.focus_creator),
        ))
    payload = combine(reports, str(args.focus_creator))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    summary = {
        "selected_count": payload["selected_count"],
        "whitelist_any": payload["aggregate"]["whitelist_any_rate"],
        "whitelist_elite": payload["aggregate"]["whitelist_elite_rate"],
        "prior_e4_creator": payload["aggregate"]["prior_e4_creator_rate"],
        "prior_e4_buyer_overlap": payload["aggregate"]["prior_e4_buyer_overlap_rate"],
        "top_numeric_differences": payload["aggregate"]["numeric_differences"][:10],
        "focus_creator": payload["focus_creator"],
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
