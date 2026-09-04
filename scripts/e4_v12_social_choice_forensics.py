#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import math
import re
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

import aiohttp

E4_WALLET = "E4EzXdwf7NNdqM2XGswWaWHfxgucVCo24PTCcrimTKBz"
TWITTER_EPOCH_MS = 1_288_834_974_657
STATUS_RE = re.compile(r"/(?:status|statuses)/(\d+)")


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


def median(values: list[float]) -> float | None:
    clean = [float(value) for value in values if math.isfinite(float(value))]
    return statistics.median(clean) if clean else None


def percentile(values: list[float], fraction: float) -> float | None:
    clean = sorted(float(value) for value in values if math.isfinite(float(value)))
    if not clean:
        return None
    index = min(len(clean) - 1, max(0, int(round((len(clean) - 1) * fraction))))
    return clean[index]


def twitter_parts(value: Any) -> tuple[str, str, int | None]:
    url = str(value or "").strip()
    try:
        parsed = urlparse(url)
    except Exception:
        return "", "", None
    host = parsed.netloc.lower().removeprefix("www.").removeprefix("mobile.")
    if host not in {"x.com", "twitter.com"}:
        return "", "", None
    parts = [part for part in parsed.path.split("/") if part]
    handle = ""
    if parts and parts[0].lower() not in {"i", "search", "intent", "home", "hashtag"}:
        handle = parts[0].lower().lstrip("@")
    match = STATUS_RE.search(parsed.path)
    status_id = int(match.group(1)) if match else None
    return url, handle, status_id


def tweet_time_ms(status_id: int | None) -> int | None:
    return ((int(status_id) >> 22) + TWITTER_EPOCH_MS) if status_id else None


def social_value(payload: Any, names: set[str]) -> str:
    found = ""

    def walk(value: Any) -> None:
        nonlocal found
        if found:
            return
        if isinstance(value, Mapping):
            for key, child in value.items():
                if str(key).lower() in names and isinstance(child, str) and child.strip():
                    found = child.strip()
                    return
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(payload)
    return found


def known_sources(path: Path) -> dict[str, float]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    output = {}
    for handle, row in (data.get("handles") or {}).items():
        output[str(handle).lower()] = finite((row or {}).get("authority"))
    return output


