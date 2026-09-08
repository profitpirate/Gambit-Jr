#!/usr/bin/env python3
"""Memory-bounded wrapper for the all-out 66,000-launch corpus builder."""
from __future__ import annotations

import argparse
import gzip
import heapq
import json
from collections import Counter
from pathlib import Path
from typing import Any

from scripts import e4_v12_allout_launch_corpus as base


class CausalHistory:
    def __init__(self) -> None:
        self.creator_launches: Counter[str] = Counter()
        self.creator_selections: Counter[str] = Counter()
        self.creator_landed: Counter[str] = Counter()
        self.creator_failed: Counter[str] = Counter()
        self.creator_wins: Counter[str] = Counter()
        self.creator_losses: Counter[str] = Counter()
        self.creator_pnl: Counter[str] = Counter()
        self.last_creator_launch: dict[str, int] = {}
        self.last_creator_selection: dict[str, int] = {}
        self.buyer_selections: Counter[str] = Counter()
        self.buyer_wins: Counter[str] = Counter()
        self.creator_buyer_pairs: Counter[tuple[str, str]] = Counter()
        self.uri_counts: Counter[str] = Counter()
        self.name_counts: Counter[str] = Counter()
        self.symbol_counts: Counter[str] = Counter()
        self.pending_outcomes: list[tuple[int, str, bool, float]] = []

    def _settle(self, now: int) -> None:
        while self.pending_outcomes and self.pending_outcomes[0][0] <= now:
            _, creator, won, pnl = heapq.heappop(self.pending_outcomes)
            if won:
                self.creator_wins[creator] += 1
            else:
                self.creator_losses[creator] += 1
            self.creator_pnl[creator] += pnl

    def enrich(self, rows: list[dict[str, Any]]) -> None:
        rows.sort(key=lambda row: (base.integer(row["create_ns"]), str(row["mint"])))
        for row in rows:
            now = base.integer(row["create_ns"])
            self._settle(now)
            creator = str(row.get("creator") or "")
            row["creator_prior_launch_count"] = self.creator_launches[creator]
            row["creator_prior_selection_count"] = self.creator_selections[creator]
            row["creator_prior_landed_count"] = self.creator_landed[creator]
            row["creator_prior_failed_fill_count"] = self.creator_failed[creator]
            row["creator_prior_known_wins"] = self.creator_wins[creator]
            row["creator_prior_known_losses"] = self.creator_losses[creator]
            row["creator_prior_known_pnl_sol"] = self.creator_pnl[creator]
            settled = self.creator_wins[creator] + self.creator_losses[creator]
            row["creator_prior_known_win_rate"] = (
                self.creator_wins[creator] / settled if settled else 0.0
            )
            row["time_since_creator_launch_ms"] = (
                (now - self.last_creator_launch[creator]) / 1_000_000.0
                if creator in self.last_creator_launch
                else None
            )
            row["time_since_creator_selection_ms"] = (
                (now - self.last_creator_selection[creator]) / 1_000_000.0
                if creator in self.last_creator_selection
                else None
            )
            uri = str(row.get("metadata_uri") or "")
            name = str(row.get("name") or "").strip().lower()
            symbol = str(row.get("symbol") or "").strip().lower()
            row["prior_exact_uri_count"] = self.uri_counts[uri] if uri else 0
            row["prior_exact_name_count"] = self.name_counts[name] if name else 0
            row["prior_exact_symbol_count"] = self.symbol_counts[symbol] if symbol else 0

            for window_ms in base.EARLY_WINDOWS_MS:
                buyers = row.get(f"first_outside_buyers_{window_ms}ms") or []
                row[f"known_e4_buyer_count_{window_ms}ms"] = sum(
                    self.buyer_selections[buyer] > 0 for buyer in buyers
                )
                row[f"buyer_prior_selection_sum_{window_ms}ms"] = sum(
                    self.buyer_selections[buyer] for buyer in buyers
                )
                row[f"buyer_prior_win_sum_{window_ms}ms"] = sum(
                    self.buyer_wins[buyer] for buyer in buyers
                )
                row[f"creator_buyer_pair_max_{window_ms}ms"] = max(
                    (self.creator_buyer_pairs[(creator, buyer)] for buyer in buyers),
                    default=0,
                )

            self.creator_launches[creator] += 1
            self.last_creator_launch[creator] = now
            if uri:
                self.uri_counts[uri] += 1
            if name:
                self.name_counts[name] += 1
            if symbol:
                self.symbol_counts[symbol] += 1

            if not row.get("selected_by_e4"):
                continue
            self.creator_selections[creator] += 1
            self.last_creator_selection[creator] = base.integer(row.get("decision_ns"), now)
            if row.get("landed_successfully"):
                self.creator_landed[creator] += 1
            elif row.get("failed_fill_selection"):
                self.creator_failed[creator] += 1
            buyers = (
                row.get("first_outside_buyers_10ms")
                or row.get("first_outside_buyers_100ms")
                or []
            )
            for buyer in buyers:
                self.buyer_selections[buyer] += 1
                self.creator_buyer_pairs[(creator, buyer)] += 1
                if row.get("e4_win") is True:
                    self.buyer_wins[buyer] += 1
            if row.get("e4_pnl_sol") is not None:
                exit_ns = base.integer(row.get("e4_exit_time_s")) * 1_000_000_000
                heapq.heappush(
                    self.pending_outcomes,
                    (
                        max(now, exit_ns),
                        creator,
                        bool(row.get("e4_win")),
                        base.finite(row.get("e4_pnl_sol")),
                    ),
                )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--v2-jsonl", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    labels = base.load_v2_labels(args.v2_jsonl)
    captures = sorted(
        manifest.get("captures") or [],
        key=lambda row: (base.integer(row.get("capture_start_ns")), base.integer(row.get("run_id"))),
    )
    output = args.output_dir
    output.mkdir(parents=True, exist_ok=True)
    corpus_path = output / "e4-v12-allout-launch-corpus.jsonl.gz"
    selected_path = output / "e4-v12-allout-selected-outcomes.jsonl"
    history = CausalHistory()
    source_audit: list[dict[str, Any]] = []
    split_counts: Counter[str] = Counter()
    label_counts: Counter[str] = Counter()
    selected_with_pnl = 0
    observed_pnl = 0.0
    total_rows = 0
    seen_mints: set[str] = set()

    with gzip.open(corpus_path, "wt", encoding="utf-8", compresslevel=6) as corpus, selected_path.open("w", encoding="utf-8") as selected_out:
        for capture in captures:
            run_id = str(capture["run_id"])
            split = str(capture["split"])
            root = args.source_root / run_id
            batches = list(root.rglob("e4-v12-forward-batch.json"))
            event_files = list(root.rglob("e4-v12-forward-batch-live-events.jsonl"))
            if len(batches) != 1 or len(event_files) != 1:
                raise ValueError(f"{run_id}: expected one batch and one live-event file")
            batch_path, events_path = batches[0], event_files[0]
            rows = base.parse_run(events_path, batch_path, run_id, split, labels)
            history.enrich(rows)
            for row in rows:
                mint = str(row["mint"])
                if mint in seen_mints:
                    raise ValueError(f"duplicate mint across captures: {mint}")
                seen_mints.add(mint)
                corpus.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
                if row.get("selected_by_e4"):
                    selected_out.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
                total_rows += 1
                split_counts[split] += 1
                label_counts[str(row["selection_label"])] += 1
                if row.get("e4_pnl_sol") is not None:
                    selected_with_pnl += 1
                    observed_pnl += base.finite(row.get("e4_pnl_sol"))
            source_audit.append(
                {
                    "run_id": run_id,
                    "split": split,
                    "batch_bytes": batch_path.stat().st_size,
                    "batch_sha256": base.sha256_path(batch_path),
                    "events_bytes": events_path.stat().st_size,
                    "events_sha256": base.sha256_path(events_path),
                    "rows": len(rows),
                    "selected": sum(bool(row.get("selected_by_e4")) for row in rows),
                }
            )
            print(json.dumps(source_audit[-1], sort_keys=True), flush=True)
            del rows

    if total_rows != 66_000:
        raise ValueError(f"expected 66,000 rows, got {total_rows}")
    missing_labels = sorted(set(labels) - seen_mints)
    if missing_labels:
        raise ValueError(f"selected labels absent from corpus: {missing_labels[:10]}")

    summary = {
        "version": "e4-v12-allout-launch-corpus-stream-v1",
        "rows": total_rows,
        "capture_runs": len(captures),
        "splits": dict(sorted(split_counts.items())),
        "selection_labels": dict(sorted(label_counts.items())),
        "selected_with_e4_pnl": selected_with_pnl,
        "observed_e4_pnl_sol": observed_pnl,
        "files": {
            corpus_path.name: {
                "bytes": corpus_path.stat().st_size,
                "sha256": base.sha256_path(corpus_path),
            },
            selected_path.name: {
                "bytes": selected_path.stat().st_size,
                "sha256": base.sha256_path(selected_path),
            },
        },
        "source_audit": source_audit,
        "causal_contract": {
            "entry_features": "CREATE plus fixed post-CREATE cutoffs only",
            "history": "strictly prior launches, selections, and settled outcomes",
            "future_outcomes": "explicitly named outcome and paper columns; prohibited from entry-model features",
        },
    }
    base.write_json(output / "e4-v12-allout-launch-corpus-summary.json", summary)
    print(json.dumps({key: value for key, value in summary.items() if key != "source_audit"}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
