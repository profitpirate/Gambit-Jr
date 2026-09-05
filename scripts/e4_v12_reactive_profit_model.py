#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import statistics
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

import joblib
import numpy as np
from sklearn.ensemble import ExtraTreesClassifier, HistGradientBoostingClassifier, RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

# Patches the shared replay primitive so reactive candidates are protected by
# E4's observed token output scaled to Gambit's own SOL input.
from scripts import e4_v12_true_latency_replay_v2  # noqa: F401
from scripts import e4_v12_golden_thesis_search as golden
from scripts import e4_v12_true_latency_replay as economics

FEATURES = [
    "log_source_sol",
    "log_source_tokens",
    "log_fdv",
    "entry_age_100ms",
    "source_curve_share",
    "source_price_impact_bps_scaled",
    "source_average_price_log",
    "post_marginal_price_log",
    "pre_virtual_sol_log",
    "pre_virtual_tokens_log",
    "creator_seed_log",
    "outside_sol_log",
    "pre_buy_count",
    "pre_unique_buyers",
    "pre_same_slot_buys",
    "pre_same_slot_unique",
    "pre_distinct_signatures",
    "pre_max_buys_one_signature",
    "pre_create_signature_buys",
    "pre_seed_share",
    "pre_price_multiple_clip",
    "source_buy_rank_inverse",
    "prior_creator_attempts_log",
    "prior_creator_wins_log",
    "prior_creator_losses_log",
    "prior_creator_win_rate",
    "known_buyer_count",
    "max_prior_buyer_attempts_log",
    "sum_prior_buyer_attempts_log",
    "max_prior_buyer_wins_log",
    "sum_prior_buyer_wins_log",
    "max_creator_buyer_pair_log",
    "identity_strength",
    "source_size_to_fdv",
    "creator_seed_to_fdv",
    "outside_to_fdv",
    "source_small_1_5",
    "source_small_2_5",
    "source_medium_5",
    "impact_under_500",
    "impact_under_800",
    "impact_under_1200",
    "fdv_core_band",
    "first_buyer_age_100ms",
    "second_buyer_age_100ms",
    "interbuyer_100ms",
]


def finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def log1p(value: Any) -> float:
    return math.log1p(max(0.0, finite(value)))


@dataclass
class Memory:
    creator_attempts: Counter[str] = field(default_factory=Counter)
    creator_wins: Counter[str] = field(default_factory=Counter)
    creator_losses: Counter[str] = field(default_factory=Counter)
    buyer_attempts: Counter[str] = field(default_factory=Counter)
    buyer_wins: Counter[str] = field(default_factory=Counter)
    pairs: Counter[str] = field(default_factory=Counter)

    def observe(self, launch: golden.Launch, buyers: Sequence[str], won: bool) -> None:
        creator = launch.creator
        if creator:
            self.creator_attempts[creator] += 1
            (self.creator_wins if won else self.creator_losses)[creator] += 1
        for buyer in buyers:
            if not buyer:
                continue
            self.buyer_attempts[buyer] += 1
            if won:
                self.buyer_wins[buyer] += 1
            if creator:
                self.pairs[f"{creator}|{buyer}"] += 1


@dataclass(frozen=True)
class Spec:
    family: str
    depth: int
    leaf: int
    estimators: int

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass(frozen=True)
class Gate:
    threshold: float
    max_output_shortfall_bps: int
    maximum_source_impact_bps: float
    minimum_prior_identity: float

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def pre_source_state(launch: golden.Launch) -> tuple[golden.LaunchState, list[str]]:
    state = golden.LaunchState(latest_ns=launch.create_ns)
    for event in launch.events:
        if launch.e4_buy is not None and economics.event_order(event) >= economics.event_order(launch.e4_buy):
            break
        if str(event.get("trader") or "") == economics.E4_WALLET:
            continue
        golden.apply_event(launch, state, event)
    return state, list(state.first_buyers)


