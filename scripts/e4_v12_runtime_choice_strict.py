#!/usr/bin/env python3
from __future__ import annotations

import e4_v12_conclusive_entry_rerun as base
from e4_v12_strict_causal_history import add_history_strict

base.add_history = add_history_strict

import e4_v12_conditional_choice_ranker as choice  # noqa: E402

choice.base.add_history = add_history_strict

# Only information physically available on the primary launch stream before
# E4's transaction may enter this model. Token-metadata HTTP fetches, tweet-age
# extraction performed after capture and future creator outcomes are excluded.
choice.FEATURES = [
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

if __name__ == "__main__":
    raise SystemExit(choice.main())
