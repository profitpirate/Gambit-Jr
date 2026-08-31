#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

E4_WALLET = "E4EzXdwf7NNdqM2XGswWaWHfxgucVCo24PTCcrimTKBz"


def finite(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def percentile_rank(values: list[float], value: float | None) -> float | None:
    if value is None or not values:
        return None
    return sum(item <= value for item in values) / len(values)


def host(uri: str | None) -> str:
    if not uri:
        return "unknown"
    parsed = urlparse(uri)
    return (parsed.netloc or parsed.path.split("/", 1)[0] or "unknown").lower()


def load_events(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict) and event.get("mint"):
                events.append(event)
    events.sort(
        key=lambda row: (
            int(row.get("received_ns") or 0),
            int(row.get("slot") or 0),
            int(row.get("event_index") or 0),
        )
    )
    return events


def initial_creator_buy(timeline: list[dict[str, Any]], creator: str | None) -> float:
    if not creator:
        return 0.0
    return sum(
        finite(row.get("sol_amount")) or 0.0
        for row in timeline
        if row.get("kind") == "BUY" and row.get("trader") == creator
    )


def launch_record(timeline: list[dict[str, Any]]) -> dict[str, Any] | None:
    create = next((row for row in timeline if row.get("kind") == "CREATE"), None)
    if not create:
        return None
    raw = create.get("raw") if isinstance(create.get("raw"), dict) else {}
    creator = str(create.get("creator") or raw.get("creator") or raw.get("user") or "") or None
    first_1000ms = [
        row
        for row in timeline
        if row.get("kind") in {"BUY", "SELL"}
        and int(row.get("received_ns") or 0) - int(create.get("received_ns") or 0)
        <= 1_000_000_000
    ]
    buys = [row for row in first_1000ms if row.get("kind") == "BUY"]
    return {
        "mint": create["mint"],
        "creator": creator,
        "uri": raw.get("uri"),
        "metadata_host": host(raw.get("uri")),
        "name": raw.get("name"),
        "symbol": raw.get("symbol"),
        "create_ns": int(create.get("received_ns") or 0),
        "create_slot": int(create.get("slot") or 0),
        "creator_initial_buy_sol": initial_creator_buy(first_1000ms, creator),
        "first_second_buy_sol": sum(finite(row.get("sol_amount")) or 0.0 for row in buys),
        "first_second_buyers": len({row.get("trader") for row in buys if row.get("trader")}),
        "first_second_buy_count": len(buys),
        "create_signature": create.get("signature"),
    }


def selected_record(
    timeline: list[dict[str, Any]],
    launch: dict[str, Any],
    wallet: str,
    creator_frequency: Counter[str],
) -> dict[str, Any] | None:
    entry = next(
        (
            row
            for row in timeline
            if row.get("kind") == "BUY" and row.get("trader") == wallet
        ),
        None,
    )
    if not entry:
        return None
    create_ns = launch["create_ns"]
    entry_ns = int(entry.get("received_ns") or 0)
    before = [
        row
        for row in timeline
        if row.get("kind") in {"BUY", "SELL"}
        and int(row.get("received_ns") or 0) <= entry_ns
        and row is not entry
    ]
    buys = [row for row in before if row.get("kind") == "BUY"]
    sells = [row for row in before if row.get("kind") == "SELL"]
    non_creator = [
        row
        for row in buys
        if row.get("trader") not in {launch.get("creator"), wallet}
    ]
    signatures = Counter(str(row.get("signature") or "") for row in buys if row.get("signature"))
    entry_delay_ms = (entry_ns - create_ns) / 1_000_000
    metadata = launch["metadata_host"]
    public_flow = len({row.get("trader") for row in non_creator if row.get("trader")})
    route_hypothesis = "mixed_or_unknown"
    if entry_delay_ms <= 60 and public_flow <= 1:
        route_hypothesis = "private_launch_intent_or_prearmed_sniper"
    elif public_flow >= 3 or sum(finite(row.get("sol_amount")) or 0 for row in non_creator) >= 8:
        route_hypothesis = "public_capital_flow_possible"
    if metadata == "metadata.j7tracker.io" and entry_delay_ms <= 100:
        route_hypothesis = "j7_or_similar_prearmed_sniper_strongly_consistent"

    return {
        **launch,
        "entry_ns": entry_ns,
        "entry_slot": int(entry.get("slot") or 0),
        "entry_signature": entry.get("signature"),
        "entry_sol": finite(entry.get("sol_amount")) or 0.0,
        "entry_fdv_usd": finite(entry.get("fdv_usd")),
        "launch_to_entry_ms": entry_delay_ms,
        "same_slot_as_create": int(entry.get("slot") or 0) == launch["create_slot"],
        "same_transaction_as_create": entry.get("signature") == launch["create_signature"],
        "pre_entry_buy_sol": sum(finite(row.get("sol_amount")) or 0.0 for row in buys),
        "pre_entry_noncreator_buy_sol": sum(
            finite(row.get("sol_amount")) or 0.0 for row in non_creator
        ),
        "pre_entry_unique_buyers": len({row.get("trader") for row in buys if row.get("trader")}),
        "pre_entry_noncreator_buyers": public_flow,
        "pre_entry_sells": len(sells),
        "pre_entry_unique_signatures": len(signatures),
        "pre_entry_max_buys_same_signature": max(signatures.values(), default=0),
        "creator_launches_in_capture": creator_frequency.get(launch.get("creator") or "", 0),
        "route_hypothesis": route_hypothesis,
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# E4 live selection forensics",
        "",
        f"- Real launches: **{report['launches']}**",
        f"- Real trade events: **{report['trade_events']}**",
        f"- E4 entries observed: **{report['e4_entries']}**",
        "",
        "## E4-selected launches",
        "",
        "| Mint | Metadata | Creator buy | Public buyers before E4 | Public SOL before E4 | Entry delay | E4 size | Hypothesis |",
        "|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in report["selected"]:
        lines.append(
            "| {mint} | {metadata_host} | {creator_initial_buy_sol:.4f} | "
            "{pre_entry_noncreator_buyers} | {pre_entry_noncreator_buy_sol:.4f} | "
            "{launch_to_entry_ms:.2f}ms | {entry_sol:.4f} | {route_hypothesis} |".format(
                **{**row, "mint": row["mint"][:10] + "…"}
            )
        )
    lines.extend(
        [
            "",
            "## Metadata source comparison",
            "",
            "| Host | All launches | Share | E4 selections | Selection share | Enrichment |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for row in report["metadata_enrichment"]:
        lines.append(
            f"| {row['host']} | {row['all_count']} | {row['all_share']:.1%} | "
            f"{row['selected_count']} | {row['selected_share']:.1%} | "
            f"{row['enrichment']:.2f}x |"
        )
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            *[f"- {finding}" for finding in report["findings"]],
            "",
            "This is observational forensics. Enrichment and timing establish consistency, not proof of a private relationship or causal strategy.",
        ]
    )
    return "\n".join(lines) + "\n"


def analyse(events: list[dict[str, Any]], wallet: str) -> dict[str, Any]:
    by_mint: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        by_mint[str(event["mint"])].append(event)

    launches = [record for timeline in by_mint.values() if (record := launch_record(timeline))]
    creator_frequency = Counter(
        str(row["creator"]) for row in launches if row.get("creator")
    )
    launch_by_mint = {row["mint"]: row for row in launches}
    selected = []
    for mint, timeline in by_mint.items():
        launch = launch_by_mint.get(mint)
        if not launch:
            continue
        row = selected_record(timeline, launch, wallet, creator_frequency)
        if row:
            selected.append(row)
    selected.sort(key=lambda row: row["entry_ns"])

    all_hosts = Counter(row["metadata_host"] for row in launches)
    selected_hosts = Counter(row["metadata_host"] for row in selected)
    enrichment = []
    for source, count in all_hosts.most_common():
        all_share = count / len(launches) if launches else 0.0
        selected_count = selected_hosts[source]
        selected_share = selected_count / len(selected) if selected else 0.0
        enrichment.append(
            {
                "host": source,
                "all_count": count,
                "all_share": all_share,
                "selected_count": selected_count,
                "selected_share": selected_share,
                "enrichment": selected_share / all_share if all_share else 0.0,
            }
        )

    creator_buys = [row["creator_initial_buy_sol"] for row in launches]
    first_second_sol = [row["first_second_buy_sol"] for row in launches]
    for row in selected:
        row["creator_buy_percentile"] = percentile_rank(
            creator_buys, row["creator_initial_buy_sol"]
        )
        row["first_second_sol_percentile"] = percentile_rank(
            first_second_sol, row["pre_entry_buy_sol"]
        )

    very_fast = [row for row in selected if row["launch_to_entry_ms"] <= 60]
    low_public_flow = [row for row in very_fast if row["pre_entry_noncreator_buyers"] <= 1]
    j7 = [row for row in selected if row["metadata_host"] == "metadata.j7tracker.io"]
    repeated_creators = [row for row in selected if row["creator_launches_in_capture"] > 1]

    findings: list[str] = []
    if selected:
        findings.append(
            f"{len(very_fast)}/{len(selected)} E4 entries arrived within 60ms of the observed create receipt."
        )
        findings.append(
            f"{len(low_public_flow)}/{len(selected)} were both sub-60ms and had no more than one non-creator buyer visible before E4; public flow alone cannot explain those entries."
        )
        findings.append(
            f"{len(j7)}/{len(selected)} used metadata.j7tracker.io versus {all_hosts['metadata.j7tracker.io']}/{len(launches)} launches in the captured universe."
        )
        findings.append(
            f"{len(repeated_creators)}/{len(selected)} came from creators that launched more than once in this capture; no strong repeat-developer rule is established by this window."
        )
    findings.append(
        "The strongest current hypothesis is a pre-launch or launch-intent feed (for example a deployer-configured sniper wallet) combined with more than one selection route, not a universal public microburst rule."
    )

    return {
        "report_version": "e4-selection-forensics-v1",
        "events": len(events),
        "trade_events": sum(row.get("kind") in {"BUY", "SELL"} for row in events),
        "launches": len(launches),
        "e4_entries": len(selected),
        "selected": selected,
        "metadata_enrichment": enrichment,
        "creator_frequency": dict(creator_frequency.most_common()),
        "findings": findings,
    }


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        description="Forensic comparison of actual E4 entries against a real Pump launch capture"
    )
    value.add_argument("--events", type=Path, required=True)
    value.add_argument("--wallet", default=E4_WALLET)
    value.add_argument("--output", type=Path, required=True)
    return value


def main() -> int:
    args = parser().parse_args()
    report = analyse(load_events(args.events), args.wallet)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    args.output.with_suffix(".md").write_text(render_markdown(report), encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(args.output),
                "launches": report["launches"],
                "e4_entries": report["e4_entries"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