def source_feature_row(run: golden.RunData, launch: golden.Launch, memory: Memory) -> dict[str, Any] | None:
    buy = launch.e4_buy
    position = launch.e4_position
    if buy is None or position is None:
        return None
    source_sol = max(0.0, finite(buy.get("sol_amount")))
    source_tokens = max(0.0, finite(buy.get("token_amount")))
    if source_sol <= 0 or source_tokens <= 0:
        return None
    buy_ns = integer(buy.get("received_ns"))
    states = economics.reserve_states(run.grouped.get(launch.mint, ()))
    post = economics.state_at_or_before(states, buy_ns) or economics.state_at_or_after(states, buy_ns)
    if post is None:
        return None
    pre_sol = max(1e-12, post.virtual_sol - source_sol)
    pre_tokens = max(1e-12, post.virtual_tokens + source_tokens)
    source_average_price = source_sol / source_tokens
    post_marginal_price = post.virtual_sol / max(post.virtual_tokens, 1e-12)
    source_impact_bps = max(
        -100_000.0,
        min(100_000.0, (post_marginal_price / max(source_average_price, 1e-18) - 1.0) * 10_000.0),
    )
    state, buyers = pre_source_state(launch)
    creator = launch.creator
    attempts = memory.creator_attempts[creator]
    wins = memory.creator_wins[creator]
    losses = memory.creator_losses[creator]
    buyer_attempts = [memory.buyer_attempts[buyer] for buyer in buyers]
    buyer_wins = [memory.buyer_wins[buyer] for buyer in buyers]
    pair_attempts = [memory.pairs[f"{creator}|{buyer}"] for buyer in buyers]
    first_age = (
        (state.first_buyer_ns[0] - launch.create_ns) / 1_000_000.0
        if state.first_buyer_ns
        else 9_999.0
    )
    second_age = (
        (state.first_buyer_ns[1] - launch.create_ns) / 1_000_000.0
        if len(state.first_buyer_ns) > 1
        else 9_999.0
    )
    interbuyer = (
        statistics.median(
            (right - left) / 1_000_000.0
            for left, right in zip(state.first_buyer_ns, state.first_buyer_ns[1:])
        )
        if len(state.first_buyer_ns) > 1
        else 9_999.0
    )
    fdv = max(1.0, finite(buy.get("fdv_usd"), post.fdv_usd))
    settled = wins + losses
    identity_strength = (
        1.50 * log1p(attempts)
        + 1.25 * log1p(wins)
        - 0.75 * log1p(losses)
        + log1p(sum(buyer_attempts))
        + 0.75 * log1p(sum(buyer_wins))
        + 0.75 * log1p(max(pair_attempts, default=0))
    )
    won = finite(position.get("pnl_sol")) > 0
    return {
        "mint": launch.mint,
        "run_id": run.run_id,
        "run_index": run.run_index,
        "decision_ns": buy_ns,
        "requested_fraction": 0.0185,
        "score": 0.99,
        "mode": "v12_reactive_profit_guard",
        "source_sol": source_sol,
        "source_tokens": source_tokens,
        "entry_fdv_usd": fdv,
        "source_curve_share": source_sol / pre_sol,
        "source_price_impact_bps": source_impact_bps,
        "source_average_price": source_average_price,
        "post_marginal_price": post_marginal_price,
        "pre_virtual_sol": pre_sol,
        "pre_virtual_tokens": pre_tokens,
        "entry_age_ms": max(0.0, (buy_ns - launch.create_ns) / 1_000_000.0),
        "creator_seed_sol": state.creator_seed_sol,
        "outside_sol": state.outside_sol,
        "pre_buy_count": state.buy_count,
        "pre_unique_buyers": len(state.unique_buyers),
        "pre_same_slot_buys": state.same_slot_buys,
        "pre_same_slot_unique": len(state.same_slot_unique),
        "pre_distinct_signatures": len(state.buy_signatures),
        "pre_max_buys_one_signature": max(state.buy_signatures.values(), default=0),
        "pre_create_signature_buys": state.buy_signatures.get(launch.create_signature, 0),
        "pre_seed_share": state.creator_seed_sol / max(1e-12, state.creator_seed_sol + state.outside_sol),
        "pre_price_multiple": (
            state.price_sol / state.initial_price_sol
            if state.price_sol > 0 and state.initial_price_sol > 0
            else 1.0
        ),
        "first_buyer_age_ms": first_age,
        "second_buyer_age_ms": second_age,
        "interbuyer_ms": interbuyer,
        "prior_creator_attempts": attempts,
        "prior_creator_wins": wins,
        "prior_creator_losses": losses,
        "prior_creator_win_rate": wins / settled if settled else 0.0,
        "known_buyer_count": sum(value > 0 for value in buyer_attempts),
        "max_prior_buyer_attempts": max(buyer_attempts, default=0),
        "sum_prior_buyer_attempts": sum(buyer_attempts),
        "max_prior_buyer_wins": max(buyer_wins, default=0),
        "sum_prior_buyer_wins": sum(buyer_wins),
        "max_creator_buyer_pair": max(pair_attempts, default=0),
        "identity_strength": identity_strength,
        "e4_won": won,
        "e4_pnl_sol": finite(position.get("pnl_sol")),
    }


