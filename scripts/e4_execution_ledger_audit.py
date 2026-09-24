#!/usr/bin/env python3
"""Offline integrity audit for the E4 execution ledger.

This never connects to Solana, Axiom, a wallet, signer, or transaction route.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        ).fetchone()
    )


def audit(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "version": "e4-ledger-integrity-v1",
            "database": str(path),
            "passed": False,
            "findings": [{"severity": "error", "code": "DATABASE_MISSING"}],
        }

    conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    findings: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    try:
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity.lower() != "ok":
            findings.append({"severity": "error", "code": "SQLITE_INTEGRITY_FAILED", "detail": integrity})

        required = ("e4_seen_mints", "e4_positions", "e4_orders", "e4_route_metrics")
        for table in required:
            if not table_exists(conn, table):
                findings.append({"severity": "error", "code": "TABLE_MISSING", "table": table})
        if findings:
            return {
                "version": "e4-ledger-integrity-v1",
                "database": str(path),
                "passed": False,
                "findings": findings,
            }

        orders = list(conn.execute("SELECT * FROM e4_orders ORDER BY created_ns,request_id"))
        positions = list(conn.execute("SELECT * FROM e4_positions ORDER BY opened_ns,position_id"))
        seen = list(conn.execute("SELECT * FROM e4_seen_mints"))
        routes = list(conn.execute("SELECT * FROM e4_route_metrics"))

        signatures: Counter[str] = Counter()
        confirmed_buys: Counter[str] = Counter()
        confirmed_sells: Counter[str] = Counter()
        for row in orders:
            side = str(row["side"] or "").upper()
            counts[f"orders_{side.lower()}"] += 1
            if int(row["confirmed"] or 0):
                counts["confirmed_orders"] += 1
                signature = str(row["signature"] or "")
                if not signature:
                    findings.append(
                        {"severity": "error", "code": "CONFIRMED_ORDER_MISSING_SIGNATURE", "request_id": row["request_id"]}
                    )
                else:
                    signatures[signature] += 1
                if side == "BUY" and row["mint"]:
                    confirmed_buys[str(row["mint"])] += 1
                elif side == "SELL" and row["mint"]:
                    confirmed_sells[str(row["mint"])] += 1
            if float(row["amount"] or 0) <= 0:
                findings.append(
                    {"severity": "error", "code": "NONPOSITIVE_ORDER_AMOUNT", "request_id": row["request_id"]}
                )

        # One logical order signature may appear in several route-metric rows,
        # but it must not back multiple confirmed order records.
        for signature, count in signatures.items():
            if count > 1:
                findings.append(
                    {"severity": "error", "code": "DUPLICATE_CONFIRMED_ORDER_SIGNATURE", "signature": signature, "count": count}
                )

        for mint, count in confirmed_buys.items():
            if count > 1:
                findings.append(
                    {"severity": "error", "code": "MULTIPLE_CONFIRMED_BUYS_PER_MINT", "mint": mint, "count": count}
                )

        position_by_mint = {str(row["mint"]): row for row in positions}
        for mint in confirmed_buys:
            if mint not in position_by_mint:
                findings.append(
                    {"severity": "warning", "code": "CONFIRMED_BUY_WITHOUT_POSITION_ROW", "mint": mint}
                )
        for mint, row in position_by_mint.items():
            if not str(row["entry_signature"] or ""):
                findings.append({"severity": "error", "code": "POSITION_MISSING_ENTRY_SIGNATURE", "mint": mint})
            if float(row["remaining"] or 0) < -1e-12:
                findings.append({"severity": "error", "code": "NEGATIVE_POSITION_REMAINING", "mint": mint})
            if str(row["status"]) == "CLOSED" and float(row["remaining"] or 0) > max(1e-9, float(row["tokens"] or 0) * 1e-8):
                findings.append(
                    {"severity": "warning", "code": "CLOSED_POSITION_HAS_REMAINING_BALANCE", "mint": mint}
                )

        seen_by_mint = {str(row["mint"]): int(row["entry_count"] or 0) for row in seen}
        for mint, count in seen_by_mint.items():
            if count not in {0, 1}:
                findings.append({"severity": "error", "code": "ENTRY_COUNT_OUT_OF_RANGE", "mint": mint, "count": count})
            if confirmed_buys.get(mint, 0) and count != 1:
                findings.append(
                    {"severity": "error", "code": "CONFIRMED_BUY_NOT_MARKED_ENTERED", "mint": mint, "entry_count": count}
                )

        route_requests: Counter[str] = Counter(str(row["request_id"]) for row in routes)
        for row in orders:
            if int(row["confirmed"] or 0) and route_requests[str(row["request_id"])] <= 0:
                findings.append(
                    {"severity": "warning", "code": "CONFIRMED_ORDER_WITHOUT_ROUTE_METRIC", "request_id": row["request_id"]}
                )

        severity_counts = Counter(str(row["severity"]) for row in findings)
        failed = severity_counts["error"] > 0
        return {
            "version": "e4-ledger-integrity-v1",
            "database": str(path),
            "passed": not failed,
            "counts": {
                **dict(counts),
                "positions": len(positions),
                "seen_mints": len(seen),
                "route_metrics": len(routes),
                "findings": len(findings),
            },
            "findings_by_severity": dict(severity_counts),
            "findings": findings,
        }
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, default=Path("data/e4.db"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--no-fail", action="store_true")
    args = parser.parse_args()
    result = audit(args.database)
    body = json.dumps(result, indent=2, sort_keys=True)
    print(body)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(body + "\n", encoding="utf-8")
    return 0 if result["passed"] or args.no_fail else 1


if __name__ == "__main__":
    raise SystemExit(main())
