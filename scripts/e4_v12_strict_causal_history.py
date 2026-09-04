from __future__ import annotations

from collections import Counter
from typing import Any, Mapping


def _integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def add_history_strict(
    rows: list[dict[str, Any]],
    static_history: Mapping[str, Mapping[str, float]],
) -> None:
    """Attach only information observed before each decision timestamp.

    The static creator registry is intentionally ignored for thesis discovery.
    It contains aggregate outcomes that can post-date early research samples and
    would leak future creator wins backwards. Runtime-equivalent history is
    rebuilt from earlier successful and failed E4 buy intentions only.
    """
    del static_history
    creator_attempts: Counter[str] = Counter()
    creator_successes: Counter[str] = Counter()
    creator_failures: Counter[str] = Counter()
    handle_attempts: Counter[str] = Counter()
    buyer_attempts: Counter[str] = Counter()
    buyer_successes: Counter[str] = Counter()
    creator_buyer_attempts: Counter[tuple[str, str]] = Counter()

    ordered = sorted(
        rows,
        key=lambda row: (_integer(row.get("timestamp_ns")), str(row.get("mint") or "")),
    )
    cursor = 0
    while cursor < len(ordered):
        timestamp = _integer(ordered[cursor].get("timestamp_ns"))
        end = cursor + 1
        while end < len(ordered) and _integer(ordered[end].get("timestamp_ns")) == timestamp:
            end += 1
        group = ordered[cursor:end]

        for row in group:
            creator = str(row.get("creator") or "")
            handle = str(row.get("twitter_handle") or "")
            buyers = [str(value) for value in row.get("first_buyers") or []]
            attempts = creator_attempts[creator] if creator else 0
            successes = creator_successes[creator] if creator else 0
            failures = creator_failures[creator] if creator else 0
            buyer_counts = [buyer_attempts[value] for value in buyers]
            buyer_success = [buyer_successes[value] for value in buyers]
            pair_counts = [creator_buyer_attempts[(creator, value)] for value in buyers]
            row.update(
                {
                    "hist_wins": successes,
                    "hist_losses": failures,
                    "hist_trades": attempts,
                    "hist_rate": successes / attempts if attempts else 0.0,
                    "prior_creator_attempts": attempts,
                    "prior_creator_successes": successes,
                    "prior_creator_failures": failures,
                    "prior_handle_attempts": handle_attempts[handle] if handle else 0,
                    "known_buyer_count": sum(value > 0 for value in buyer_counts),
                    "max_prior_buyer_attempts": max(buyer_counts, default=0),
                    "sum_prior_buyer_attempts": sum(buyer_counts),
                    "max_prior_buyer_successes": max(buyer_success, default=0),
                    "sum_prior_buyer_successes": sum(buyer_success),
                    "max_creator_buyer_pair_attempts": max(pair_counts, default=0),
                }
            )

        # Equal-timestamp decisions cannot teach one another. Apply all new
        # observations only after the entire timestamp group has been scored.
        for row in group:
            if not bool(row.get("positive")):
                continue
            creator = str(row.get("creator") or "")
            handle = str(row.get("twitter_handle") or "")
            label = str(row.get("label") or "")
            if creator:
                creator_attempts[creator] += 1
                creator_successes[creator] += int(label == "SUCCESS")
                creator_failures[creator] += int(label == "FAILED_ATTEMPT")
            if handle:
                handle_attempts[handle] += 1
            for buyer in row.get("first_buyers") or []:
                buyer_key = str(buyer)
                buyer_attempts[buyer_key] += 1
                buyer_successes[buyer_key] += int(label == "SUCCESS")
                if creator:
                    creator_buyer_attempts[(creator, buyer_key)] += 1
        cursor = end