def build_rows(runs: Sequence[golden.RunData]) -> list[dict[str, Any]]:
    memory = Memory()
    output: list[dict[str, Any]] = []
    for run in runs:
        current: list[dict[str, Any]] = []
        launches = sorted(
            run.launches.values(),
            key=lambda launch: (
                integer(launch.e4_buy.get("received_ns")) if launch.e4_buy is not None else 2**63 - 1,
                launch.mint,
            ),
        )
        for launch in launches:
            row = source_feature_row(run, launch, memory)
            if row is not None:
                current.append(row)
        output.extend(current)
        # Outcomes are made available only after the whole run. This is more
        # conservative than production and prevents a later trade from learning
        # the result of a position that may still have been open at its entry.
        for row in current:
            launch = run.launches[str(row["mint"])]
            _, buyers = pre_source_state(launch)
            memory.observe(launch, buyers, bool(row["e4_won"]))
        print(json.dumps({"run_id": run.run_id, "reactive_profit_rows": len(current)}), flush=True)
    return output


def feature_values(row: Mapping[str, Any]) -> dict[str, float]:
    fdv = max(1.0, finite(row.get("entry_fdv_usd")))
    source_sol = max(0.0, finite(row.get("source_sol")))
    impact = finite(row.get("source_price_impact_bps"))
    values = {
        "log_source_sol": log1p(source_sol),
        "log_source_tokens": log1p(row.get("source_tokens")),
        "log_fdv": log1p(fdv),
        "entry_age_100ms": min(100.0, max(0.0, finite(row.get("entry_age_ms"))) / 100.0),
        "source_curve_share": finite(row.get("source_curve_share")),
        "source_price_impact_bps_scaled": max(-10.0, min(20.0, impact / 1_000.0)),
        "source_average_price_log": math.log(max(1e-18, finite(row.get("source_average_price"), 1e-18))),
        "post_marginal_price_log": math.log(max(1e-18, finite(row.get("post_marginal_price"), 1e-18))),
        "pre_virtual_sol_log": log1p(row.get("pre_virtual_sol")),
        "pre_virtual_tokens_log": log1p(row.get("pre_virtual_tokens")),
        "creator_seed_log": log1p(row.get("creator_seed_sol")),
        "outside_sol_log": log1p(row.get("outside_sol")),
        "pre_buy_count": finite(row.get("pre_buy_count")),
        "pre_unique_buyers": finite(row.get("pre_unique_buyers")),
        "pre_same_slot_buys": finite(row.get("pre_same_slot_buys")),
        "pre_same_slot_unique": finite(row.get("pre_same_slot_unique")),
        "pre_distinct_signatures": finite(row.get("pre_distinct_signatures")),
        "pre_max_buys_one_signature": finite(row.get("pre_max_buys_one_signature")),
        "pre_create_signature_buys": finite(row.get("pre_create_signature_buys")),
        "pre_seed_share": finite(row.get("pre_seed_share")),
        "pre_price_multiple_clip": min(10.0, max(0.0, finite(row.get("pre_price_multiple"), 1.0))),
        "source_buy_rank_inverse": 1.0 / max(1.0, finite(row.get("pre_buy_count")) + 1.0),
        "prior_creator_attempts_log": log1p(row.get("prior_creator_attempts")),
        "prior_creator_wins_log": log1p(row.get("prior_creator_wins")),
        "prior_creator_losses_log": log1p(row.get("prior_creator_losses")),
        "prior_creator_win_rate": finite(row.get("prior_creator_win_rate")),
        "known_buyer_count": finite(row.get("known_buyer_count")),
        "max_prior_buyer_attempts_log": log1p(row.get("max_prior_buyer_attempts")),
        "sum_prior_buyer_attempts_log": log1p(row.get("sum_prior_buyer_attempts")),
        "max_prior_buyer_wins_log": log1p(row.get("max_prior_buyer_wins")),
        "sum_prior_buyer_wins_log": log1p(row.get("sum_prior_buyer_wins")),
        "max_creator_buyer_pair_log": log1p(row.get("max_creator_buyer_pair")),
        "identity_strength": finite(row.get("identity_strength")),
        "source_size_to_fdv": source_sol / fdv * 10_000.0,
        "creator_seed_to_fdv": finite(row.get("creator_seed_sol")) / fdv * 10_000.0,
        "outside_to_fdv": finite(row.get("outside_sol")) / fdv * 10_000.0,
        "source_small_1_5": float(source_sol <= 1.5),
        "source_small_2_5": float(source_sol <= 2.5),
        "source_medium_5": float(source_sol <= 5.0),
        "impact_under_500": float(impact <= 500.0),
        "impact_under_800": float(impact <= 800.0),
        "impact_under_1200": float(impact <= 1_200.0),
        "fdv_core_band": float(3_000.0 <= fdv <= 8_500.0),
        "first_buyer_age_100ms": min(100.0, finite(row.get("first_buyer_age_ms"), 9_999.0) / 100.0),
        "second_buyer_age_100ms": min(100.0, finite(row.get("second_buyer_age_ms"), 9_999.0) / 100.0),
        "interbuyer_100ms": min(100.0, finite(row.get("interbuyer_ms"), 9_999.0) / 100.0),
    }
    return {name: finite(values.get(name)) for name in FEATURES}


