#!/usr/bin/env python3
"""Freeze the causal creator/social rule before forward paper-live evaluation."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import e4_v12_holdout_social_backfill as social
import e4_v12_wallet_selection_model as wallet

SCHEMA_VERSION = "e4-v12-prearmed-axiom-frozen-model-v1"


def freeze(root: Path, source: Path, metadata_cache: Path) -> dict[str, Any]:
    source_payload = json.loads(source.read_text(encoding="utf-8"))
    cache = json.loads(metadata_cache.read_text(encoding="utf-8"))
    handles: dict[str, set[str]] = {}
    prior_attempts: Counter[str] = Counter()
    maximum_evidence_ns = 0

    for row in sorted(
        source_payload["rows"],
        key=lambda item: (int(item.get("create_ns") or 0), str(item.get("mint") or "")),
    ):
        creator = str(row.get("creator") or "")
        handle = str(row.get("twitter_handle") or "").lower().lstrip("@")
        maximum_evidence_ns = max(maximum_evidence_ns, int(row.get("create_ns") or 0))
        if creator and handle:
            handles.setdefault(creator, set()).add(handle)
        attempts = int(row.get("prior_creator_attempts") or 0) + int(
            str(row.get("label") or "").upper() != "IGNORED"
        )
        prior_attempts[creator] = max(prior_attempts[creator], attempts)

    rows, traces, audit = wallet.extract_dataset(
        root, include_holdout=True, only_holdout=True
    )
    unique_rows = [row for row in rows if row.horizon_ms == 0]
    uri_by_key: dict[tuple[str, str], str] = {}
    for spec in wallet.base.capture_specs(root, include_holdout=True):
        if spec.split != "holdout":
            continue
        for mint, create in wallet.load_create_rows(spec.events_path).items():
            raw = create.get("raw") if isinstance(create.get("raw"), dict) else {}
            uri_by_key[(spec.run_id, mint)] = str(raw.get("uri") or create.get("uri") or "")

    prior_index = wallet.FEATURE_NAMES.index("creator_prior_e4_attempts")
    for row in sorted(unique_rows, key=lambda item: (item.create_ns, item.run_id, item.mint)):
        trace = traces[(row.run_id, row.mint)]
        maximum_evidence_ns = max(maximum_evidence_ns, row.create_ns)
        uri = uri_by_key.get((row.run_id, row.mint), "")
        metadata = cache.get(uri, {})
        handle = str(
            metadata.get("handle")
            or social.social_handle(str(metadata.get("twitter") or ""))
        ).lower().lstrip("@")
        if trace.creator and handle:
            handles.setdefault(trace.creator, set()).add(handle)
        attempts = int(row.features[prior_index]) + int(row.selected)
        prior_attempts[trace.creator] = max(prior_attempts[trace.creator], attempts)

    eligible = {
        creator: sorted(values)
        for creator, values in sorted(handles.items())
        if creator and values and prior_attempts[creator] >= 1
    }
    output = {
        "schema_version": SCHEMA_VERSION,
        "frozen_at_utc": datetime.now(UTC).isoformat(),
        "maximum_evidence_ns": maximum_evidence_ns,
        "selector": {
            "family": "prearmed_repeat_creator_social_status",
            "minimum_prior_e4_attempts": 1,
            "minimum_creator_seed_sol": 2.0,
            "minimum_tweet_age_seconds": 0.0,
            "maximum_tweet_age_seconds": 10.0,
            "reject_mayhem": True,
            "entry_latency_ms": 5.0,
            "maximum_create_to_entry_price_multiple": 1.50,
            "minimum_entry_output_ratio": 0.65,
        },
        "position_sizing": {
            "starting_bankroll_sol": 3.0,
            "fraction_of_available_cash": 0.10,
            "maximum_concurrent_positions": 2,
        },
        "exit_policy": {
            "stop": 0.70,
            "first_take": 1.15,
            "first_fraction": 0.30,
            "hold_ms": 2_000,
            "trail_retrace": 0.25,
        },
        "execution_costs": {
            "axiom_net_fee_bps_each_side": 95.0,
            "pump_bonding_curve_fee_bps_each_side": 125.0,
            "solana_base_fee_sol_per_transaction": 0.000005,
            "priority_fee_sol_per_transaction": 0.001,
            "mev_bribe_sol_on_buy": 0.001,
            "mev_bribe_sol_on_sell": 0.0,
            "axiom_tier": "Wood / 0.95% net fee",
            "axiom_fee_source": "https://docs.axiom.trade/getting-started/fees/axiom-fees",
            "axiom_network_fee_source": "https://docs.axiom.trade/getting-started/fees/solana-fees",
            "pump_fee_source": "https://pump.fun/docs/fees",
        },
        "acceptance_gate": {
            "closed_trades": 50,
            "minimum_win_rate": 0.65,
            "minimum_wilson_lower_bound": 0.55,
            "minimum_profit_factor": 1.25,
            "minimum_net_pnl_sol": 0.0,
            "maximum_drawdown_fraction": 0.20,
            "minimum_capture_windows": 5,
        },
        "creator_handles": eligible,
        "creator_prior_e4_attempts": {
            creator: prior_attempts[creator] for creator in eligible
        },
        "evidence": {
            "source_intent_sha256": wallet.base.sha256_path(source),
            "metadata_cache_sha256": wallet.base.sha256_path(metadata_cache),
            "holdout_audit": audit,
            "eligible_creators": len(eligible),
            "eligible_creator_handle_edges": sum(len(value) for value in eligible.values()),
        },
        "frozen_before_forward_test": True,
        "production_paths_changed": 0,
    }
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--source",
        type=Path,
        default=Path(
            ".tmp-choice-set-source/research/33889403746/"
            "e4-v12-global-attempt-intent.json"
        ),
    )
    parser.add_argument(
        "--metadata-cache",
        type=Path,
        default=Path(".tmp-e4-v12-holdout-social-metadata.json"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("research/e4-v12-prearmed-axiom-frozen-model.json"),
    )
    args = parser.parse_args()
    root = args.repo_root.resolve()
    payload = freeze(root, root / args.source, root / args.metadata_cache)
    wallet.write_json(root / args.output, payload)
    print(
        json.dumps(
            {
                "output": str(args.output),
                "eligible_creators": payload["evidence"]["eligible_creators"],
                "model_sha256": wallet.base.stable_hash(payload),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
