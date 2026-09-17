#!/usr/bin/env python3
"""Build the canonical V12 E4 + HG trade/developer library.

The builder is evidence-preserving:
- E4 winner/loser labels come only from the frozen creator-expectancy corpus.
- Entry/exit/PnL fields are enriched only when explicit evidence is present.
- Post-exit winner/loser flags require explicit observed post-exit coverage.
- HG imports only terminal closed positions and explicit rejections from completed
  workflow checkpoints. Observer-only V2 accounts are never counted as strategies.
- Unknown or incomplete fields remain unknown; nothing is imputed.

No transaction signing, broadcast, or live execution exists in this module.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping

SCHEMA = "e4-hg-trade-library-v12-1"
MODEL = "FULL_3S_V2_LIBRARY"
E4_MODEL = "E4"
UNKNOWN_CREATOR = "UNKNOWN_CREATOR"


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, sort_keys=True, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def stable_id(*parts: Any) -> str:
    raw = "|".join("" if x is None else str(x) for x in parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def iso_to_ns(value: Any) -> int | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        d = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return int(d.timestamp() * 1_000_000_000)
    except ValueError:
        return None


def walk(obj: Any, path: tuple[str, ...] = ()) -> Iterable[tuple[tuple[str, ...], Any]]:
    yield path, obj
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield from walk(value, path + (str(key),))
    elif isinstance(obj, list):
        for i, value in enumerate(obj):
            yield from walk(value, path + (str(i),))


def creator_rows(expectancy: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    rows = expectancy.get("all_creators") or expectancy.get("top_creators") or []
    if not isinstance(rows, list):
        raise ValueError("creator expectancy missing creator rows")
    return [x for x in rows if isinstance(x, dict)]


def load_winner_tsv(paths: list[Path]) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for path in paths:
        if not path.exists():
            continue
        with path.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f, delimiter="\t"):
                mint = row.get("mint")
                if not mint:
                    continue
                out[mint] = {
                    "gross_pnl_sol": finite_number(float(row["gross_pnl_sol"])) if row.get("gross_pnl_sol") else None,
                    "entry_sol": finite_number(float(row["entry_sol"])) if row.get("entry_sol") else None,
                }
    return out


def evidence_index(evidence: Any) -> dict[str, dict[str, Any]]:
    """Best-effort explicit trade-field enrichment keyed by mint."""
    index: dict[str, dict[str, Any]] = {}
    trade_keys = {
        "decision_ns", "entry_fill_ns", "entry_intent_ns", "entry_sol", "entry_tokens",
        "sell_fill_ns", "sell_intent_ns", "closed_ns", "net_pnl_sol", "net_return",
        "gross_sell_sol", "highest_return", "lowest_return", "trigger_reason", "strategy",
        "post", "post_exit", "went_higher", "went_lower",
    }
    for _, node in walk(evidence):
        if not isinstance(node, dict):
            continue
        mint = node.get("mint")
        if not isinstance(mint, str) or not mint or not any(k in node for k in trade_keys):
            continue
        dest = index.setdefault(mint, {})
        for k in trade_keys:
            if k in node and node[k] is not None and dest.get(k) is None:
                dest[k] = node[k]
    return index


def post_exit_from(node: Mapping[str, Any] | None) -> dict[str, Any]:
    node = node or {}
    post = node.get("post") if isinstance(node.get("post"), dict) else None
    if post is None and isinstance(node.get("post_exit"), dict):
        post = node.get("post_exit")
    post = post or {}
    coverage = post.get("coverage")
    complete = post.get("complete")
    higher = post.get("went_higher", node.get("went_higher"))
    lower = post.get("went_lower", node.get("went_lower"))
    explicitly_observed = complete is True and coverage in ("OBSERVED", "NO_FURTHER_TICKS_OBSERVED")
    if coverage is None and (higher is not None or lower is not None):
        explicitly_observed = True
    if not explicitly_observed:
        return {"status": "UNKNOWN", "coverage": coverage,
                "complete": bool(complete) if complete is not None else None,
                "went_higher": None, "went_lower": None}
    return {"status": "OBSERVED", "coverage": coverage,
            "complete": True if complete is None else bool(complete),
            "went_higher": bool(higher) if higher is not None else None,
            "went_lower": bool(lower) if lower is not None else None}


def build_e4_records(expectancy: Mapping[str, Any], evidence: Any,
                     winner_tsv: dict[str, dict[str, float]]) -> list[dict[str, Any]]:
    enrich = evidence_index(evidence)
    records: list[dict[str, Any]] = []
    seen_mints: dict[str, str] = {}
    for c in creator_rows(expectancy):
        creator = str(c.get("creator") or UNKNOWN_CREATOR)
        for outcome, field in (("WIN", "winner_mints"), ("LOSS", "loser_mints")):
            mints = c.get(field) or []
            if not isinstance(mints, list):
                continue
            for mint in mints:
                if not isinstance(mint, str) or not mint:
                    continue
                prev = seen_mints.get(mint)
                if prev and prev != outcome:
                    raise ValueError(f"conflicting E4 labels for {mint}: {prev} vs {outcome}")
                if prev:
                    continue
                seen_mints[mint] = outcome
                x = enrich.get(mint, {})
                tsv = winner_tsv.get(mint, {})
                entry_sol = finite_number(x.get("entry_sol"))
                if entry_sol is None:
                    entry_sol = finite_number(tsv.get("entry_sol"))
                records.append({
                    "record_id": stable_id("E4", E4_MODEL, mint, outcome),
                    "source": "E4", "record_type": "TRADE", "model": E4_MODEL,
                    "mint": mint, "creator": creator, "outcome": outcome,
                    "decision_ns": x.get("decision_ns"),
                    "entry_ns": x.get("entry_fill_ns") or x.get("entry_intent_ns"),
                    "exit_ns": x.get("closed_ns") or x.get("sell_fill_ns") or x.get("sell_intent_ns"),
                    "observed_ns": x.get("closed_ns") or x.get("sell_fill_ns") or x.get("decision_ns"),
                    "entry_sol": entry_sol,
                    "gross_pnl_sol": finite_number(tsv.get("gross_pnl_sol")),
                    "net_pnl_sol": finite_number(x.get("net_pnl_sol")),
                    "net_return": finite_number(x.get("net_return")),
                    "highest_return": finite_number(x.get("highest_return")),
                    "lowest_return": finite_number(x.get("lowest_return")),
                    "trigger_reason": x.get("trigger_reason"),
                    "post_exit": post_exit_from(x),
                    "provenance": {"label_source": "models/e4/e4-creator-expectancy.json",
                                   "evidence_enriched": bool(x),
                                   "winner_tsv_enriched": mint in winner_tsv,
                                   "frozen_prelive_corpus": True},
                })
    return records


def _model_for_path(path: tuple[str, ...], arm: str | None = None) -> str | None:
    p = "/".join(path).lower()
    if "/observer" in p or p.endswith("/observer"):
        return None
    if "loss_v2" in p:
        return "FULL_3S_V2_2S_LOSS_EXIT"
    if "loss_control" in p:
        return "FULL_3S_2S_LOSS_EXIT"
    if "/v2/candidate" in p or p.endswith("v2/candidate"):
        return "FULL_3S_V2"
    return arm or "HG"


def _decision_map(state: Any) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for _, node in walk(state):
        if not isinstance(node, dict) or not isinstance(node.get("decisions"), dict):
            continue
        for mint, row in node["decisions"].items():
            if isinstance(mint, str) and isinstance(row, dict):
                dest = out.setdefault(mint, {})
                for k, v in row.items():
                    if v is not None and dest.get(k) is None:
                        dest[k] = v
    return out


def _creator_from_decision(d: Mapping[str, Any] | None) -> str:
    d = d or {}
    return str(d.get("creator") or d.get("creator_wallet") or d.get("developer") or UNKNOWN_CREATOR)


def extract_hg_records(state: Mapping[str, Any], checkpoint: Mapping[str, Any]) -> list[dict[str, Any]]:
    decisions = _decision_map(state)
    records: list[dict[str, Any]] = []
    seen_trade: set[tuple[Any, ...]] = set()
    seen_rej: set[tuple[Any, ...]] = set()
    campaign_id = state.get("campaign_id") or checkpoint.get("run_id") or checkpoint.get("checkpoint_kind")

    for path, node in walk(state):
        if not isinstance(node, dict) or not isinstance(node.get("accounts"), dict):
            continue
        for arm, account in node["accounts"].items():
            if not isinstance(account, dict):
                continue
            model = _model_for_path(path, str(arm))
            if model is None:
                continue
            ledger = account.get("ledger") or []
            if not isinstance(ledger, list):
                continue
            for pos in ledger:
                if not isinstance(pos, dict) or pos.get("status") != "CLOSED":
                    continue
                mint = pos.get("mint")
                if not isinstance(mint, str):
                    continue
                key = (model, mint, pos.get("signal_id"), pos.get("exit_ns"), campaign_id)
                if key in seen_trade:
                    continue
                seen_trade.add(key)
                pnl = finite_number(pos.get("net_pnl_sol"))
                outcome = "UNKNOWN" if pnl is None else ("WIN" if pnl > 0 else "LOSS" if pnl < 0 else "BREAKEVEN")
                decision = decisions.get(mint, {})
                records.append({
                    "record_id": stable_id("HG", campaign_id, model, mint, pos.get("signal_id"), pos.get("exit_ns")),
                    "source": "HG", "record_type": "TRADE", "model": model,
                    "mint": mint, "creator": _creator_from_decision(decision), "outcome": outcome,
                    "decision_ns": decision.get("decision_ns"), "entry_ns": pos.get("entry_ns"),
                    "exit_ns": pos.get("exit_ns"),
                    "observed_ns": pos.get("exit_ns") or decision.get("decision_ns"),
                    "entry_sol": finite_number(pos.get("budget")),
                    "gross_pnl_sol": finite_number(pos.get("gross_pnl_sol")),
                    "net_pnl_sol": pnl, "net_return": finite_number(pos.get("net_return")),
                    "highest_return": finite_number(pos.get("mfe")), "lowest_return": finite_number(pos.get("mae")),
                    "trigger_reason": pos.get("exit_reason"), "post_exit": post_exit_from(pos),
                    "provenance": dict(checkpoint),
                })

    for path, node in walk(state):
        if not isinstance(node, dict) or not isinstance(node.get("rejections"), list):
            continue
        model = _model_for_path(path, None)
        if model is None:
            continue
        for rej in node["rejections"]:
            if not isinstance(rej, dict):
                continue
            mint = rej.get("mint")
            if not isinstance(mint, str):
                continue
            reason = rej.get("reason") or "UNSPECIFIED_REJECTION"
            ns = rej.get("ns")
            arm = rej.get("arm")
            display_model = _model_for_path(path, str(arm)) if arm else model
            key = (display_model, mint, reason, ns, campaign_id)
            if key in seen_rej:
                continue
            seen_rej.add(key)
            decision = decisions.get(mint, {})
            records.append({
                "record_id": stable_id("HG", campaign_id, "REJECTION", display_model, mint, reason, ns),
                "source": "HG", "record_type": "REJECTION", "model": display_model,
                "mint": mint, "creator": _creator_from_decision(decision), "outcome": "UNKNOWN",
                "decision_ns": decision.get("decision_ns") or ns, "entry_ns": None, "exit_ns": None,
                "observed_ns": ns or decision.get("decision_ns"), "entry_sol": None,
                "gross_pnl_sol": None, "net_pnl_sol": None, "net_return": None,
                "highest_return": None, "lowest_return": None, "trigger_reason": reason,
                "post_exit": {"status": "UNKNOWN", "coverage": None, "complete": None,
                              "went_higher": None, "went_lower": None},
                "provenance": dict(checkpoint),
            })
    return records


def checkpoint_from_spec(spec: str) -> tuple[dict[str, Any], dict[str, Any]]:
    parts = spec.split("|", 2)
    if len(parts) != 3:
        raise ValueError("--hg-checkpoint must be KIND|STATE_PATH|RUN_JSON_PATH")
    kind, state_path, run_path = parts
    state = load_json(Path(state_path)); run = load_json(Path(run_path))
    if run.get("status") != "completed":
        raise ValueError(f"HG checkpoint {kind} is not terminal: {run.get('status')}")
    checkpoint = {"checkpoint_kind": kind, "branch": run.get("head_branch"), "run_id": run.get("id"),
                  "run_conclusion": run.get("conclusion"), "run_head_sha": run.get("head_sha"),
                  "run_updated_at": run.get("updated_at"), "run_updated_ns": iso_to_ns(run.get("updated_at")),
                  "terminal_checkpoint": True, "closed_rows_only": True}
    return state, checkpoint


def developer_index(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    by_creator: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in records:
        by_creator[r["creator"]].append(r)
    out: dict[str, dict[str, Any]] = {}
    for creator, rows in sorted(by_creator.items()):
        rows.sort(key=lambda r: (r.get("observed_ns") is None, r.get("observed_ns") or 0, r["record_id"]))
        e4 = [r for r in rows if r["source"] == "E4" and r["record_type"] == "TRADE"]
        hg = [r for r in rows if r["source"] == "HG" and r["record_type"] == "TRADE"]
        rejections = [r for r in rows if r["record_type"] == "REJECTION"]
        e4_wins = sum(r["outcome"] == "WIN" for r in e4); e4_losses = sum(r["outcome"] == "LOSS" for r in e4)
        out[creator] = {
            "creator": creator, "golden_bunch": e4_wins >= 2 and e4_losses == 0,
            "golden_bunch_rule": "E4 resolved history has >=2 wins and 0 losses",
            "e4": {"trades": len(e4), "wins": e4_wins, "losses": e4_losses},
            "hg": {"trades": len(hg), "wins": sum(r["outcome"] == "WIN" for r in hg),
                   "losses": sum(r["outcome"] == "LOSS" for r in hg),
                   "breakeven": sum(r["outcome"] == "BREAKEVEN" for r in hg),
                   "rejections": len(rejections)},
            "record_ids": [r["record_id"] for r in rows], "mints": sorted({r["mint"] for r in rows}),
        }
    return out


def category_index(records: list[dict[str, Any]], developers: Mapping[str, Mapping[str, Any]]) -> dict[str, list[str]]:
    golden_creators = {c for c, d in developers.items() if d.get("golden_bunch")}
    cat = {k: [] for k in ("golden_bunch", "wins", "losses", "breakeven", "wins_after_exit",
                             "losses_after_exit", "post_exit_unknown", "unknown_outcome", "hg_rejections")}
    for r in records:
        rid = r["record_id"]
        if r["creator"] in golden_creators and r["record_type"] == "TRADE": cat["golden_bunch"].append(rid)
        if r["record_type"] == "REJECTION": cat["hg_rejections"].append(rid)
        elif r["outcome"] == "WIN": cat["wins"].append(rid)
        elif r["outcome"] == "LOSS": cat["losses"].append(rid)
        elif r["outcome"] == "BREAKEVEN": cat["breakeven"].append(rid)
        else: cat["unknown_outcome"].append(rid)
        post = r.get("post_exit") or {}
        if post.get("status") != "OBSERVED": cat["post_exit_unknown"].append(rid)
        else:
            if post.get("went_higher") is True: cat["wins_after_exit"].append(rid)
            if post.get("went_lower") is True: cat["losses_after_exit"].append(rid)
    return {k: sorted(v) for k, v in cat.items()}


def build_library(expectancy_path: Path, evidence_path: Path, winner_tsvs: list[Path],
                  checkpoint_specs: list[str]) -> tuple[dict[str, Any], dict[str, Any]]:
    expectancy = load_json(expectancy_path)
    evidence = load_json(evidence_path) if evidence_path.exists() and evidence_path.stat().st_size else {}
    records = build_e4_records(expectancy, evidence, load_winner_tsv(winner_tsvs))
    checkpoints = []
    for spec in checkpoint_specs:
        state, checkpoint = checkpoint_from_spec(spec); checkpoints.append(checkpoint)
        records.extend(extract_hg_records(state, checkpoint))
    unique: dict[str, dict[str, Any]] = {}
    for r in records:
        rid = r["record_id"]
        if rid in unique and unique[rid] != r: raise ValueError(f"record id collision: {rid}")
        unique[rid] = r
    records = sorted(unique.values(), key=lambda r: (r["source"], r["creator"], r["mint"], r["model"], r["record_id"]))
    e4 = [r for r in records if r["source"] == "E4" and r["record_type"] == "TRADE"]
    expected_wins = int(expectancy.get("winner_mints", expectancy.get("source_winner_attached", -1)))
    expected_losses = int(expectancy.get("loser_mints", expectancy.get("source_losers", -1)))
    if sum(r["outcome"] == "WIN" for r in e4) != expected_wins: raise ValueError("E4 winner count mismatch")
    if sum(r["outcome"] == "LOSS" for r in e4) != expected_losses: raise ValueError("E4 loser count mismatch")
    if len({r["mint"] for r in e4}) != expected_wins + expected_losses:
        raise ValueError("E4 source is not a complete one-row-per-mint backfill")
    developers = developer_index(records); categories = category_index(records, developers)
    available_ns = max((x.get("run_updated_ns") or 0 for x in checkpoints), default=0)
    library = {
        "schema": SCHEMA, "version": "V12", "library_model": MODEL, "snapshot_available_ns": available_ns,
        "rules": {
            "golden_bunch": "creator has at least 2 E4 wins and 0 E4 losses; chosen implementation rule because no prior numeric threshold was frozen",
            "wins_after_exit": "explicit observed post-exit evidence says price went higher after exit",
            "losses_after_exit": "explicit observed post-exit evidence says price went lower after exit",
            "post_exit_nonexclusive": True,
            "unknown_policy": "missing outcome/post-exit evidence remains UNKNOWN and is never inferred",
            "hg_import_policy": "terminal workflow checkpoints only; CLOSED paper positions plus explicit rejections; observer-only accounts excluded",
        },
        "source_expectancy": str(expectancy_path), "source_evidence": str(evidence_path),
        "hg_checkpoints": checkpoints,
        "counts": {"records": len(records), "e4_trades": len(e4),
                   "e4_wins": sum(r["outcome"] == "WIN" for r in e4),
                   "e4_losses": sum(r["outcome"] == "LOSS" for r in e4),
                   "hg_trades": sum(r["source"] == "HG" and r["record_type"] == "TRADE" for r in records),
                   "hg_rejections": sum(r["source"] == "HG" and r["record_type"] == "REJECTION" for r in records),
                   "developers": len(developers), "golden_bunch_developers": sum(d["golden_bunch"] for d in developers.values()),
                   "wins_after_exit": len(categories["wins_after_exit"]),
                   "losses_after_exit": len(categories["losses_after_exit"]),
                   "post_exit_unknown": len(categories["post_exit_unknown"])},
        "categories": categories, "records": records,
    }
    devdoc = {"schema": SCHEMA + "-developer-index", "version": "V12", "snapshot_available_ns": available_ns,
              "golden_bunch_developers": sorted(c for c, d in developers.items() if d["golden_bunch"]),
              "developers": developers}
    return library, devdoc


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--expectancy", type=Path, default=Path("models/e4/e4-creator-expectancy.json"))
    p.add_argument("--evidence", type=Path, default=Path("models/e4/e4-v12-forward-evidence.json"))
    p.add_argument("--winner-tsv", type=Path, action="append", default=[])
    p.add_argument("--hg-checkpoint", action="append", default=[])
    p.add_argument("--output-dir", type=Path, default=Path("models/e4/library"))
    a = p.parse_args()
    tsvs = a.winner_tsv or sorted(Path("models/e4").glob("winning-mints-*.tsv"))
    library, devdoc = build_library(a.expectancy, a.evidence, tsvs, a.hg_checkpoint)
    write_json(a.output_dir / "e4-hg-trade-library.json", library)
    write_json(a.output_dir / "e4-hg-developer-index.json", devdoc)
    print(json.dumps(library["counts"], sort_keys=True)); return 0


if __name__ == "__main__":
    raise SystemExit(main())
