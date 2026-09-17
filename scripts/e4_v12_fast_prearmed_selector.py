#!/usr/bin/env python3
"""Research-only constant-time selector for the pre-armed E4 social cohort."""

from __future__ import annotations

import statistics
import time
from collections.abc import Mapping
from collections.abc import Set as AbstractSet
from typing import Any

MAX_TWEET_AGE_NS = 10_000_000_000
MINIMUM_CREATOR_SEED_SOL = 2.0


def should_select(
    *,
    creator: str,
    social_handle: str,
    social_status_ns: int,
    create_ns: int,
    prior_e4_attempts: int,
    creator_seed_sol: float,
    mayhem_mode: bool,
    creator_handles: Mapping[str, AbstractSet[str]],
) -> bool:
    """Decide using two hash lookups and integer comparisons; never perform I/O."""
    if (
        mayhem_mode
        or prior_e4_attempts < 1
        or creator_seed_sol < MINIMUM_CREATOR_SEED_SOL
        or not social_handle
    ):
        return False
    age_ns = create_ns - social_status_ns
    if age_ns < 0 or age_ns > MAX_TWEET_AGE_NS:
        return False
    return social_handle in creator_handles.get(creator, ())


def benchmark(iterations: int = 100_000) -> dict[str, Any]:
    creator_handles = {"creator": {"known_handle"}}
    timings = []
    for _ in range(iterations):
        started = time.perf_counter_ns()
        should_select(
            creator="creator",
            social_handle="known_handle",
            social_status_ns=1_000_000_000,
            create_ns=2_000_000_000,
            prior_e4_attempts=1,
            creator_seed_sol=2.0,
            mayhem_mode=False,
            creator_handles=creator_handles,
        )
        timings.append(time.perf_counter_ns() - started)
    timings.sort()
    return {
        "iterations": iterations,
        "median_microseconds": statistics.median(timings) / 1_000,
        "p95_microseconds": timings[int(iterations * 0.95)] / 1_000,
        "p99_microseconds": timings[int(iterations * 0.99)] / 1_000,
        "hot_path_io_operations": 0,
        "hot_path_model_allocations": 0,
    }


if __name__ == "__main__":
    import json

    print(json.dumps(benchmark(), indent=2))