def load_capture(batch_path: Path, events_path: Path, run_index: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    batch = json.loads(batch_path.read_text(encoding="utf-8"))
    outcomes = {
        str(row.get("mint") or ""): {
            "pnl_sol": finite(row.get("pnl_sol")),
            "won": finite(row.get("pnl_sol")) > 0,
        }
        for row in (batch.get("actual_e4_fresh_sample") or {}).get("positions") or []
        if str(row.get("mint") or "")
    }
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    all_rows: list[dict[str, Any]] = []
    with events_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            mint = str(row.get("mint") or "")
            if mint:
                grouped[mint].append(row)
                all_rows.append(row)
    all_rows.sort(key=lambda row: (integer(row.get("received_ns")), integer(row.get("slot")), integer(row.get("event_index"))))

    launches = []
    for mint, rows in grouped.items():
        rows.sort(key=lambda row: (integer(row.get("received_ns")), integer(row.get("slot")), integer(row.get("event_index"))))
        create = next((row for row in rows if str(row.get("kind") or "").upper() == "CREATE"), None)
        if create is None:
            continue
        raw = create.get("raw") if isinstance(create.get("raw"), Mapping) else {}
        creator = str(create.get("creator") or raw.get("creator") or create.get("trader") or "")
        e4_buy = next(
            (
                row for row in rows
                if str(row.get("trader") or "") == E4_WALLET
                and str(row.get("kind") or "").upper() in {"BUY", "PUMPSWAP_BUY"}
            ),
            None,
        )
        cutoff = integer(e4_buy.get("received_ns")) if e4_buy else 2**63 - 1
        buyers = []
        unique_outside = []
        seed = 0.0
        outside_sol = 0.0
        sells = 0
        same_slot_buys = 0
        last = create
        for row in rows:
            if integer(row.get("received_ns")) > cutoff:
                break
            kind = str(row.get("kind") or "").upper()
            trader = str(row.get("trader") or "")
            if trader == E4_WALLET:
                continue
            if kind in {"SELL", "PUMPSWAP_SELL"}:
                sells += 1
                continue
            if kind not in {"BUY", "PUMPSWAP_BUY"}:
                continue
            last = row
            buyers.append(row)
            if integer(row.get("slot")) == integer(create.get("slot")):
                same_slot_buys += 1
            amount = max(0.0, finite(row.get("sol_amount")))
            if trader == creator:
                seed += amount
            elif trader:
                outside_sol += amount
                if trader not in unique_outside:
                    unique_outside.append(trader)
        outcome = outcomes.get(mint) or {}
        launches.append(
            {
                "run_index": run_index,
                "run": batch_path.parent.parent.name,
                "mint": mint,
                "creator": creator,
                "create_ns": integer(create.get("received_ns")),
                "create_slot": integer(create.get("slot")),
                "name": str(raw.get("name") or ""),
                "symbol": str(raw.get("symbol") or ""),
                "uri": str(raw.get("uri") or ""),
                "metadata_host": (urlparse(str(raw.get("uri") or "")).netloc or "").lower().removeprefix("www."),
                "token_program": str(raw.get("token_program") or ""),
                "mayhem": bool(raw.get("is_mayhem_mode")),
                "cashback": bool(raw.get("is_cashback_enabled")),
                "selected": e4_buy is not None,
                "e4_entry_ns": integer(e4_buy.get("received_ns")) if e4_buy else 0,
                "e4_entry_sol": finite(e4_buy.get("sol_amount")) if e4_buy else 0.0,
                "e4_won": bool(outcome.get("won")),
                "e4_pnl_sol": finite(outcome.get("pnl_sol")),
                "buy_count": len(buyers),
                "sell_count": sells,
                "same_slot_buys": same_slot_buys,
                "creator_seed_sol": seed,
                "outside_sol": outside_sol,
                "unique_outside_buyers": len(unique_outside),
                "first_buyers": unique_outside[:8],
                "first_three_cluster": tuple(unique_outside[:3]),
                "fdv_usd": finite(last.get("fdv_usd")),
                "last_event_ns": integer(last.get("received_ns")),
                "age_ms": max(0.0, (integer(last.get("received_ns")) - integer(create.get("received_ns"))) / 1e6),
            }
        )
    return launches, all_rows


def numeric_distance(left: Mapping[str, Any], right: Mapping[str, Any]) -> float:
    def logdiff(key: str, scale: float = 1.0) -> float:
        return abs(math.log1p(max(0.0, finite(left.get(key)))) - math.log1p(max(0.0, finite(right.get(key))))) * scale
    return (
        logdiff("fdv_usd", 2.0)
        + logdiff("creator_seed_sol", 2.0)
        + logdiff("outside_sol", 2.0)
        + abs(integer(left.get("buy_count")) - integer(right.get("buy_count"))) * 0.65
        + abs(integer(left.get("unique_outside_buyers")) - integer(right.get("unique_outside_buyers"))) * 0.55
        + abs(integer(left.get("same_slot_buys")) - integer(right.get("same_slot_buys"))) * 0.35
    )


def target_launches(all_launches: list[dict[str, Any]], contemporaneous: int) -> tuple[dict[str, dict[str, Any]], dict[str, set[str]]]:
    selected = [row for row in all_launches if row["selected"]]
    selected_creators = {row["creator"] for row in selected if row["creator"]}
    selected_clusters = Counter(row["first_three_cluster"] for row in selected if len(row["first_three_cluster"]) == 3)
    targets: dict[str, dict[str, Any]] = {}
    cohorts: dict[str, set[str]] = defaultdict(set)

    for row in all_launches:
        if row["selected"]:
            targets[row["mint"]] = row
            cohorts["selected"].add(row["mint"])
        if row["creator"] in selected_creators:
            targets[row["mint"]] = row
            cohorts["selected_creator_population"].add(row["mint"])
            if not row["selected"]:
                cohorts["same_creator_ignored"].add(row["mint"])
        cluster = row["first_three_cluster"]
        if len(cluster) == 3 and selected_clusters.get(cluster, 0) > 0:
            targets[row["mint"]] = row
            cohorts["selected_buyer_cluster_population"].add(row["mint"])
            if not row["selected"]:
                cohorts["same_buyer_cluster_ignored"].add(row["mint"])

    by_run: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in all_launches:
        by_run[row["run_index"]].append(row)
    for entry in selected:
        candidates = []
        for row in by_run[entry["run_index"]]:
            if row["selected"] or row["mint"] == entry["mint"]:
                continue
            if row["create_ns"] > entry["e4_entry_ns"]:
                continue
            age_at_choice = (entry["e4_entry_ns"] - row["create_ns"]) / 1e6
            staleness = (entry["e4_entry_ns"] - row["last_event_ns"]) / 1e6
            if age_at_choice < 0 or age_at_choice > 2_000 or staleness < 0 or staleness > 1_000:
                continue
            if row["fdv_usd"] <= 0 or row["sell_count"] > 0:
                continue
            candidates.append((numeric_distance(entry, row), row))
        candidates.sort(key=lambda item: item[0])
        for distance, row in candidates[:contemporaneous]:
            targets[row["mint"]] = row
            cohorts["contemporaneous_ignored"].add(row["mint"])
            row.setdefault("matched_distances", []).append({"selected_mint": entry["mint"], "distance": distance})
    return targets, cohorts


async def fetch_metadata(targets: Mapping[str, Mapping[str, Any]], concurrency: int) -> dict[str, dict[str, Any]]:
    timeout = aiohttp.ClientTimeout(total=15.0)
    connector = aiohttp.TCPConnector(limit=max(16, concurrency * 2), ttl_dns_cache=600, keepalive_timeout=45)
    sem = asyncio.Semaphore(concurrency)
    output: dict[str, dict[str, Any]] = {}

    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        async def one(mint: str, row: Mapping[str, Any]) -> None:
            uri = str(row.get("uri") or "")
            if not uri:
                output[mint] = {"ok": False, "error": "missing URI"}
                return
            async with sem:
                try:
                    async with session.get(uri, allow_redirects=True) as response:
                        raw = await response.read()
                        text = raw.decode("utf-8", "replace")
                        payload: Any = None
                        try:
                            payload = json.loads(text)
                        except json.JSONDecodeError:
                            pass
                        twitter = social_value(payload, {"twitter", "x", "x_url", "twitter_url"})
                        website = social_value(payload, {"website", "external_url", "externalurl"})
                        telegram = social_value(payload, {"telegram", "telegram_url"})
                        discord = social_value(payload, {"discord", "discord_url"})
                        _, handle, status_id = twitter_parts(twitter)
                        tweet_ms = tweet_time_ms(status_id)
                        launch_ms = integer(row.get("create_ns")) / 1e6
                        tweet_age = (launch_ms - tweet_ms) / 1000 if tweet_ms is not None else None
                        output[mint] = {
                            "ok": response.status < 400,
                            "status": response.status,
                            "twitter": twitter,
                            "twitter_handle": handle,
                            "twitter_status_id": status_id,
                            "twitter_kind": "status" if status_id else "profile" if handle else "other" if twitter else "none",
                            "tweet_age_seconds": tweet_age,
                            "website": website,
                            "telegram": telegram,
                            "discord": discord,
                            "has_any_social": bool(twitter or website or telegram or discord),
                            "description": str(payload.get("description") or "") if isinstance(payload, Mapping) else "",
                            "created_on": str(payload.get("createdOn") or payload.get("created_on") or "") if isinstance(payload, Mapping) else "",
                            "metadata_keys": sorted(str(key) for key in payload) if isinstance(payload, Mapping) else [],
                        }
                except Exception as exc:
                    output[mint] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

        await asyncio.gather(*(one(mint, row) for mint, row in targets.items()))
    return output


def cohort_summary(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    valid = [row for row in rows if row.get("metadata_ok")]
    status = [row for row in valid if row.get("twitter_status_id")]
    nonnegative = [row for row in status if finite(row.get("tweet_age_seconds"), float("nan")) >= -5]
    ages = [finite(row.get("tweet_age_seconds")) for row in nonnegative]
    return {
        "launches": len(rows),
        "metadata_resolved": len(valid),
        "selected": sum(bool(row.get("selected")) for row in rows),
        "e4_winners": sum(bool(row.get("selected")) and bool(row.get("e4_won")) for row in rows),
        "twitter_rate": sum(bool(row.get("twitter")) for row in valid) / len(valid) if valid else None,
        "twitter_status_rate": len(status) / len(valid) if valid else None,
        "website_rate": sum(bool(row.get("website")) for row in valid) / len(valid) if valid else None,
        "any_social_rate": sum(bool(row.get("has_any_social")) for row in valid) / len(valid) if valid else None,
        "status_age_seconds": {
            "count": len(ages),
            "median": median(ages),
            "p10": percentile(ages, 0.10),
            "p25": percentile(ages, 0.25),
            "p75": percentile(ages, 0.75),
            "p90": percentile(ages, 0.90),
            "within_1s": sum(-5 <= value <= 1 for value in ages),
            "within_5s": sum(-5 <= value <= 5 for value in ages),
            "within_20s": sum(-5 <= value <= 20 for value in ages),
            "within_60s": sum(-5 <= value <= 60 for value in ages),
            "within_300s": sum(-5 <= value <= 300 for value in ages),
        },
    }


def chronological_features(rows: list[dict[str, Any]], source_authority: Mapping[str, float]) -> None:
    prior_handle_selected: Counter[str] = Counter()
    prior_handle_wins: Counter[str] = Counter()
    prior_creator_selected: Counter[str] = Counter()
    prior_creator_wins: Counter[str] = Counter()
    for row in sorted(rows, key=lambda value: (integer(value.get("create_ns")), str(value.get("mint")))):
        handle = str(row.get("twitter_handle") or "")
        creator = str(row.get("creator") or "")
        row["prior_handle_e4"] = prior_handle_selected[handle] if handle else 0
        row["prior_handle_wins"] = prior_handle_wins[handle] if handle else 0
        row["prior_creator_e4"] = prior_creator_selected[creator] if creator else 0
        row["prior_creator_wins"] = prior_creator_wins[creator] if creator else 0
        row["source_authority"] = finite(source_authority.get(handle)) if handle else 0.0
        if row.get("selected"):
            if handle:
                prior_handle_selected[handle] += 1
                if row.get("e4_won"):
                    prior_handle_wins[handle] += 1
            if creator:
                prior_creator_selected[creator] += 1
                if row.get("e4_won"):
                    prior_creator_wins[creator] += 1


def rule_grid(rows: list[dict[str, Any]], train_max_run: int) -> dict[str, Any]:
    train = [row for row in rows if integer(row.get("run_index")) <= train_max_run]
    holdout = [row for row in rows if integer(row.get("run_index")) > train_max_run]

    def evaluate(dataset: list[dict[str, Any]], params: Mapping[str, Any]) -> dict[str, Any]:
        candidates = []
        for row in dataset:
            age = row.get("tweet_age_seconds")
            if age is None or not (-5 <= finite(age) <= finite(params["max_tweet_age_seconds"])):
                continue
            if params["require_status"] and not row.get("twitter_status_id"):
                continue
            identity = str(params["identity"])
            allowed = {
                "ANY": True,
                "KNOWN_SOURCE": finite(row.get("source_authority")) > 0,
                "PRIOR_HANDLE": integer(row.get("prior_handle_e4")) >= 1,
                "PRIOR_CREATOR": integer(row.get("prior_creator_e4")) >= 1,
                "PRIOR_HANDLE_OR_CREATOR": integer(row.get("prior_handle_e4")) >= 1 or integer(row.get("prior_creator_e4")) >= 1,
                "PRIOR_WIN": integer(row.get("prior_handle_wins")) >= 1 or integer(row.get("prior_creator_wins")) >= 1,
                "KNOWN_OR_PRIOR": finite(row.get("source_authority")) > 0 or integer(row.get("prior_handle_e4")) >= 1 or integer(row.get("prior_creator_e4")) >= 1,
            }[identity]
            if not allowed:
                continue
            if finite(row.get("creator_seed_sol")) < finite(params["min_seed_sol"]):
                continue
            if integer(row.get("unique_outside_buyers")) < integer(params["min_buyers"]):
                continue
            if integer(row.get("sell_count")) > 0:
                continue
            if not (2_500 <= finite(row.get("fdv_usd")) <= 10_000):
                continue
            candidates.append(row)
        true = [row for row in candidates if row.get("selected")]
        winners = [row for row in true if row.get("e4_won")]
        all_selected = [row for row in dataset if row.get("selected")]
        all_winners = [row for row in all_selected if row.get("e4_won")]
        return {
            "universe": len(dataset),
            "e4_entries": len(all_selected),
            "e4_winners": len(all_winners),
            "candidates": len(candidates),
            "true_e4": len(true),
            "true_e4_winners": len(winners),
            "false_positives": len(candidates) - len(true),
            "precision": len(true) / len(candidates) if candidates else 0.0,
            "recall": len(true) / len(all_selected) if all_selected else 0.0,
            "winner_recall": len(winners) / len(all_winners) if all_winners else 0.0,
            "selected_win_rate": len(winners) / len(true) if true else None,
            "selected_pnl_sol": sum(finite(row.get("e4_pnl_sol")) for row in true),
        }

    ranked = []
    for values in itertools.product(
        (1, 2, 5, 10, 20, 30, 60, 120, 300, 600, 1_800),
        ("ANY", "KNOWN_SOURCE", "PRIOR_HANDLE", "PRIOR_CREATOR", "PRIOR_HANDLE_OR_CREATOR", "PRIOR_WIN", "KNOWN_OR_PRIOR"),
        (0.0, 0.25, 0.5, 1.0, 2.0),
        (0, 1, 2),
    ):
        params = {
            "max_tweet_age_seconds": values[0],
            "identity": values[1],
            "min_seed_sol": values[2],
            "min_buyers": values[3],
            "require_status": True,
        }
        result = evaluate(train, params)
        valid = result["recall"] >= 0.10 and result["winner_recall"] >= 0.10
        objective = (
            1 if valid else 0,
            result["precision"],
            result["winner_recall"],
            result["recall"],
            -result["candidates"],
        )
        ranked.append((objective, params, result))
    ranked.sort(key=lambda value: value[0], reverse=True)
    _, best_params, train_result = ranked[0]
    holdout_result = evaluate(holdout, best_params)
    return {
        "train_max_run_index": train_max_run,
        "rule": best_params,
        "train": train_result,
        "holdout": holdout_result,
        "safe_to_authorize": bool(
            holdout_result["precision"] >= 0.20
            and holdout_result["recall"] >= 0.10
            and holdout_result["winner_recall"] >= 0.10
            and holdout_result["candidates"] <= max(20, holdout_result["e4_entries"] * 5)
        ),
        "top_train_rules": [
            {"rule": params, "train": result}
            for _, params, result in ranked[:20]
        ],
    }


async def main_async(args: argparse.Namespace) -> int:
    all_launches: list[dict[str, Any]] = []
    for run_index, item in enumerate(args.pair):
        batch, events = item.split(":", 1)
        launches, _ = load_capture(Path(batch), Path(events), run_index)
        all_launches.extend(launches)
    targets, cohorts = target_launches(all_launches, args.controls_per_entry)
    metadata = await fetch_metadata(targets, args.metadata_concurrency)
    sources = known_sources(args.social_sources)

    rows = []
    for mint, launch in targets.items():
        info = metadata.get(mint) or {}
        rows.append({
            **launch,
            "metadata_ok": bool(info.get("ok")),
            **{key: value for key, value in info.items() if key != "ok"},
        })
    chronological_features(rows, sources)
    row_by_mint = {row["mint"]: row for row in rows}

    cohort_report = {}
    for name, mints in cohorts.items():
        cohort_report[name] = cohort_summary([row_by_mint[mint] for mint in mints if mint in row_by_mint])
    cohort_report["all_targets"] = cohort_summary(rows)
    cohort_report["all_ignored_targets"] = cohort_summary([row for row in rows if not row["selected"]])

    handle_rows = []
    by_handle: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        handle = str(row.get("twitter_handle") or "")
        if handle:
            by_handle[handle].append(row)
    for handle, group in by_handle.items():
        chosen = [row for row in group if row["selected"]]
        handle_rows.append({
            "handle": handle,
            "authority": finite(sources.get(handle)),
            "target_launches": len(group),
            "e4_selected": len(chosen),
            "selection_rate": len(chosen) / len(group),
            "e4_wins": sum(bool(row.get("e4_won")) for row in chosen),
            "selected_win_rate": sum(bool(row.get("e4_won")) for row in chosen) / len(chosen) if chosen else None,
            "selected_pnl_sol": sum(finite(row.get("e4_pnl_sol")) for row in chosen),
            "median_tweet_age_seconds": median([finite(row.get("tweet_age_seconds")) for row in group if row.get("tweet_age_seconds") is not None]),
        })
    handle_rows.sort(key=lambda row: (-integer(row["e4_selected"]), -finite(row["selected_pnl_sol"]), row["handle"]))

    rule = rule_grid(rows, args.train_max_run_index)
    selected_status = [row for row in rows if row["selected"] and row.get("twitter_status_id")]
    result = {
        "version": "e4-v12-social-choice-forensics-v1",
        "coverage": {
            "captured_launches": len(all_launches),
            "e4_entries": sum(bool(row["selected"]) for row in all_launches),
            "metadata_targets": len(rows),
            "metadata_resolved": sum(bool(row.get("metadata_ok")) for row in rows),
            "selected_creator_population": len(cohorts.get("selected_creator_population", set())),
            "selected_buyer_cluster_population": len(cohorts.get("selected_buyer_cluster_population", set())),
            "contemporaneous_ignored": len(cohorts.get("contemporaneous_ignored", set())),
        },
        "selected_tweet_age_outcomes": {
            str(threshold): {
                "trades": len(subset := [row for row in selected_status if -5 <= finite(row.get("tweet_age_seconds")) <= threshold]),
                "wins": sum(bool(row.get("e4_won")) for row in subset),
                "win_rate": sum(bool(row.get("e4_won")) for row in subset) / len(subset) if subset else None,
                "pnl_sol": sum(finite(row.get("e4_pnl_sol")) for row in subset),
            }
            for threshold in (1, 2, 5, 10, 20, 30, 60, 120, 300, 600, 1_800, 3_600, 86_400)
        },
        "cohorts": cohort_report,
        "causal_rule_search": rule,
        "handles": handle_rows,
        "rows": rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({
        "coverage": result["coverage"],
        "selected": cohort_report.get("selected"),
        "same_creator_ignored": cohort_report.get("same_creator_ignored"),
        "same_buyer_cluster_ignored": cohort_report.get("same_buyer_cluster_ignored"),
        "contemporaneous_ignored": cohort_report.get("contemporaneous_ignored"),
        "causal_rule_search": rule,
        "top_handles": handle_rows[:20],
    }, indent=2, sort_keys=True), flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare exact X/social catalysts for every captured E4 choice and ignored control")
    parser.add_argument("--pair", action="append", default=[], metavar="BATCH:EVENTS")
    parser.add_argument("--social-sources", type=Path, default=Path("models/e4/e4-social-sources.json"))
    parser.add_argument("--controls-per-entry", type=int, default=10)
    parser.add_argument("--metadata-concurrency", type=int, default=20)
    parser.add_argument("--train-max-run-index", type=int, default=7)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.pair:
        parser.error("at least one --pair is required")
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
