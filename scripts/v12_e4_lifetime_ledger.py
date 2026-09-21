#!/usr/bin/env python3
"""Build a fail-closed lifetime E4 trade and attempt ledger for V12 Next Gen.

Only explicit E4 evidence is admitted. Gambit replay/backtest positions are
deliberately excluded. "Complete" means reconciled source coverage, not merely
that one wallet-address scan reached the end of its signature history.
"""
from __future__ import annotations

import argparse
import json
import math
import time
from collections import defaultdict
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

SCHEMA = "e4-lifetime-ledger-v1"
UNKNOWN = "UNKNOWN_CREATOR"
E4_WALLET = "E4EzXdwf7NNdqM2XGswWaWHfxgucVCo24PTCcrimTKBz"

DEFAULT_SOURCES = (
    Path("models/e4/e4-complete-creator-history.json"),
    Path("models/e4/e4-observed-v1.json"),
    Path("models/e4/e4-v11-forward-evidence.json"),
    Path("models/e4/evidence-epochs/v12-pre-role-model-pipeline-77611ad417ef.json"),
    Path("models/e4/e4-v12-forward-evidence.json"),
    Path("research/v12-e4-48h-comparison.json"),
    Path("artifacts/e4-v12-selection-backfill.json"),
)
POSITION_KEYS = ("same_window_e4_positions", "e4_positions")


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


def finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def integer(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def rows(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, list):
        return [row for row in value if isinstance(row, Mapping)]
    if isinstance(value, Mapping):
        return [row for row in value.values() if isinstance(row, Mapping)]
    return []


def outcome(raw: Mapping[str, Any]) -> str:
    explicit = str(raw.get("outcome") or "").upper()
    if explicit in {"WIN", "LOSS"}:
        return explicit
    pnl = finite(raw.get("pnl_sol"))
    if pnl is None:
        return "UNKNOWN"
    return "WIN" if pnl > 0 else "LOSS"


def normalize_trade(
    raw: Mapping[str, Any],
    *,
    source: str,
    evidence_class: str,
) -> dict[str, Any] | None:
    mint = str(raw.get("mint") or "")
    if not mint:
        return None
    creator = str(raw.get("creator") or UNKNOWN)
    confidence = finite(raw.get("creator_resolution_confidence"))
    if confidence is None:
        confidence = 1.0 if creator != UNKNOWN else 0.0
    return {
        "mint": mint,
        "creator": creator,
        "creator_resolution_confidence": confidence,
        "entry_signature": str(raw.get("entry_signature") or ""),
        "exit_signature": str(raw.get("exit_signature") or ""),
        "entry_time": integer(raw.get("entry_time")),
        "exit_time": integer(raw.get("exit_time")),
        "pnl_sol": finite(raw.get("pnl_sol")),
        "cost_sol": finite(raw.get("cost_sol") or raw.get("entry_cost_sol")),
        "proceeds_sol": finite(raw.get("proceeds_sol")),
        "hold_ms": finite(raw.get("hold_ms")),
        "outcome": outcome(raw),
        "source": source,
        "evidence_class": evidence_class,
    }


def normalize_attempt(raw: Mapping[str, Any], *, source: str) -> dict[str, Any] | None:
    selection_type = str(raw.get("selection_type") or "").upper()
    if selection_type not in {"SUCCESSFUL_BUY", "FAILED_ATTEMPT"}:
        return None
    identity = str(raw.get("event_identity") or raw.get("wallet_signature") or "")
    if not identity:
        return None
    return {
        "event_identity": identity,
        "selection_type": selection_type,
        "wallet_signature": str(raw.get("wallet_signature") or ""),
        "mint": str(raw.get("mint") or ""),
        "timestamp": integer(raw.get("timestamp")),
        "reason_code": str(raw.get("reason_code") or raw.get("outcome") or ""),
        "source_window": str(raw.get("source_window") or ""),
        "source": source,
    }


def trade_match(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    if str(a.get("mint") or "") != str(b.get("mint") or ""):
        return False
    a_sig = str(a.get("entry_signature") or "")
    b_sig = str(b.get("entry_signature") or "")
    if a_sig and b_sig:
        return a_sig == b_sig
    a_time = integer(a.get("entry_time"))
    b_time = integer(b.get("entry_time"))
    if a_time and b_time:
        return abs(a_time - b_time) <= 5
    # An old labelled row has no timestamp/signature. Attach it to the timed
    # record for the same mint instead of counting the same trade twice.
    return not a_time or not b_time


def merge_trade(a: Mapping[str, Any], b: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(a)
    sources = set(merged.get("sources") or [merged.get("source")])
    sources.update(b.get("sources") or [b.get("source")])
    merged["sources"] = sorted(str(x) for x in sources if x)

    classes = set(merged.get("evidence_classes") or [merged.get("evidence_class")])
    classes.update(b.get("evidence_classes") or [b.get("evidence_class")])
    merged["evidence_classes"] = sorted(str(x) for x in classes if x)

    for key in (
        "entry_signature", "exit_signature", "entry_time", "exit_time",
        "pnl_sol", "cost_sol", "proceeds_sol", "hold_ms",
    ):
        if merged.get(key) in (None, "", 0) and b.get(key) not in (None, "", 0):
            merged[key] = b.get(key)

    old_conf = finite(merged.get("creator_resolution_confidence")) or 0.0
    new_conf = finite(b.get("creator_resolution_confidence")) or 0.0
    if str(merged.get("creator") or UNKNOWN) == UNKNOWN or new_conf > old_conf:
        creator = str(b.get("creator") or UNKNOWN)
        if creator != UNKNOWN:
            merged["creator"] = creator
            merged["creator_resolution_confidence"] = new_conf

    known = {
        str(value)
        for value in (merged.get("outcome"), b.get("outcome"))
        if str(value) in {"WIN", "LOSS"}
    }
    merged["outcome"] = next(iter(known)) if len(known) == 1 else (
        "CONFLICT" if len(known) > 1 else "UNKNOWN"
    )
    merged.pop("source", None)
    merged.pop("evidence_class", None)
    return merged


def dedupe_trades(records: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for raw in records:
        row = dict(raw)
        bucket = grouped[str(row["mint"])]
        for index, existing in enumerate(bucket):
            if trade_match(existing, row):
                bucket[index] = merge_trade(existing, row)
                break
        else:
            row["sources"] = [str(row.pop("source"))]
            row["evidence_classes"] = [str(row.pop("evidence_class"))]
            bucket.append(row)
    result = [row for bucket in grouped.values() for row in bucket]
    result.sort(
        key=lambda row: (
            integer(row.get("entry_time")),
            str(row.get("mint") or ""),
            str(row.get("entry_signature") or ""),
        )
    )
    return result


def dedupe_attempts(records: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for raw in records:
        row = dict(raw)
        key = str(row.get("event_identity") or "")
        if not key:
            continue
        current = result.get(key)
        if current is None:
            result[key] = row
            continue
        for field in ("mint", "wallet_signature", "reason_code", "source_window"):
            if not current.get(field) and row.get(field):
                current[field] = row[field]
        sources = set(str(current.get("source") or "").split("+"))
        sources.update(str(row.get("source") or "").split("+"))
        current["source"] = "+".join(sorted(x for x in sources if x))
    return sorted(
        result.values(),
        key=lambda row: (integer(row.get("timestamp")), row["event_identity"]),
    )


def creators_from_trades(trades: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in trades:
        creator = str(row.get("creator") or UNKNOWN)
        if creator != UNKNOWN:
            grouped[creator].append(row)

    output = []
    for creator, group in grouped.items():
        winners = [row for row in group if row.get("outcome") == "WIN"]
        losers = [row for row in group if row.get("outcome") == "LOSS"]
        pnls = [
            float(value)
            for row in group
            if (value := finite(row.get("pnl_sol"))) is not None
        ]
        confidences = [
            float(value)
            for row in group
            if (value := finite(row.get("creator_resolution_confidence"))) is not None
        ]
        known = len(winners) + len(losers)
        output.append({
            "creator": creator,
            "wins": len(winners),
            "losses": len(losers),
            "trades": len(group),
            "known_outcome_trades": known,
            "win_rate": len(winners) / known if known else 0.0,
            "winning_pnl_sol": sum(value for value in pnls if value > 0),
            "net_pnl_sol_observed": sum(pnls) if pnls else None,
            "pnl_observed_trades": len(pnls),
            "minimum_resolution_confidence": min(confidences) if confidences else 0.0,
            "winner_mints": sorted({str(row["mint"]) for row in winners}),
            "loser_mints": sorted({str(row["mint"]) for row in losers}),
        })
    output.sort(key=lambda row: (-row["wins"], -row["trades"], row["creator"]))
    return output


def extract_document(
    path: str,
    payload: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    trades: list[dict[str, Any]] = []
    attempts: list[dict[str, Any]] = []
    claims: list[dict[str, Any]] = []

    if path.endswith("e4-complete-creator-history.json"):
        for raw in rows(payload.get("trades")):
            item = normalize_trade(
                raw,
                source=path,
                evidence_class=str(raw.get("source") or "COMPLETE_HISTORY"),
            )
            if item:
                trades.append(item)

    for key in POSITION_KEYS:
        for raw in rows(payload.get(key)):
            item = normalize_trade(
                raw,
                source=path,
                evidence_class=key.upper(),
            )
            if item:
                trades.append(item)

    for raw in rows(payload.get("outcomes")):
        item = normalize_attempt(raw, source=path)
        if item:
            attempts.append(item)

    evidence = payload.get("evidence")
    if isinstance(evidence, Mapping) and (
        "closed_memecoin_positions" in evidence or "swaps" in evidence
    ):
        claims.append({
            "source": path,
            "claim_type": "AGGREGATE_OBSERVED_BASELINE",
            "wallet": str(payload.get("wallet") or ""),
            "closed_positions": integer(evidence.get("closed_memecoin_positions")),
            "swaps": integer(evidence.get("swaps")),
            "window_hours": finite(evidence.get("window_hours_approx")),
            "note": "aggregate only; not added to canonical closed-trade rows",
        })

    summary = payload.get("summary")
    if isinstance(summary, Mapping):
        if "e4_historical_exact_positions" in summary:
            claims.append({
                "source": path,
                "claim_type": "HISTORICAL_EXACT_POSITION_CLAIM",
                "closed_positions": integer(summary.get("e4_historical_exact_positions")),
                "note": "aggregate claim only",
            })
        if "same_window_e4_closed_positions" in summary:
            claims.append({
                "source": path,
                "claim_type": "SAME_WINDOW_POSITION_CLAIM",
                "closed_positions": integer(summary.get("same_window_e4_closed_positions")),
                "note": "extractor reconciliation claim",
            })
    return trades, attempts, claims


def discover_artifact_json(root: Path | None) -> list[Path]:
    if root is None or not root.exists():
        return []
    return sorted(
        path
        for path in root.rglob("*.json")
        if path.is_file() and path.stat().st_size <= 64 * 1024 * 1024
    )


def build(
    documents: list[tuple[str, Mapping[str, Any], str]],
    *,
    minimum_expected_closed_trades: int,
    artifact_inventory_complete: bool,
    wallet_identity_inventory_complete: bool,
    source_reconciliation_complete: bool,
) -> dict[str, Any]:
    raw_trades: list[dict[str, Any]] = []
    raw_attempts: list[dict[str, Any]] = []
    claims: list[dict[str, Any]] = []
    manifest = []

    for path, payload, origin in documents:
        trade_rows, attempt_rows, aggregate = extract_document(path, payload)
        raw_trades.extend(trade_rows)
        raw_attempts.extend(attempt_rows)
        claims.extend(aggregate)
        manifest.append({
            "path": path,
            "origin": origin,
            "trade_rows_extracted": len(trade_rows),
            "attempt_rows_extracted": len(attempt_rows),
            "aggregate_claims": len(aggregate),
            "contains_gambit_positions_ignored": bool(payload.get("gambit_positions")),
        })

    trades = dedupe_trades(raw_trades)
    attempts = dedupe_attempts(raw_attempts)
    creators = creators_from_trades(trades)

    wins = sum(row.get("outcome") == "WIN" for row in trades)
    losses = sum(row.get("outcome") == "LOSS" for row in trades)
    conflicts = sum(row.get("outcome") == "CONFLICT" for row in trades)
    resolved = sum(str(row.get("creator") or UNKNOWN) != UNKNOWN for row in trades)
    successful_attempts = sum(
        row.get("selection_type") == "SUCCESSFUL_BUY" for row in attempts
    )
    failed_attempts = sum(
        row.get("selection_type") == "FAILED_ATTEMPT" for row in attempts
    )

    blockers: list[str] = []
    if len(trades) < minimum_expected_closed_trades:
        blockers.append(
            f"closed_trade_floor_not_met:{len(trades)}<{minimum_expected_closed_trades}"
        )
    if not artifact_inventory_complete:
        blockers.append("historical_actions_artifact_inventory_not_certified_complete")
    if not wallet_identity_inventory_complete:
        blockers.append("historical_e4_wallet_identity_inventory_not_certified_complete")
    if not source_reconciliation_complete:
        blockers.append("source_reconciliation_not_certified_complete")
    if conflicts:
        blockers.append(f"trade_outcome_conflicts:{conflicts}")

    ready = not blockers
    return {
        "schema_version": SCHEMA,
        "status": "CERTIFIED_COMPLETE" if ready else "COLLECTING",
        "generated_epoch": int(time.time()),
        "canonical_wallet": E4_WALLET,
        "counts": {
            "closed_trade_records": len(trades),
            "wins": wins,
            "losses": losses,
            "unknown_outcomes": len(trades) - wins - losses - conflicts,
            "outcome_conflicts": conflicts,
            "creator_resolved_trades": resolved,
            "creator_resolution_fraction": resolved / len(trades) if trades else 0.0,
            "attempt_records": len(attempts),
            "successful_buy_attempts": successful_attempts,
            "failed_attempts": failed_attempts,
            "unique_creators_resolved": len(creators),
            "source_documents": len(documents),
        },
        "completeness": {
            "ready_for_nextgen_training": ready,
            "minimum_expected_closed_trades": minimum_expected_closed_trades,
            "closed_trade_floor_met": len(trades) >= minimum_expected_closed_trades,
            "committed_source_inventory_scanned": True,
            "historical_actions_artifact_inventory_complete": artifact_inventory_complete,
            "historical_e4_wallet_identity_inventory_complete": wallet_identity_inventory_complete,
            "source_reconciliation_complete": source_reconciliation_complete,
            "blockers": blockers,
            "definition": (
                "Complete requires reconciled lifetime source coverage; exhausting "
                "one wallet address alone is insufficient."
            ),
        },
        "source_manifest": manifest,
        "aggregate_claims": claims,
        "creators": creators,
        "trades": trades,
        "attempts": attempts,
    }


def render(ledger: Mapping[str, Any]) -> str:
    counts = ledger["counts"]
    completeness = ledger["completeness"]
    blockers = list(completeness.get("blockers") or [])
    lines = [
        "# E4 lifetime ledger",
        "",
        f"Status: **{ledger['status']}**",
        "",
        "Only explicit E4 evidence is counted. Gambit replay/backtest positions are excluded.",
        "",
        "## Recovered evidence",
        "",
        f"- Canonical closed trades: {counts['closed_trade_records']}",
        f"- Wins / losses: {counts['wins']} / {counts['losses']}",
        f"- E4 decision/attempt records: {counts['attempt_records']}",
        f"- Successful-buy attempts: {counts['successful_buy_attempts']}",
        f"- Failed attempts: {counts['failed_attempts']}",
        f"- Creator-resolved trades: {counts['creator_resolved_trades']}",
        f"- Resolved creators: {counts['unique_creators_resolved']}",
        "",
        "## Certification",
        "",
        f"- Minimum expected trade floor: {completeness['minimum_expected_closed_trades']}",
        f"- Trade floor met: {completeness['closed_trade_floor_met']}",
        "- Actions artifact inventory complete: "
        f"{completeness['historical_actions_artifact_inventory_complete']}",
        "- Wallet identity inventory complete: "
        f"{completeness['historical_e4_wallet_identity_inventory_complete']}",
        "- Source reconciliation complete: "
        f"{completeness['source_reconciliation_complete']}",
        "",
        "### Blocking items",
        "",
    ]
    lines.extend(f"- {item}" for item in blockers)
    if not blockers:
        lines.append("- none")
    lines += [
        "",
        "Next Gen creator authority remains disabled until "
        "ready_for_nextgen_training=true.",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", action="append", default=[])
    parser.add_argument("--artifact-root", type=Path)
    parser.add_argument("--minimum-expected-closed-trades", type=int, default=1000)
    parser.add_argument("--artifact-inventory-complete", action="store_true")
    parser.add_argument("--wallet-identity-inventory-complete", action="store_true")
    parser.add_argument("--source-reconciliation-complete", action="store_true")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("models/e4/e4-lifetime-ledger.json"),
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("research/e4-lifetime-ledger.md"),
    )
    args = parser.parse_args()

    documents: list[tuple[str, Mapping[str, Any], str]] = []
    seen: set[str] = set()
    for path in [*DEFAULT_SOURCES, *(Path(value) for value in args.source)]:
        if not path.exists():
            continue
        payload = load(path)
        if not isinstance(payload, Mapping) or str(path) in seen:
            continue
        seen.add(str(path))
        documents.append((str(path), payload, "COMMITTED_REPO"))

    for path in discover_artifact_json(args.artifact_root):
        try:
            payload = load(path)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, Mapping):
            continue
        relative = str(path.relative_to(args.artifact_root)) if args.artifact_root else str(path)
        key = f"artifact://{relative}"
        if key in seen:
            continue
        seen.add(key)
        documents.append((key, payload, "GITHUB_ACTION_ARTIFACT"))

    ledger = build(
        documents,
        minimum_expected_closed_trades=max(1, args.minimum_expected_closed_trades),
        artifact_inventory_complete=args.artifact_inventory_complete,
        wallet_identity_inventory_complete=args.wallet_identity_inventory_complete,
        source_reconciliation_complete=args.source_reconciliation_complete,
    )
    write(args.output, ledger)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(render(ledger) + "\n", encoding="utf-8")
    print(json.dumps({
        "status": ledger["status"],
        "counts": ledger["counts"],
        "completeness": ledger["completeness"],
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
