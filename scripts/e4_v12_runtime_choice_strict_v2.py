#!/usr/bin/env python3
from __future__ import annotations

import random
from collections import defaultdict

import e4_v12_conclusive_entry_rerun as base
from e4_v12_strict_causal_history import add_history_strict

base.add_history = add_history_strict

import e4_v12_conditional_choice_ranker as choice  # noqa: E402

choice.base.add_history = add_history_strict

RUNTIME_FEATURES = [
    "log_seed",
    "log_outside",
    "log_fdv",
    "age_100ms",
    "prior_creator_log",
    "prior_creator_success_log",
    "known_buyer_count",
    "max_prior_buyer_log",
    "sum_prior_buyer_log",
    "max_prior_buyer_success_log",
    "sum_prior_buyer_success_log",
    "max_pair_log",
    "seed_share",
    "first_buyer_age_100ms",
    "second_buyer_age_100ms",
    "interbuyer_100ms",
    "distinct_buy_signatures",
    "max_buys_one_signature",
    "max_buys_one_slot",
    "create_signature_buys",
    "price_multiple_clip",
    "visible_competitors_log",
    "prior_signature_shape_log",
    "outside_per_buyer",
    "buyer_graph_density",
    "buyer_success_density",
    "identity_strength",
    "slot_cluster_strength",
    "launch_velocity",
    "seed_to_fdv",
    "outside_to_fdv",
    "no_public_buyers",
    "one_public_buyer",
    "two_plus_public_buyers",
    "very_early_50ms",
    "very_early_150ms",
    "very_early_400ms",
    "fdv_core_band",
    "seed_roundness",
]
choice.FEATURES = RUNTIME_FEATURES


def build_choice_sets_strict(
    launches,
    failed,
    controls_per_set: int,
    create_window_ms: float,
):
    for launch in launches.values():
        if failed.get(launch.mint):
            launch.failed_attempt = failed[launch.mint][0]

    markers = {launch.mint: base.marker_for(launch) for launch in launches.values()}
    by_run = defaultdict(list)
    for launch in launches.values():
        by_run[launch.run_index].append(launch)
    for rows in by_run.values():
        rows.sort(key=lambda item: item.create_ns)

    sets = []
    all_rows = []
    create_window_ns = int(create_window_ms * 1e6)
    positive_times = defaultdict(list)
    for launch in launches.values():
        marker = markers[launch.mint]
        if marker is not None:
            positive_times[launch.run_index].append(marker[2])

    for launch in sorted(launches.values(), key=lambda item: (item.run_index, item.create_ns)):
        target_value = choice.target_snapshot(launch)
        if target_value is None:
            continue
        target, _ = target_value
        if not base.eligible(target):
            continue
        timestamp = base.integer(target["timestamp_ns"])
        candidates = []
        for control_launch in by_run[launch.run_index]:
            if control_launch.mint == launch.mint:
                continue
            if control_launch.create_ns > timestamp:
                break
            if abs(control_launch.create_ns - launch.create_ns) > create_window_ns:
                continue
            other_marker = markers.get(control_launch.mint)
            # A launch already selected before this decision is not an ignored
            # alternative. A launch selected later *is* a valid "not chosen yet"
            # control and must not disappear through future-label leakage.
            if other_marker is not None and other_marker[2] <= timestamp:
                continue
            control = choice.snapshot_at(control_launch, timestamp)
            if control is None or not base.eligible(control):
                continue
            if base.finite(control.get("age_ms")) > 1_500.0:
                continue
            control["deferred_future_intent"] = bool(other_marker is not None)
            candidates.append((choice.shape_distance(target, control), control))
        candidates.sort(key=lambda pair: pair[0])
        controls = [row for _, row in candidates[:controls_per_set]]
        if not controls:
            continue
        set_id = f"{launch.run_id}:{launch.mint}"
        target["choice_set_id"] = set_id
        target["deferred_future_intent"] = False
        for row in controls:
            row["choice_set_id"] = set_id
        item = choice.ChoiceSet(
            run_index=launch.run_index,
            run_id=launch.run_id,
            timestamp_ns=timestamp,
            target_mint=launch.mint,
            target_label=str(target["label"]),
            candidates=[target, *controls],
        )
        sets.append(item)
        all_rows.extend(item.candidates)

    # Null-choice windows may contain only launches with no E4 intention in the
    # following 1.5 seconds. This prevents a later E4 target being mislabeled as
    # an absolute abstention example.
    rng = random.Random(713)
    for run_index, run_launches in by_run.items():
        buckets = defaultdict(list)
        for launch in run_launches:
            buckets[launch.create_ns // create_window_ns].append(launch)
        bucket_rows = list(buckets.items())
        rng.shuffle(bucket_rows)
        target_nulls = max(1, len(positive_times[run_index]))
        added = 0
        for _, group in bucket_rows:
            if added >= target_nulls:
                break
            timestamp = max(item.create_ns for item in group) + min(create_window_ns, 400_000_000)
            if any(abs(timestamp - intent_time) <= create_window_ns for intent_time in positive_times[run_index]):
                continue
            candidates = []
            for launch in group:
                marker = markers.get(launch.mint)
                if marker is not None and marker[2] <= timestamp + 1_500_000_000:
                    continue
                row = choice.snapshot_at(launch, timestamp)
                if row is None or not base.eligible(row):
                    continue
                row["choice_set_id"] = f"{launch.run_id}:NULL:{timestamp}"
                row["deferred_future_intent"] = bool(marker is not None)
                candidates.append(row)
            if len(candidates) < 2:
                continue
            candidates.sort(
                key=lambda row: (
                    base.log1p(row.get("creator_seed_sol"))
                    + base.integer(row.get("unique_buyers"))
                    + 0.5 * base.integer(row.get("same_slot_buys"))
                ),
                reverse=True,
            )
            candidates = candidates[:controls_per_set]
            item = choice.ChoiceSet(
                run_index=run_index,
                run_id=candidates[0]["run_id"],
                timestamp_ns=timestamp,
                target_mint=None,
                target_label="NO_CHOICE",
                candidates=candidates,
            )
            sets.append(item)
            all_rows.extend(candidates)
            added += 1

    return sets, all_rows


choice.build_choice_sets = build_choice_sets_strict

if __name__ == "__main__":
    raise SystemExit(choice.main())
