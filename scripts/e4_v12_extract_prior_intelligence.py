#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

PUBKEY_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{30,50}$")


def integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def pubkey(value: Any) -> str:
    text = str(value or "").strip()
    return text if PUBKEY_RE.match(text) else ""


def timestamp_ns(row: Mapping[str, Any]) -> int:
    for key in (
        "received_ns", "observed_ns", "entry_ns", "decision_ns", "attempt_ns",
        "timestamp_ns", "created_ns", "launch_ns",
    ):
        value = integer(row.get(key))
        if value > 10**17:
            return value
    for key in (
        "entry_time", "timestamp", "time", "created_at_epoch", "observed_at_epoch",
        "generated_at_epoch", "attempt_time",
    ):
        value = finite(row.get(key))
        if value > 1_000_000_000:
            return int(value * 1e9)
    return 0


def walk(value: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        yield value
        for item in value.values():
            yield from walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk(item)


def creator_of(row: Mapping[str, Any]) -> str:
    for key in (
        "creator", "creator_address", "creator_wallet", "deployer",
        "deployer_address", "launch_creator",
    ):
        value = pubkey(row.get(key))
        if value:
            return value
    return ""


def funder_of(row: Mapping[str, Any]) -> str:
    for key in (
        "funder", "funder_wallet", "funding_wallet", "source_wallet",
        "funded_by", "origin_wallet", "parent_wallet",
    ):
        value = pubkey(row.get(key))
        if value:
            return value
    return ""


def mint_of(row: Mapping[str, Any]) -> str:
    for key in ("mapped_mint", "mint", "token", "token_address", "contract_address"):
        value = pubkey(row.get(key))
        if value:
            return value
    return ""


def stats(row: Mapping[str, Any]) -> tuple[int, int, int]:
    wins = max(
        integer(row.get("wins")),
        integer(row.get("e4_observed_wins")),
        integer(row.get("gross_wins")),
        integer(row.get("successful_trades")),
    )
    losses = max(
        integer(row.get("losses")),
        integer(row.get("e4_observed_losses")),
        integer(row.get("gross_losses")),
        integer(row.get("failed_trades")),
    )
    trades = max(
        integer(row.get("trades")),
        integer(row.get("samples")),
        integer(row.get("observed_trades")),
        wins + losses,
    )
    return wins, losses, trades


def metadata_record(row: Mapping[str, Any]) -> dict[str, Any] | None:
    mint = mint_of(row)
    if not mint:
        return None
    keys = {
        "twitter_handle", "twitter_status_id", "tweet_age_seconds", "tweet_time_ns",
        "metadata_ok", "website_present", "twitter", "uri", "metadata_host",
    }
    if not any(key in row for key in keys):
        return None
    return {
        "mint": mint,
        "twitter_handle": str(row.get("twitter_handle") or "").lower().lstrip("@"),
        "twitter_status_id": str(row.get("twitter_status_id") or ""),
        "tweet_age_seconds": row.get("tweet_age_seconds"),
        "tweet_time_ns": row.get("tweet_time_ns"),
        "metadata_ok": bool(row.get("metadata_ok")),
        "website_present": bool(row.get("website_present")),
        "twitter": str(row.get("twitter") or ""),
        "uri": str(row.get("uri") or ""),
        "metadata_host": str(row.get("metadata_host") or ""),
        "observed_ns": timestamp_ns(row),
    }


def failed_attempt_record(row: Mapping[str, Any]) -> dict[str, Any] | None:
    mint = mint_of(row)
    if not mint:
        return None
    keys = set(row)
    looks_failed = bool(
        "attempt_slot" in keys
        or "mapping_ok" in keys
        or "failed_reason" in keys
        or str(row.get("intent_label") or "").upper() == "FAILED_ATTEMPT"
        or str(row.get("status") or "").lower() in {"failed", "rejected", "error"}
    )
    if not looks_failed:
        return None
    if row.get("mapping_ok") is False:
        return None
    return {
        "mint": mint,
        "signature": str(row.get("signature") or row.get("attempt_signature") or ""),
        "attempt_slot": integer(row.get("attempt_slot") or row.get("slot")),
        "attempt_transaction_index": integer(
            row.get("attempt_transaction_index") or row.get("transaction_index"), -1
        ),
        "attempt_ns": timestamp_ns(row),
        "error": str(row.get("error") or row.get("failed_reason") or row.get("reason") or ""),
        "expected_tokens": finite(row.get("expected_tokens") or row.get("minimum_tokens")),
        "available_tokens": finite(row.get("available_tokens") or row.get("actual_tokens")),
        "priority_fee_sol": finite(row.get("priority_fee_sol")),
        "source": str(row.get("source") or ""),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Consolidate previous E4 research evidence")
    parser.add_argument("--input", action="append", default=[], type=Path)
    parser.add_argument("--cutoff-ns", type=int, default=0)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    files: list[Path] = []
    for root in args.input:
        if root.is_file():
            files.append(root)
        elif root.exists():
            files.extend(root.rglob("*.json"))
    creators: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "wins": 0, "losses": 0, "trades": 0, "sources": set(),
    })
    funders: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "creators": set(), "observations": 0, "first_observed_ns": 0, "sources": set(),
    })
    creator_funders: dict[str, dict[str, Any]] = {}
    failed_by_key: dict[tuple[str, str, int], dict[str, Any]] = {}
    metadata_by_mint: dict[str, dict[str, Any]] = {}
    parsed = 0

    for path in files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        parsed += 1
        for row in walk(payload):
            observed = timestamp_ns(row)
            if args.cutoff_ns and observed and observed >= args.cutoff_ns:
                continue
            creator = creator_of(row)
            wins, losses, trades = stats(row)
            if creator and (wins > 0 or losses > 0):
                value = creators[creator]
                value["wins"] = max(value["wins"], wins)
                value["losses"] = max(value["losses"], losses)
                value["trades"] = max(value["trades"], trades, wins + losses)
                value["sources"].add(str(path))
            funder = funder_of(row)
            if creator and funder and creator != funder:
                relation = {
                    "creator": creator,
                    "funder": funder,
                    "observed_ns": observed,
                    "source": str(path),
                }
                previous = creator_funders.get(creator)
                if previous is None or (
                    observed and (not previous.get("observed_ns") or observed < previous["observed_ns"])
                ):
                    creator_funders[creator] = relation
                value = funders[funder]
                value["creators"].add(creator)
                value["observations"] += 1
                value["sources"].add(str(path))
                if observed and (
                    not value["first_observed_ns"] or observed < value["first_observed_ns"]
                ):
                    value["first_observed_ns"] = observed
            failed = failed_attempt_record(row)
            if failed is not None:
                key = (failed["mint"], failed["signature"], failed["attempt_slot"])
                failed_by_key[key] = failed
            meta = metadata_record(row)
            if meta is not None:
                previous = metadata_by_mint.get(meta["mint"])
                if previous is None or sum(bool(value) for value in meta.values()) > sum(
                    bool(value) for value in previous.values()
                ):
                    metadata_by_mint[meta["mint"]] = meta

    creator_output = {}
    for creator, row in creators.items():
        trades = max(row["trades"], row["wins"] + row["losses"])
        creator_output[creator] = {
            "wins": row["wins"],
            "losses": row["losses"],
            "trades": trades,
            "win_rate": row["wins"] / trades if trades else 0.0,
            "sources": sorted(row["sources"]),
        }
    funder_output = {
        funder: {
            "creators": sorted(row["creators"]),
            "creator_count": len(row["creators"]),
            "observations": row["observations"],
            "first_observed_ns": row["first_observed_ns"],
            "sources": sorted(row["sources"]),
        }
        for funder, row in funders.items()
    }
    payload = {
        "version": "e4-v12-prior-intelligence-cache-v1",
        "cutoff_ns": args.cutoff_ns or None,
        "input_roots": [str(path) for path in args.input],
        "json_files_seen": len(files),
        "json_files_parsed": parsed,
        "creators": dict(sorted(creator_output.items())),
        "creator_funders": dict(sorted(creator_funders.items())),
        "funders": dict(sorted(funder_output.items())),
        "failed_attempts": sorted(
            failed_by_key.values(),
            key=lambda row: (row["attempt_ns"], row["attempt_slot"], row["mint"]),
        ),
        "metadata": dict(sorted(metadata_by_mint.items())),
        "coverage": {
            "creator_count": len(creator_output),
            "creator_funder_count": len(creator_funders),
            "funder_count": len(funder_output),
            "failed_attempt_count": len(failed_by_key),
            "metadata_count": len(metadata_by_mint),
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(payload["coverage"], indent=2, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