def matrix(rows: Sequence[Mapping[str, Any]]) -> np.ndarray:
    return np.asarray([[feature_values(row)[name] for name in FEATURES] for row in rows], dtype=float)


def specs() -> list[Spec]:
    return [
        Spec("logit", 0, 0, 0),
        Spec("extra", 3, 3, 200),
        Spec("extra", 4, 4, 240),
        Spec("extra", 5, 5, 280),
        Spec("forest", 3, 3, 220),
        Spec("forest", 4, 4, 260),
        Spec("hist", 3, 5, 0),
    ]


def fit(rows: Sequence[Mapping[str, Any]], spec: Spec):
    x = matrix(rows)
    y = np.asarray([int(bool(row.get("e4_won"))) for row in rows], dtype=int)
    if len(set(y.tolist())) < 2:
        raise RuntimeError("both outcome classes are required")
    if spec.family == "logit":
        model = Pipeline(
            [
                ("scale", StandardScaler()),
                ("model", LogisticRegression(C=0.2, class_weight="balanced", max_iter=5_000, solver="liblinear", random_state=712)),
            ]
        )
        model.fit(x, y)
        return model
    if spec.family == "extra":
        model = ExtraTreesClassifier(
            n_estimators=spec.estimators,
            max_depth=spec.depth,
            min_samples_leaf=spec.leaf,
            max_features="sqrt",
            class_weight="balanced_subsample",
            random_state=712,
            n_jobs=-1,
        )
        model.fit(x, y)
        return model
    if spec.family == "forest":
        model = RandomForestClassifier(
            n_estimators=spec.estimators,
            max_depth=spec.depth,
            min_samples_leaf=spec.leaf,
            max_features="sqrt",
            class_weight="balanced_subsample",
            random_state=712,
            n_jobs=-1,
        )
        model.fit(x, y)
        return model
    if spec.family == "hist":
        positives = max(1, int(y.sum()))
        weights = np.where(y == 1, max(1.0, (len(y) - positives) / positives), 1.0)
        model = HistGradientBoostingClassifier(
            max_iter=160,
            learning_rate=0.04,
            max_depth=spec.depth,
            min_samples_leaf=spec.leaf,
            l2_regularization=3.0,
            random_state=712,
        )
        model.fit(x, y, sample_weight=weights)
        return model
    raise ValueError(spec.family)


def probability(model: Any, rows: Sequence[Mapping[str, Any]]) -> np.ndarray:
    if not rows:
        return np.asarray([], dtype=float)
    return model.predict_proba(matrix(rows))[:, 1]


def choose(rows: Sequence[Mapping[str, Any]], model: Any, gate: Gate) -> list[dict[str, Any]]:
    scores = probability(model, rows)
    selected: list[dict[str, Any]] = []
    for row, score in zip(rows, scores):
        if float(score) < gate.threshold:
            continue
        if finite(row.get("source_price_impact_bps")) > gate.maximum_source_impact_bps:
            continue
        identity = max(
            finite(row.get("prior_creator_attempts")),
            finite(row.get("known_buyer_count")),
            finite(row.get("max_creator_buyer_pair")),
        )
        if identity < gate.minimum_prior_identity:
            continue
        item = dict(row)
        item["score"] = float(score)
        item["requested_fraction"] = 0.0185
        item["mode"] = "v12_reactive_profit_guard"
        selected.append(item)
    selected.sort(key=lambda row: (integer(row.get("decision_ns")), str(row.get("mint"))))
    return selected


