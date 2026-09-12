#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from scripts import e4_v12_true_latency_replay as economics


def load_events(events_path: Path) -> dict[str, list[dict[str, Any]]]:
    """Load captured JSONL events grouped by mint in causal arrival order.

    The golden-thesis search historically called economics.load_events(), but
    the true-latency replay module no longer exposed that helper.  Keep this
    compatibility shim outside the research/model code so the frozen thesis is
    not altered while restoring the historical and untouched-live pipeline.
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    with Path(events_path).open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            mint = str(row.get("mint") or "")
            if mint:
                grouped.setdefault(mint, []).append(row)

    for rows in grouped.values():
        rows.sort(key=economics.event_sort_key)
        for sequence, row in enumerate(rows):
            row["__sequence"] = sequence
    return grouped


def main() -> int:
    # Patch only the missing compatibility API.  Search/apply logic and frozen
    # thesis parameters remain untouched.
    economics.load_events = load_events
    from scripts import e4_v12_social_prearm_search as social

    return int(social.main())


if __name__ == "__main__":
    raise SystemExit(main())