def candidate_gates(validation_probabilities: np.ndarray) -> Sequence[Gate]:
    thresholds = sorted(
        set(
            float(np.quantile(validation_probabilities, q))
            for q in (0.35, 0.45, 0.55, 0.65, 0.72, 0.78, 0.84, 0.88, 0.92, 0.95)
        )
    ) if len(validation_probabilities) else [1.0]
    return [
        Gate(threshold, guard, impact, identity)
        for threshold in thresholds
        for guard in (300, 500, 800, 1_000, 1_250, 1_500, 2_000, 2_500)
        for impact in (400.0, 600.0, 800.0, 1_000.0, 1_250.0, 1_500.0, 2_000.0, 3_000.0, 100_000.0)
        for identity in (0.0, 1.0, 2.0)
    ]


def all_latency_pass(
    blocks: Mapping[str, Mapping[str, Any]],
    minimum_trades: int,
    minimum_wr: float,
    minimum_pf: float,
) -> bool:
    return bool(blocks) and all(
        golden.passes_economics(block, minimum_trades, minimum_wr, minimum_pf)
        for block in blocks.values()
    )


def search(args: argparse.Namespace) -> int:
    pairs = [golden.parse_pair(value) for value in args.pair]
    if len(pairs) < 8:
        raise SystemExit("at least eight chronological live windows are required")
    runs = golden.load_runs(pairs)
    rows = build_rows(runs)
    holdout_start = len(runs) - 2
    validation_start = max(4, holdout_start - 2)
    train = [row for row in rows if integer(row.get("run_index")) < validation_start]
    validation = [row for row in rows if validation_start <= integer(row.get("run_index")) < holdout_start]
    holdout = [row for row in rows if integer(row.get("run_index")) >= holdout_start]
    train_runs = runs[:validation_start]
    validation_runs = runs[validation_start:holdout_start]
    holdout_runs = runs[holdout_start:]
    latencies = economics.parse_latencies(args.latencies)

    best: tuple[Any, ...] | None = None
    diagnostics: list[dict[str, Any]] = []
    for spec in specs():
        model = fit(train, spec)
        validation_probs = probability(model, validation)
        spec_best: tuple[Any, ...] | None = None
        for gate in candidate_gates(validation_probs):
            selected_train = choose(train, model, gate)
            selected_validation = choose(validation, model, gate)
            if len(selected_train) < 10 or len(selected_validation) < 4:
                continue
            train_blocks = golden.aggregate_economics(
                train_runs,
                selected_train,
                latencies,
                starting_balance_sol=args.starting_balance_sol,
                max_output_shortfall_bps=gate.max_output_shortfall_bps,
            )
            if not all_latency_pass(train_blocks, 10, args.minimum_win_rate, args.minimum_profit_factor):
                continue
            validation_blocks = golden.aggregate_economics(
                validation_runs,
                selected_validation,
                latencies,
                starting_balance_sol=args.starting_balance_sol,
                max_output_shortfall_bps=gate.max_output_shortfall_bps,
            )
            if not all_latency_pass(validation_blocks, 4, args.minimum_win_rate, args.minimum_profit_factor):
                continue
            objective = (
                min(finite(block.get("win_rate")) for block in validation_blocks.values()),
                min(finite(block.get("wilson_low")) for block in validation_blocks.values()),
                min(finite(block.get("profit_factor")) for block in validation_blocks.values()),
                sum(finite(block.get("net_pnl_sol")) for block in validation_blocks.values()),
                min(integer(block.get("trades")) for block in validation_blocks.values()),
                -gate.max_output_shortfall_bps,
            )
            candidate = (objective, spec, model, gate, train_blocks, validation_blocks)
            if spec_best is None or objective > spec_best[0]:
                spec_best = candidate
            if best is None or objective > best[0]:
                best = candidate
        diagnostics.append({"spec": spec.as_dict(), "found": spec_best is not None, "objective": list(spec_best[0]) if spec_best else None})
        print(json.dumps(diagnostics[-1], sort_keys=True), flush=True)

    if best is None:
        report = {
            "version": "e4-v12-reactive-profit-model-v1",
            "status": "NOT_CONCLUSIVE",
            "reason": "no model/gate passed true 0-10ms train and validation economics",
            "diagnostics": diagnostics,
            "coverage": {"rows": len(rows), "train": len(train), "validation": len(validation), "holdout": len(holdout)},
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        print(json.dumps(report, indent=2, sort_keys=True))
        return 2

    _, spec, model, gate, train_blocks, validation_blocks = best
    holdout_selected = choose(holdout, model, gate)
    holdout_blocks = golden.aggregate_economics(
        holdout_runs,
        holdout_selected,
        latencies,
        starting_balance_sol=args.starting_balance_sol,
        max_output_shortfall_bps=gate.max_output_shortfall_bps,
    )
    passed = len(holdout_selected) >= 4 and all_latency_pass(
        holdout_blocks,
        4,
        args.minimum_win_rate,
        args.minimum_profit_factor,
    )
    status = "HISTORICAL_HOLDOUT_CONFIRMED" if passed else "NOT_CONCLUSIVE"
    report = {
        "version": "e4-v12-reactive-profit-model-v1",
        "status": status,
        "thesis": (
            "Use E4's authenticated source BUY as selection authority, but execute only the frozen "
            "historically profitable source/launch regime and only while scaled source token output "
            "remains inside a tightly protected current curve quote."
        ),
        "spec": spec.as_dict(),
        "gate": gate.as_dict(),
        "features": FEATURES,
        "latencies_ms": latencies,
        "starting_balance_sol": args.starting_balance_sol,
        "train_runs": [run.run_id for run in train_runs],
        "validation_runs": [run.run_id for run in validation_runs],
        "holdout_runs": [run.run_id for run in holdout_runs],
        "train": train_blocks,
        "validation": validation_blocks,
        "holdout": holdout_blocks,
        "holdout_predictions": len(holdout_selected),
        "diagnostics": diagnostics,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    args.predictions_output.write_text(json.dumps({"predictions": holdout_selected}, indent=2, sort_keys=True), encoding="utf-8")
    joblib.dump(
        {
            "version": "e4-v12-reactive-profit-model-v1",
            "status": status,
            "model": model,
            "spec": spec.as_dict(),
            "gate": gate.as_dict(),
            "features": FEATURES,
            "history_run_ids": [run.run_id for run in runs],
        },
        args.model_output,
    )
    print(json.dumps({"status": status, "spec": spec.as_dict(), "gate": gate.as_dict(), "holdout_predictions": len(holdout_selected)}, indent=2, sort_keys=True))
    return 0 if passed else 3


def apply(args: argparse.Namespace) -> int:
    bundle = joblib.load(args.model_input)
    model = bundle["model"]
    gate = Gate(**bundle["gate"])
    pairs = [golden.parse_pair(value) for value in args.pair]
    runs = golden.load_runs(pairs)
    rows = build_rows(runs)
    live_index = len(runs) - 1
    selected = choose([row for row in rows if integer(row.get("run_index")) == live_index], model, gate)
    args.predictions_output.parent.mkdir(parents=True, exist_ok=True)
    args.predictions_output.write_text(
        json.dumps(
            {
                "version": "e4-v12-reactive-profit-live-v1",
                "live_run_id": runs[-1].run_id,
                "gate": gate.as_dict(),
                "predictions": selected,
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    print(json.dumps({"live_run_id": runs[-1].run_id, "predictions": len(selected)}, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Search a profit-first sub-10ms E4 source-entry model")
    parser.add_argument("--mode", choices=("search", "apply"), default="search")
    parser.add_argument("--pair", action="append", default=[])
    parser.add_argument("--latencies", default="0,1,2,5,10")
    parser.add_argument("--starting-balance-sol", type=float, default=3.0)
    parser.add_argument("--minimum-win-rate", type=float, default=0.65)
    parser.add_argument("--minimum-profit-factor", type=float, default=1.25)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model-output", type=Path, required=True)
    parser.add_argument("--model-input", type=Path)
    parser.add_argument("--predictions-output", type=Path, required=True)
    args = parser.parse_args()
    if args.mode == "apply":
        if args.model_input is None:
            parser.error("--model-input is required in apply mode")
        return apply(args)
    return search(args)


if __name__ == "__main__":
    raise SystemExit(main())
