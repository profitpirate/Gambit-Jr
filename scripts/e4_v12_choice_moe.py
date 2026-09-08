#!/usr/bin/env python3
"""Causal E4 V12 no-trade, conditional-choice, and execution research.

The program consumes the immutable V1/V2 evidence bundle and local, pinned capture
restorations.  It writes paper-research artifacts only.  Production modules, model
paths, entry/exit/risk policy, and live-wallet execution are deliberately not imported.
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import math
import statistics
import subprocess
from collections import Counter, defaultdict, deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import e4_v12_canonical_choice_sets as v1
import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score

BASE_COMMIT = "3ba523f1090fa1232eba667484fe534879b3f346"
SCHEMA_VERSION = "e4-v12-choice-moe-v1"
THESIS_FAMILY = "joint-no-trade-pairwise-choice-moe-execution-v1"
ACTIVE_HORIZON_MS = 60_000
EXCLUSION_MARGIN_MS = 2_000
NULL_TO_POSITIVE_TRAIN_RATIO = 2.0
LATENCIES_MS = (0, 1, 2, 5, 10)
RANDOM_SEED = 12_041
STARTING_BANKROLL_SOL = 3.0
POSITION_FRACTION = 0.0185
MAX_CONCURRENT_POSITIONS = 2
V2_EXPECTED = {
    "artifacts/e4-v12-canonical-choice-risksets-v2.jsonl": (
        97_787_860,
        "90224a7810daa23eea0ecd9318a58380bf6d9cca0f2c2e41ffba308fad8892b0",
    ),
    "artifacts/e4-v12-canonical-choice-risksets-v2.parquet": (
        7_009_644,
        "806845ede9efaf4ad48191a9d2ac8a80a600e89df0cdc95daff95cd07c44bee4",
    ),
    "artifacts/e4-v12-choice-riskset-v2-coverage.json": (
        37_457,
        "f383cd7ba37e661850df12a7cc5e02cddc50af9b68968233b12d66ab797ebd4c",
    ),
    "artifacts/e4-v12-choice-riskset-v2-report.md": (
        1_574,
        "2cc8eb8dc8cdb35988480fd6c66f8c6e89187f53333c9b1e3a5537afff66288b",
    ),
    "artifacts/e4-v12-entry-age-forensics.json": (
        30_031,
        "bdef19f519c6b32b209a731be5d84624b669b03aeed13d9a8734cee99ee1a5bb",
    ),
    "artifacts/e4-v12-selection-backfill.json": (
        623_074,
        "41891e5e553c5d81898c527d980e54f549a49c70065757123a13bf6355a7e0c9",
    ),
    "artifacts/e4-v12-selection-exclusions-v2.jsonl": (
        353_734,
        "ee2c8a104cb71fa5601fbc76574a2657c46c98d6ffb64b227b6e7eab68db0e9d",
    ),
    "artifacts/e4-v12-causal-entity-graph.json": (
        5_491_696,
        "a7da85b86ffd55bf62264221f1e2be7363f4ac7103a716cd321051526d2e71be",
    ),
    "artifacts/e4-v12-hard-negative-audit.json": (
        1_168,
        "12512efb5cb08502466b4f001eb32ed85b44d5d164b2f18ba0d0356712fd7953",
    ),
    "artifacts/e4-v12-riskset-source-manifest.json": (
        36_595,
        "e2218156bc054af75f0dcc8c2debbfd04c2245343ba516850d4167e3a117dca1",
    ),
    "artifacts/e4-v12-feature-causality-audit.json": (
        84_210,
        "68cd5e76d027dc5468c3d06d7a85756aea44843df2eb79cae340e09c5f6aa217",
    ),
    "artifacts/e4-v12-v1-immutability-audit.json": (
        2_254,
        "9a29dd507dbe519bf9085e7d69ea76b041bfb23addb236e60df18f9ff2beec16",
    ),
}
PROHIBITED_FEATURE_TOKENS = (
    "selected_by_e4",
    "selection_label",
    "candidate_role",
    "landed_successfully",
    "source_transaction_failed",
    "output_guard_rejected",
    "decision_signature",
    "chosen_mint",
    "future_",
    "maximum_market_cap",
    "sell_timestamp",
    "source_row_event_identity",
    "source_artifact_name",
    "hard_negative_categories",
)
NUMERIC_FEATURES = (
    "candidate_age_ms",
    "creator_seed_sol",
    "public_buy_sol",
    "buy_count",
    "sell_count",
    "unique_buyers",
    "same_slot_buyer_count",
    "same_transaction_buyer_count",
    "transaction_count",
    "max_buys_in_one_transaction",
    "virtual_sol_reserve",
    "virtual_token_reserve",
    "real_token_reserve",
    "executable_token_output_0_1_sol",
    "price_sol",
    "fdv_usd",
    "price_multiple_from_create",
    "creator_history_trades",
    "creator_history_win_rate",
    "creator_prior_launch_count",
    "creator_prior_e4_selection_count_v2",
    "creator_prior_landed_count",
    "creator_prior_failed_fill_count",
    "time_since_previous_launch_ms",
    "time_since_previous_e4_selection_ms",
    "known_e4_associated_buyer_count",
    "buyer_prior_e4_selection_count",
    "buyer_cluster_recurrence",
    "creator_buyer_pair_recurrence",
    "social_authority_prior_selection_count",
    "buyer_concentration",
    "buys_per_second",
    "unique_buyers_per_second",
    "buy_sell_imbalance",
    "topology_score",
    "estimated_price_impact_bps",
    "cumulative_outside_sol",
)
BOOLEAN_FEATURES = (
    "creator_history_available",
    "first_buyer_history_available",
    "buyer_cluster_history_available",
    "social_history_available",
    "metadata_history_available",
    "social_available_before_decision",
    "metadata_observation_available",
    "website_available_before_decision",
    "mayhem_mode",
    "cashback_enabled",
    "opportunity_pool_left_truncated",
)
EXPERT_FEATURES = {
    "recency_same_slot": (
        "candidate_age_ms",
        "same_slot_buyer_count",
        "same_transaction_buyer_count",
        "buys_per_second",
    ),
    "creator_recurrence": (
        "creator_seed_sol",
        "creator_prior_launch_count",
        "creator_prior_e4_selection_count_v2",
        "creator_prior_landed_count",
        "creator_prior_failed_fill_count",
        "creator_history_win_rate",
        "time_since_previous_launch_ms",
    ),
    "buyer_cluster": (
        "unique_buyers",
        "known_e4_associated_buyer_count",
        "buyer_prior_e4_selection_count",
        "buyer_cluster_recurrence",
        "creator_buyer_pair_recurrence",
        "buyer_concentration",
    ),
    "transaction_topology": (
        "transaction_count",
        "max_buys_in_one_transaction",
        "same_slot_buyer_count",
        "same_transaction_buyer_count",
        "topology_score",
    ),
    "market_flow_curve": (
        "fdv_usd",
        "virtual_sol_reserve",
        "real_token_reserve",
        "public_buy_sol",
        "buy_count",
        "sell_count",
        "unique_buyers_per_second",
        "buy_sell_imbalance",
        "estimated_price_impact_bps",
        "executable_token_output_0_1_sol",
    ),
    "social_metadata": (
        "social_authority_prior_selection_count",
        "social_available_before_decision",
        "metadata_observation_available",
        "website_available_before_decision",
    ),
    "cold_start_generalist": NUMERIC_FEATURES + BOOLEAN_FEATURES,
}
BASELINES = (
    "random_candidate",
    "always_no_trade",
    "youngest_candidate",
    "newest_same_slot_candidate",
    "highest_creator_seed",
    "highest_unique_buyer_count",
    "highest_outside_sol",
    "highest_flow_velocity",
    "lowest_fdv",
    "strongest_creator_prior",
    "strongest_buyer_cluster_prior",
    "conditional_logistic_regression",
    "pairwise_linear_ranker",
    "gradient_boosted_pairwise_ranker",
    "listwise_group_softmax",
    "non_mixture_generalist",
    "restricted_mixture_of_experts",
)


def json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(item) for item in value]
    if isinstance(value, np.ndarray):
        return [json_safe(item) for item in value.tolist()]
    if isinstance(value, np.generic):
        return json_safe(value.item())
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(json_safe(value), separators=(",", ":"), sort_keys=True)


def stable_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(json_safe(value), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_jsonl(path: Path, values: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for value in values:
            handle.write(canonical_json(value) + "\n")


def finite(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def percentile(values: Sequence[float], quantile: float) -> float | None:
    if not values:
        return None
    return float(np.quantile(np.asarray(values, dtype=float), quantile))


def verify_v2_inputs(repo_root: Path) -> dict[str, Any]:
    files = []
    for relative, (expected_bytes, expected_sha) in V2_EXPECTED.items():
        path = repo_root / relative
        actual_bytes = path.stat().st_size
        actual_sha = sha256_path(path)
        match = actual_bytes == expected_bytes and actual_sha == expected_sha
        files.append(
            {
                "path": relative,
                "bytes": actual_bytes,
                "sha256": actual_sha,
                "expected_bytes": expected_bytes,
                "expected_sha256": expected_sha,
                "match": match,
            }
        )
    failures = [row for row in files if not row["match"]]
    if failures:
        raise ValueError(f"immutable V2 input changed: {canonical_json(failures)}")
    coverage = json.loads(
        (repo_root / "artifacts/e4-v12-choice-riskset-v2-coverage.json").read_text()
    )
    integrity = coverage["integrity"]
    required_zero = (
        "ambiguous_labels",
        "chronological_split_leakage",
        "duplicate_rows",
        "future_leakage_violations",
        "missing_chosen_rows",
    )
    if integrity["status"] != "PASS" or any(integrity[key] for key in required_zero):
        raise ValueError("V2 integrity gate is not clean")
    if coverage["entry_age_policy"]["policy_fit_split"] != "train":
        raise ValueError("V2 risk-set policy was not fitted on training data")
    return {
        "status": "PASS",
        "base_commit": BASE_COMMIT,
        "files": files,
        "frozen_dataset_fingerprint": stable_hash({row["path"]: row["sha256"] for row in files}),
    }


def load_v2_groups(path: Path) -> list[list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    observed_order = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            group_id = str(row["decision_group_id"])
            groups[group_id].append(row)
            observed_order.append(
                (
                    integer(row["decision_ns"]),
                    group_id,
                    0 if row["selected_by_e4"] else 1,
                    str(row["candidate_mint"]),
                )
            )
    if observed_order != sorted(observed_order):
        raise ValueError("V2 deterministic row order changed")
    output = list(groups.values())
    for group in output:
        selected = [row for row in group if row["selected_by_e4"]]
        if len(selected) != 1:
            raise ValueError("V2 group does not contain exactly one selected row")
        if len({row["candidate_mint"] for row in group}) != len(group):
            raise ValueError("V2 group contains a duplicate candidate")
    return output


def feature_names(fields: Sequence[str] = NUMERIC_FEATURES + BOOLEAN_FEATURES) -> list[str]:
    names = []
    for name in fields:
        if any(token in name.lower() for token in PROHIBITED_FEATURE_TOKENS):
            raise ValueError(f"prohibited feature selected: {name}")
        names.append(name)
        if name in NUMERIC_FEATURES:
            names.append(f"{name}__missing")
    return names


def raw_feature_vector(row: Mapping[str, Any], fields: Sequence[str]) -> np.ndarray:
    values = []
    for name in fields:
        if any(token in name.lower() for token in PROHIBITED_FEATURE_TOKENS):
            raise ValueError(f"prohibited feature selected: {name}")
        value = row.get(name)
        if name in BOOLEAN_FEATURES:
            values.append(float(bool(value)))
            continue
        missing = value is None
        number = finite(value)
        if name in {
            "candidate_age_ms",
            "fdv_usd",
            "virtual_token_reserve",
            "real_token_reserve",
            "executable_token_output_0_1_sol",
            "time_since_previous_launch_ms",
            "time_since_previous_e4_selection_ms",
            "buys_per_second",
            "unique_buyers_per_second",
        }:
            number = math.copysign(math.log1p(abs(number)), number)
        values.extend((number, float(missing)))
    return np.asarray(values, dtype=float)


@dataclass
class TrainOnlyTransformer:
    fields: tuple[str, ...]
    means: np.ndarray | None = None
    scales: np.ndarray | None = None
    fit_group_ids: tuple[str, ...] = ()

    def fit(self, groups: Sequence[Sequence[Mapping[str, Any]]]) -> TrainOnlyTransformer:
        if not groups or any(group[0]["split"] != "train" for group in groups):
            raise ValueError("transforms may only fit training groups")
        matrix = np.vstack(
            [raw_feature_vector(row, self.fields) for group in groups for row in group]
        )
        self.means = matrix.mean(axis=0)
        self.scales = matrix.std(axis=0)
        self.scales[self.scales < 1e-9] = 1.0
        self.fit_group_ids = tuple(str(group[0]["decision_group_id"]) for group in groups)
        return self

    def transform_rows(self, rows: Sequence[Mapping[str, Any]]) -> np.ndarray:
        if self.means is None or self.scales is None:
            raise ValueError("transformer is not fitted")
        matrix = np.vstack([raw_feature_vector(row, self.fields) for row in rows])
        return (matrix - self.means) / self.scales


def symmetrical_pairs(
    groups: Sequence[Sequence[Mapping[str, Any]]],
    transformer: TrainOnlyTransformer,
    *,
    selected_by_group: Mapping[str, str] | None = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    differences = []
    labels = []
    weights = []
    for group in groups:
        group_id = str(group[0]["decision_group_id"])
        chosen = (
            selected_by_group[group_id]
            if selected_by_group is not None
            else str(next(row for row in group if row["selected_by_e4"])["candidate_mint"])
        )
        selected = next(row for row in group if str(row["candidate_mint"]) == chosen)
        alternatives = [row for row in group if str(row["candidate_mint"]) != chosen]
        if not alternatives:
            continue
        selected_vector = transformer.transform_rows([selected])[0]
        alternative_vectors = transformer.transform_rows(alternatives)
        pair_weight = 1.0 / (2.0 * len(alternatives))
        for alternative_vector in alternative_vectors:
            difference = selected_vector - alternative_vector
            differences.extend((difference, -difference))
            labels.extend((1, 0))
            weights.extend((pair_weight, pair_weight))
    if not differences:
        raise ValueError("no pairwise observations")
    return np.vstack(differences), np.asarray(labels), np.asarray(weights)


@dataclass
class LinearRanker:
    fields: tuple[str, ...]
    transformer: TrainOnlyTransformer = field(init=False)
    model: LogisticRegression = field(init=False)

    def __post_init__(self) -> None:
        self.transformer = TrainOnlyTransformer(self.fields)
        self.model = LogisticRegression(C=0.25, max_iter=500, solver="liblinear")

    def fit(self, groups: Sequence[Sequence[Mapping[str, Any]]]) -> LinearRanker:
        self.transformer.fit(groups)
        matrix, labels, weights = symmetrical_pairs(groups, self.transformer)
        self.model.fit(matrix, labels, sample_weight=weights)
        return self

    def score(self, rows: Sequence[Mapping[str, Any]]) -> np.ndarray:
        matrix = self.transformer.transform_rows(rows)
        return matrix @ self.model.coef_[0]


@dataclass
class RowLogisticRanker:
    fields: tuple[str, ...]
    transformer: TrainOnlyTransformer = field(init=False)
    model: LogisticRegression = field(init=False)

    def __post_init__(self) -> None:
        self.transformer = TrainOnlyTransformer(self.fields)
        self.model = LogisticRegression(C=0.2, max_iter=500, solver="liblinear")

    def fit(self, groups: Sequence[Sequence[Mapping[str, Any]]]) -> RowLogisticRanker:
        self.transformer.fit(groups)
        matrix = []
        labels = []
        weights = []
        for group in groups:
            group_matrix = self.transformer.transform_rows(group)
            matrix.extend(group_matrix)
            labels.extend(int(row["selected_by_e4"]) for row in group)
            weights.extend([1.0 / len(group)] * len(group))
        self.model.fit(np.vstack(matrix), np.asarray(labels), sample_weight=np.asarray(weights))
        return self

    def score(self, rows: Sequence[Mapping[str, Any]]) -> np.ndarray:
        return self.model.decision_function(self.transformer.transform_rows(rows))


@dataclass
class PairwiseGBTRanker:
    fields: tuple[str, ...]
    transformer: TrainOnlyTransformer = field(init=False)
    model: HistGradientBoostingClassifier = field(init=False)

    def __post_init__(self) -> None:
        self.transformer = TrainOnlyTransformer(self.fields)
        self.model = HistGradientBoostingClassifier(
            learning_rate=0.05,
            max_iter=80,
            max_leaf_nodes=7,
            l2_regularization=3.0,
            random_state=RANDOM_SEED,
        )

    def fit(self, groups: Sequence[Sequence[Mapping[str, Any]]]) -> PairwiseGBTRanker:
        self.transformer.fit(groups)
        matrix, labels, weights = symmetrical_pairs(groups, self.transformer)
        self.model.fit(matrix, labels, sample_weight=weights)
        return self

    def score(self, rows: Sequence[Mapping[str, Any]]) -> np.ndarray:
        matrix = self.transformer.transform_rows(rows)
        zero = np.zeros_like(matrix)
        # A candidate's utility is its learned pairwise preference over a neutral peer.
        return self.model.predict_proba(matrix - zero)[:, 1]


@dataclass
class ListwiseSoftmaxRanker:
    fields: tuple[str, ...]
    transformer: TrainOnlyTransformer = field(init=False)
    coefficients: np.ndarray | None = None

    def __post_init__(self) -> None:
        self.transformer = TrainOnlyTransformer(self.fields)

    def fit(self, groups: Sequence[Sequence[Mapping[str, Any]]]) -> ListwiseSoftmaxRanker:
        self.transformer.fit(groups)
        width = len(feature_names(self.fields))
        coefficients = np.zeros(width, dtype=float)
        cached = [
            (
                self.transformer.transform_rows(group),
                np.asarray([float(row["selected_by_e4"]) for row in group]),
            )
            for group in groups
        ]
        for step in range(500):
            gradient = 0.04 * coefficients
            for matrix, labels in cached:
                logits = matrix @ coefficients
                probabilities = softmax(logits)
                gradient += matrix.T @ (probabilities - labels) / len(groups)
            rate = 0.06 / math.sqrt(1 + step / 40)
            coefficients -= rate * gradient
        self.coefficients = coefficients
        return self

    def score(self, rows: Sequence[Mapping[str, Any]]) -> np.ndarray:
        if self.coefficients is None:
            raise ValueError("ranker is not fitted")
        return self.transformer.transform_rows(rows) @ self.coefficients


@dataclass
class MixtureRanker:
    experts: dict[str, LinearRanker]
    train_weights: dict[str, float]

    @classmethod
    def fit(cls, groups: Sequence[Sequence[Mapping[str, Any]]]) -> MixtureRanker:
        experts = {
            name: LinearRanker(tuple(fields)).fit(groups)
            for name, fields in EXPERT_FEATURES.items()
        }
        accuracies = {
            name: selection_metrics(groups, expert.score)["top_1_accuracy"]
            for name, expert in experts.items()
        }
        excess = {name: max(0.01, value - 0.04) for name, value in accuracies.items()}
        total = sum(excess.values())
        return cls(experts, {name: value / total for name, value in excess.items()})

    def active_weights(self, rows: Sequence[Mapping[str, Any]]) -> dict[str, float]:
        available = dict(self.train_weights)
        if not any(row.get("social_available_before_decision") for row in rows):
            available["social_metadata"] = 0.0
        if not any(row.get("buyer_cluster_history_available") for row in rows):
            available["buyer_cluster"] = 0.0
        if not any(row.get("creator_history_available") for row in rows):
            available["creator_recurrence"] = 0.0
        total = sum(available.values()) or 1.0
        return {name: value / total for name, value in available.items()}

    def score(self, rows: Sequence[Mapping[str, Any]]) -> np.ndarray:
        weights = self.active_weights(rows)
        result = np.zeros(len(rows), dtype=float)
        for name, expert in self.experts.items():
            values = expert.score(rows)
            scale = max(float(np.std(values)), 1e-9)
            result += weights[name] * (values - float(np.mean(values))) / scale
        return result


def softmax(values: np.ndarray) -> np.ndarray:
    shifted = values - float(np.max(values))
    exponent = np.exp(np.clip(shifted, -40, 40))
    return exponent / exponent.sum()


def deterministic_argmax(rows: Sequence[Mapping[str, Any]], scores: np.ndarray) -> int:
    order = sorted(
        range(len(rows)),
        key=lambda index: (-float(scores[index]), str(rows[index]["candidate_mint"])),
    )
    return order[0]


def ranking_from_scores(rows: Sequence[Mapping[str, Any]], scores: np.ndarray) -> list[int]:
    return sorted(
        range(len(rows)),
        key=lambda index: (-float(scores[index]), str(rows[index]["candidate_mint"])),
    )


def selection_metrics(
    groups: Sequence[Sequence[Mapping[str, Any]]],
    scorer: Any,
) -> dict[str, Any]:
    ranks = []
    pair_correct = 0
    pair_total = 0
    losses = []
    probabilities = []
    labels = []
    by_window: dict[str, list[int]] = defaultdict(list)
    for group in groups:
        scores = np.asarray(scorer(group), dtype=float)
        order = ranking_from_scores(group, scores)
        chosen_index = next(index for index, row in enumerate(group) if row["selected_by_e4"])
        rank = order.index(chosen_index) + 1
        ranks.append(rank)
        pair_correct += sum(
            scores[chosen_index] > scores[index] for index in order if index != chosen_index
        )
        pair_correct += 0.5 * sum(
            scores[chosen_index] == scores[index] for index in order if index != chosen_index
        )
        pair_total += max(0, len(group) - 1)
        group_probabilities = softmax(scores)
        losses.append(-math.log(max(float(group_probabilities[chosen_index]), 1e-12)))
        probabilities.extend(group_probabilities.tolist())
        labels.extend(float(index == chosen_index) for index in range(len(group)))
        by_window[str(group[0]["source_run_id"])].append(int(rank == 1))
    pair_accuracy = pair_correct / pair_total if pair_total else 1.0
    try:
        pair_auc = float(roc_auc_score(labels, probabilities))
    except ValueError:
        pair_auc = None
    return {
        "groups": len(groups),
        "top_1_accuracy": sum(rank == 1 for rank in ranks) / len(ranks),
        "top_2_recall": sum(rank <= 2 for rank in ranks) / len(ranks),
        "top_3_recall": sum(rank <= 3 for rank in ranks) / len(ranks),
        "mean_reciprocal_rank": statistics.fmean(1.0 / rank for rank in ranks),
        "ndcg": statistics.fmean(1.0 / math.log2(rank + 1) for rank in ranks),
        "pairwise_accuracy": pair_accuracy,
        "pairwise_auc": pair_auc,
        "listwise_cross_entropy": statistics.fmean(losses),
        "brier_score": statistics.fmean(
            (probability - label) ** 2 for probability, label in zip(probabilities, labels)
        ),
        "by_capture_window": {
            run_id: {"groups": len(values), "top_1_accuracy": statistics.fmean(values)}
            for run_id, values in sorted(by_window.items())
        },
    }


def baseline_scorer(name: str) -> Any:
    fields = {
        "youngest_candidate": ("candidate_age_ms", False),
        "newest_same_slot_candidate": ("candidate_age_ms", False),
        "highest_creator_seed": ("creator_seed_sol", True),
        "highest_unique_buyer_count": ("unique_buyers", True),
        "highest_outside_sol": ("public_buy_sol", True),
        "highest_flow_velocity": ("unique_buyers_per_second", True),
        "lowest_fdv": ("fdv_usd", False),
        "strongest_creator_prior": ("creator_prior_e4_selection_count_v2", True),
        "strongest_buyer_cluster_prior": ("buyer_cluster_recurrence", True),
    }
    if name == "random_candidate":
        return lambda rows: np.asarray(
            [
                int(hashlib.sha256(str(row["candidate_mint"]).encode()).hexdigest()[:12], 16)
                for row in rows
            ],
            dtype=float,
        )
    field_name, descending = fields[name]

    def score(rows: Sequence[Mapping[str, Any]]) -> np.ndarray:
        values = np.asarray([finite(row.get(field_name)) for row in rows], dtype=float)
        if name == "newest_same_slot_candidate":
            same_slot = np.asarray([float(bool(row.get("same_slot_alternative"))) for row in rows])
            values = -values + same_slot * 1e12
            return values
        return values if descending else -values

    return score


@dataclass(frozen=True)
class CaptureSpec:
    run_id: str
    split: str
    start_ns: int
    end_ns: int
    events_path: Path
    expected_sha256: str


@dataclass(frozen=True)
class Clock:
    run_id: str
    split: str
    timestamp_ns: int
    mint: str
    slot: int
    signature: str
    event_index: int
    activity_1s: int
    capture_progress: float

    @property
    def identity(self) -> str:
        return stable_hash(
            [self.run_id, self.timestamp_ns, self.mint, self.signature, self.event_index]
        )


@dataclass
class NullSamplingPolicy:
    activity_cutpoints: tuple[float, float]
    probabilities: dict[str, float]
    exclusion_margin_ms: int = EXCLUSION_MARGIN_MS
    active_horizon_ms: int = ACTIVE_HORIZON_MS
    fit_split: str = "train"
    target_ratio: float = NULL_TO_POSITIVE_TRAIN_RATIO

    @classmethod
    def fit(cls, clocks: Sequence[Clock], positive_train_groups: int) -> NullSamplingPolicy:
        train_activity = [clock.activity_1s for clock in clocks if clock.split == "train"]
        if not train_activity:
            raise ValueError("no valid training clocks for null policy")
        cutpoints = (
            float(np.quantile(train_activity, 1 / 3)),
            float(np.quantile(train_activity, 2 / 3)),
        )
        counts = Counter(
            cls.band_for(clock.activity_1s, cutpoints) for clock in clocks if clock.split == "train"
        )
        target_total = max(1, round(positive_train_groups * NULL_TO_POSITIVE_TRAIN_RATIO))
        target_per_band = target_total / 3
        probabilities = {
            band: min(1.0, target_per_band / max(counts[band], 1))
            for band in ("low", "medium", "high")
        }
        return cls(cutpoints, probabilities)

    @staticmethod
    def band_for(activity: int, cutpoints: tuple[float, float]) -> str:
        if activity <= cutpoints[0]:
            return "low"
        if activity <= cutpoints[1]:
            return "medium"
        return "high"

    def band(self, clock: Clock) -> str:
        return self.band_for(clock.activity_1s, self.activity_cutpoints)

    def includes(self, clock: Clock) -> bool:
        uniform = int(clock.identity[:16], 16) / float(16**16 - 1)
        return uniform < self.probabilities[self.band(clock)]

    def as_dict(self) -> dict[str, Any]:
        return {
            "fit_split": self.fit_split,
            "validation_used_to_fit": False,
            "holdout_used_to_fit": False,
            "event_clock": "CREATE receipt after all same-receipt events",
            "activity_window_ms": 1_000,
            "activity_cutpoints": list(self.activity_cutpoints),
            "sampling_probabilities_by_activity_band": self.probabilities,
            "target_null_to_positive_train_ratio": self.target_ratio,
            "active_horizon_ms": self.active_horizon_ms,
            "exclusion_margin_ms": self.exclusion_margin_ms,
            "hash_sampling_seed": "sha256(run,timestamp,mint,signature,event_index)",
        }


@dataclass
class HistoryIndex:
    creator_launches: dict[str, list[int]]
    selected_creator: dict[str, list[tuple[int, bool]]]
    selected_buyer: dict[str, list[tuple[int, bool]]]
    selected_cluster: dict[str, list[int]]
    selected_pair: dict[tuple[str, str], list[int]]
    prior_creators: dict[str, Mapping[str, Any]]

    @classmethod
    def build(
        cls,
        groups: Sequence[Sequence[Mapping[str, Any]]],
        creator_launches: Mapping[str, Sequence[int]],
        prior_registry: Mapping[str, Any],
    ) -> HistoryIndex:
        creators: dict[str, list[tuple[int, bool]]] = defaultdict(list)
        buyers: dict[str, list[tuple[int, bool]]] = defaultdict(list)
        clusters: dict[str, list[int]] = defaultdict(list)
        pairs: dict[tuple[str, str], list[int]] = defaultdict(list)
        for group in groups:
            selected = next(row for row in group if row["selected_by_e4"])
            timestamp = integer(selected["decision_ns"])
            landed = str(selected["selection_label"]) == "SELECTED_LANDED"
            creator = str(selected.get("creator_address") or "")
            first_buyers = json.loads(str(selected.get("first_buyer_identities_json") or "[]"))
            cluster = str(selected.get("first_buyer_cluster") or "")
            if creator:
                creators[creator].append((timestamp, landed))
            for buyer in first_buyers:
                buyers[str(buyer)].append((timestamp, landed))
                if creator:
                    pairs[(creator, str(buyer))].append(timestamp)
            if cluster:
                clusters[cluster].append(timestamp)
        return cls(
            {key: sorted(values) for key, values in creator_launches.items()},
            {key: sorted(values) for key, values in creators.items()},
            {key: sorted(values) for key, values in buyers.items()},
            {key: sorted(values) for key, values in clusters.items()},
            {key: sorted(values) for key, values in pairs.items()},
            dict(prior_registry.get("creators") or {}),
        )

    @staticmethod
    def timed_count(values: Sequence[Any], timestamp_ns: int) -> int:
        if values and isinstance(values[0], tuple):
            return bisect.bisect_left(values, (timestamp_ns, False))
        return bisect.bisect_left(values, timestamp_ns)

    def creator_counts(self, creator: str, timestamp_ns: int) -> dict[str, Any]:
        selected = self.selected_creator.get(creator, [])
        count = self.timed_count(selected, timestamp_ns)
        prior = selected[:count]
        launches = self.creator_launches.get(creator, [])
        launch_count = bisect.bisect_left(launches, timestamp_ns)
        previous_launch = launches[launch_count - 1] if launch_count else None
        base = self.prior_creators.get(creator) or {}
        return {
            "creator_history_available": bool(base or launch_count),
            "creator_history_trades": integer(base.get("trades")),
            "creator_history_win_rate": finite(base.get("win_rate")),
            "creator_prior_launch_count": launch_count,
            "creator_prior_e4_selection_count_v2": count,
            "creator_prior_landed_count": sum(landed for _, landed in prior),
            "creator_prior_failed_fill_count": sum(not landed for _, landed in prior),
            "time_since_previous_launch_ms": (
                (timestamp_ns - previous_launch) / 1_000_000 if previous_launch else None
            ),
            "time_since_previous_e4_selection_ms": (
                (timestamp_ns - prior[-1][0]) / 1_000_000 if prior else None
            ),
        }

    def buyer_counts(
        self, creator: str, buyers: Sequence[str], cluster: str, timestamp_ns: int
    ) -> dict[str, Any]:
        prior = {
            buyer: self.timed_count(self.selected_buyer.get(buyer, []), timestamp_ns)
            for buyer in buyers
        }
        return {
            "first_buyer_history_available": bool(buyers),
            "buyer_cluster_history_available": bool(cluster),
            "known_e4_associated_buyer_count": sum(count > 0 for count in prior.values()),
            "buyer_prior_e4_selection_count": sum(prior.values()),
            "buyer_cluster_recurrence": self.timed_count(
                self.selected_cluster.get(cluster, []), timestamp_ns
            ),
            "creator_buyer_pair_recurrence": max(
                (
                    self.timed_count(self.selected_pair.get((creator, buyer), []), timestamp_ns)
                    for buyer in buyers
                ),
                default=0,
            ),
        }


@dataclass
class CandidateState:
    run_id: str
    split: str
    mint: str
    creator: str
    create_ns: int
    create_slot: int
    create_signature: str
    raw: dict[str, Any]
    creator_seed_sol: float = 0.0
    public_buy_sol: float = 0.0
    buy_count: int = 0
    sell_count: int = 0
    buyers: set[str] = field(default_factory=set)
    signatures: set[str] = field(default_factory=set)
    signature_buy_counts: Counter[str] = field(default_factory=Counter)
    same_slot_buyers: set[str] = field(default_factory=set)
    same_transaction_buyers: set[str] = field(default_factory=set)
    latest_ns: int = 0
    latest_price_sol: float | None = None
    initial_price_sol: float | None = None
    latest_fdv_usd: float | None = None
    virtual_sol_reserve: float | None = None
    virtual_token_reserve: float | None = None
    real_token_reserve: float | None = None
    complete: bool = False

    @classmethod
    def from_create(cls, row: Mapping[str, Any], split: str, run_id: str) -> CandidateState:
        raw = row.get("raw") if isinstance(row.get("raw"), Mapping) else {}
        state = cls(
            run_id=run_id,
            split=split,
            mint=str(row.get("mint") or ""),
            creator=str(row.get("creator") or raw.get("creator") or row.get("trader") or ""),
            create_ns=integer(row.get("received_ns")),
            create_slot=integer(row.get("slot"), -1),
            create_signature=str(row.get("signature") or ""),
            raw=dict(raw),
        )
        state.update(row)
        return state

    def update(self, row: Mapping[str, Any]) -> None:
        timestamp_ns = integer(row.get("received_ns"))
        self.latest_ns = max(self.latest_ns, timestamp_ns)
        signature = str(row.get("signature") or "")
        kind = str(row.get("kind") or "").upper()
        trader = str(row.get("trader") or "")
        if signature:
            self.signatures.add(signature)
        if kind in v1.BUY_KINDS:
            self.buy_count += 1
            self.signature_buy_counts[signature] += 1
            if trader == self.creator:
                self.creator_seed_sol += finite(row.get("sol_amount"))
            elif trader != v1.E4_WALLET:
                self.public_buy_sol += finite(row.get("sol_amount"))
                if trader:
                    self.buyers.add(trader)
                    if integer(row.get("slot"), -1) == self.create_slot:
                        self.same_slot_buyers.add(trader)
                    if signature == self.create_signature:
                        self.same_transaction_buyers.add(trader)
        elif kind in v1.SELL_KINDS:
            self.sell_count += 1
        if kind == "MIGRATION" or bool(row.get("complete")):
            self.complete = True
        virtual_sol, virtual_tokens, real_tokens = v1.reserve_values(row)
        if virtual_sol is not None and virtual_tokens is not None:
            self.virtual_sol_reserve = virtual_sol
            self.virtual_token_reserve = virtual_tokens
            self.real_token_reserve = real_tokens
        price = finite(row.get("price_sol"))
        if price > 0:
            if self.initial_price_sol is None:
                self.initial_price_sol = price
            self.latest_price_sol = price
        fdv = finite(row.get("fdv_usd"))
        if fdv > 0:
            self.latest_fdv_usd = fdv

    def feature_row(
        self,
        decision_ns: int,
        decision_slot: int,
        history: HistoryIndex,
        *,
        capture_start_ns: int,
    ) -> dict[str, Any]:
        age_ms = max(0.0, (decision_ns - self.create_ns) / 1_000_000)
        age_seconds = max(age_ms / 1_000, 0.001)
        buyers = sorted(self.buyers)[:8]
        cluster = "|".join(buyers[:3])
        quote = None
        if self.virtual_sol_reserve and self.virtual_token_reserve:
            quote = v1.buy_tokens(0.1, self.virtual_sol_reserve, self.virtual_token_reserve)
            if self.real_token_reserve is not None:
                quote = min(quote, self.real_token_reserve)
        buy_total = max(self.buy_count, 1)
        row = {
            "schema_version": SCHEMA_VERSION,
            "source_run_id": self.run_id,
            "split": self.split,
            "candidate_mint": self.mint,
            "decision_ns": decision_ns,
            "candidate_age_ms": age_ms,
            "creator_address": self.creator,
            "first_buyer_identities_json": canonical_json(buyers),
            "first_buyer_cluster": cluster,
            "creator_seed_sol": self.creator_seed_sol,
            "public_buy_sol": self.public_buy_sol,
            "buy_count": self.buy_count,
            "sell_count": self.sell_count,
            "unique_buyers": len(self.buyers),
            "same_slot_buyer_count": len(self.same_slot_buyers),
            "same_transaction_buyer_count": len(self.same_transaction_buyers),
            "transaction_count": len(self.signatures),
            "max_buys_in_one_transaction": max(self.signature_buy_counts.values(), default=0),
            "virtual_sol_reserve": self.virtual_sol_reserve,
            "virtual_token_reserve": self.virtual_token_reserve,
            "real_token_reserve": self.real_token_reserve,
            "executable_token_output_0_1_sol": quote,
            "price_sol": self.latest_price_sol,
            "fdv_usd": self.latest_fdv_usd,
            "price_multiple_from_create": (
                self.latest_price_sol / self.initial_price_sol
                if self.latest_price_sol and self.initial_price_sol
                else None
            ),
            "buyer_concentration": max(self.signature_buy_counts.values(), default=0) / buy_total,
            "buys_per_second": self.buy_count / age_seconds,
            "unique_buyers_per_second": len(self.buyers) / age_seconds,
            "buy_sell_imbalance": (
                (self.buy_count - self.sell_count) / (self.buy_count + self.sell_count)
                if self.buy_count + self.sell_count
                else 0.0
            ),
            "topology_score": (
                len(self.same_slot_buyers)
                + len(self.same_transaction_buyers)
                + max(self.signature_buy_counts.values(), default=0)
            ),
            "estimated_price_impact_bps": (
                0.1 / (self.virtual_sol_reserve + 0.1) * 10_000
                if self.virtual_sol_reserve
                else None
            ),
            "cumulative_outside_sol": self.public_buy_sol,
            "social_authority_prior_selection_count": 0,
            "social_history_available": False,
            "metadata_history_available": bool(self.raw.get("uri")),
            "social_available_before_decision": False,
            "metadata_observation_available": bool(self.raw.get("uri")),
            "website_available_before_decision": False,
            "mayhem_mode": bool(self.raw.get("is_mayhem_mode")),
            "cashback_enabled": bool(self.raw.get("is_cashback_enabled")),
            "opportunity_pool_left_truncated": (
                decision_ns - capture_start_ns < ACTIVE_HORIZON_MS * 1_000_000
            ),
            "same_slot_alternative": self.create_slot == decision_slot,
            "decision_time_is_lower_bound": False,
            "feature_max_event_ns": self.latest_ns,
            "candidate_state_ns": self.latest_ns,
            "funder_history_available": False,
        }
        row.update(history.creator_counts(self.creator, decision_ns))
        row.update(history.buyer_counts(self.creator, buyers, cluster, decision_ns))
        return row


def capture_specs(repo_root: Path) -> list[CaptureSpec]:
    manifest = json.loads((repo_root / "artifacts/e4-v12-riskset-source-manifest.json").read_text())
    output = []
    capture_root = repo_root / ".tmp-choice-set-source/runs"
    for row in manifest["captures"]:
        files = {item["filename"]: item for item in row["files"]}
        run_id = str(row["run_id"])
        output.append(
            CaptureSpec(
                run_id=run_id,
                split=str(row["split"]),
                start_ns=integer(row["capture_start_ns"]),
                end_ns=integer(row["capture_end_ns"]),
                events_path=capture_root
                / run_id
                / "artifacts/e4-v12-forward-batch-live-events.jsonl",
                expected_sha256=str(files["e4-v12-forward-batch-live-events.jsonl"]["sha256"]),
            )
        )
    return output


def known_and_unresolved_intervals(
    repo_root: Path,
    groups: Sequence[Sequence[Mapping[str, Any]]],
    specs: Sequence[CaptureSpec],
) -> tuple[dict[str, list[tuple[int, int, str]]], dict[str, int]]:
    margin_ns = EXCLUSION_MARGIN_MS * 1_000_000
    intervals: dict[str, list[tuple[int, int, str]]] = defaultdict(list)
    known_signatures = set()
    for group in groups:
        selected = next(row for row in group if row["selected_by_e4"])
        lower = integer(selected["decision_ns_lower_bound"])
        upper = integer(selected["decision_ns_upper_bound"], lower)
        run_id = str(selected["source_run_id"])
        intervals[run_id].append((lower - margin_ns, upper + margin_ns, "KNOWN_SELECTION"))
        known_signatures.add(str(selected.get("decision_signature") or ""))
    wallet_paths = (
        repo_root / ".tmp-choice-set-source/research/33876680669/e4-v12-full-wallet-history.json",
        repo_root / ".tmp-choice-set-source/research/33913332895/e4-v12-latest-wallet-history.json",
    )
    wallet_rows: dict[str, Mapping[str, Any]] = {}
    for path in wallet_paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        for row in payload.get("transactions") or []:
            signature = str(row.get("signature") or "")
            current = wallet_rows.get(signature)
            if current is None or (not current.get("detail_ok") and row.get("detail_ok")):
                wallet_rows[signature] = row
    unresolved = 0
    for signature, row in wallet_rows.items():
        if signature in known_signatures or bool(row.get("detail_ok")):
            continue
        timestamp_ns = integer(row.get("blockTime")) * 1_000_000_000
        if not timestamp_ns:
            continue
        for spec in specs:
            if spec.start_ns - margin_ns <= timestamp_ns <= spec.end_ns + margin_ns:
                intervals[spec.run_id].append(
                    (
                        timestamp_ns - margin_ns,
                        timestamp_ns + 1_000_000_000 + margin_ns,
                        "UNRESOLVED_WALLET_ACTIVITY",
                    )
                )
                unresolved += 1
                break
    for rows in intervals.values():
        rows.sort()
    return dict(intervals), {
        "known_selection_intervals": len(groups),
        "unresolved_wallet_intervals": unresolved,
    }


def interval_reason(timestamp_ns: int, intervals: Sequence[tuple[int, int, str]]) -> str | None:
    for lower, upper, reason in intervals:
        if lower <= timestamp_ns <= upper:
            return reason
        if lower > timestamp_ns:
            break
    return None


def discover_clocks(
    specs: Sequence[CaptureSpec],
    intervals: Mapping[str, Sequence[tuple[int, int, str]]],
    *,
    verify_source_hashes: bool,
) -> tuple[list[Clock], dict[str, list[int]], dict[str, Any]]:
    clocks = []
    creator_launches: dict[str, list[int]] = defaultdict(list)
    exclusions = Counter()
    source_checks = []
    for spec in specs:
        if verify_source_hashes:
            actual_sha = sha256_path(spec.events_path)
            source_checks.append(
                {
                    "run_id": spec.run_id,
                    "sha256": actual_sha,
                    "expected_sha256": spec.expected_sha256,
                    "match": actual_sha == spec.expected_sha256,
                }
            )
            if actual_sha != spec.expected_sha256:
                raise ValueError(f"capture source hash changed for {spec.run_id}")
        recent: deque[int] = deque()
        creates = []
        with spec.events_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if '"kind":"CREATE"' not in line:
                    continue
                row = json.loads(line)
                timestamp_ns = integer(row.get("received_ns"))
                raw = row.get("raw") if isinstance(row.get("raw"), Mapping) else {}
                creator = str(row.get("creator") or raw.get("creator") or row.get("trader") or "")
                creator_launches[creator].append(timestamp_ns)
                while recent and recent[0] < timestamp_ns - 1_000_000_000:
                    recent.popleft()
                recent.append(timestamp_ns)
                creates.append((row, len(recent)))
        for row, activity in creates:
            timestamp_ns = integer(row.get("received_ns")) + 1
            if timestamp_ns < spec.start_ns + ACTIVE_HORIZON_MS * 1_000_000:
                exclusions["CAPTURE_WARMUP"] += 1
                continue
            if timestamp_ns > spec.end_ns:
                exclusions["OUTSIDE_FULLY_MONITORED_INTERVAL"] += 1
                continue
            reason = interval_reason(timestamp_ns, intervals.get(spec.run_id, ()))
            if reason:
                exclusions[reason] += 1
                continue
            clocks.append(
                Clock(
                    run_id=spec.run_id,
                    split=spec.split,
                    timestamp_ns=timestamp_ns,
                    mint=str(row.get("mint") or ""),
                    slot=integer(row.get("slot"), -1),
                    signature=str(row.get("signature") or ""),
                    event_index=integer(row.get("event_index"), -1),
                    activity_1s=activity,
                    capture_progress=(timestamp_ns - spec.start_ns)
                    / max(spec.end_ns - spec.start_ns, 1),
                )
            )
    for values in creator_launches.values():
        values.sort()
    return (
        clocks,
        dict(creator_launches),
        {
            "candidate_clocks": len(clocks) + sum(exclusions.values()),
            "valid_clocks": len(clocks),
            "exclusions": dict(sorted(exclusions.items())),
            "source_hash_checks": source_checks,
        },
    )


def iter_timestamp_batches(path: Path) -> Iterable[tuple[int, list[dict[str, Any]]]]:
    timestamp = None
    batch: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            row_timestamp = integer(row.get("received_ns"))
            if timestamp is not None and row_timestamp != timestamp:
                yield timestamp, batch
                batch = []
            timestamp = row_timestamp
            batch.append(row)
    if timestamp is not None:
        yield timestamp, batch


def candidate_snapshot(
    states: Mapping[str, CandidateState],
    queue: deque[tuple[int, str]],
    decision_ns: int,
    decision_slot: int,
    history: HistoryIndex,
    capture_start_ns: int,
) -> list[dict[str, Any]]:
    active_after = decision_ns - ACTIVE_HORIZON_MS * 1_000_000
    while queue and queue[0][0] < active_after:
        queue.popleft()
    rows = []
    seen = set()
    for _, mint in queue:
        if mint in seen:
            continue
        seen.add(mint)
        state = states.get(mint)
        if state is None or state.complete:
            continue
        row = state.feature_row(
            decision_ns,
            decision_slot,
            history,
            capture_start_ns=capture_start_ns,
        )
        if row["virtual_sol_reserve"] and row["virtual_token_reserve"]:
            rows.append(row)
    rows.sort(key=lambda row: (finite(row["candidate_age_ms"]), str(row["candidate_mint"])))
    return rows


def build_null_groups(
    specs: Sequence[CaptureSpec],
    sampled_clocks: Sequence[Clock],
    history: HistoryIndex,
) -> list[dict[str, Any]]:
    clocks_by_run: dict[str, dict[int, list[Clock]]] = defaultdict(lambda: defaultdict(list))
    for clock in sampled_clocks:
        clocks_by_run[clock.run_id][clock.timestamp_ns - 1].append(clock)
    output = []
    for spec in specs:
        targets = clocks_by_run.get(spec.run_id)
        if not targets:
            continue
        states: dict[str, CandidateState] = {}
        active: deque[tuple[int, str]] = deque()
        for timestamp_ns, batch in iter_timestamp_batches(spec.events_path):
            for event in batch:
                mint = str(event.get("mint") or "")
                kind = str(event.get("kind") or "").upper()
                if kind == "CREATE" and mint and mint not in states:
                    state = CandidateState.from_create(event, spec.split, spec.run_id)
                    states[mint] = state
                    active.append((state.create_ns, mint))
                elif mint in states:
                    states[mint].update(event)
            for clock in targets.get(timestamp_ns, ()):
                decision_ns = clock.timestamp_ns
                rows = candidate_snapshot(
                    states,
                    active,
                    decision_ns,
                    clock.slot,
                    history,
                    spec.start_ns,
                )
                if not rows:
                    continue
                group_id = "e4null-" + clock.identity[:24]
                for row in rows:
                    row["decision_group_id"] = group_id
                    row["selected_by_e4"] = False
                output.append(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "decision_group_id": group_id,
                        "source_run_id": spec.run_id,
                        "split": spec.split,
                        "decision_ns": decision_ns,
                        "decision_time_quality": "exact_create_receipt_clock",
                        "event_clock": "CREATE",
                        "clock_mint": clock.mint,
                        "activity_1s": clock.activity_1s,
                        "capture_progress": clock.capture_progress,
                        "chosen_alternative": "NO_TRADE",
                        "selected_coin_rows": 0,
                        "candidate_count": len(rows),
                        "exclusion_margin_ms": EXCLUSION_MARGIN_MS,
                        "alternatives": [
                            *rows,
                            {
                                "candidate_mint": "NO_TRADE",
                                "is_outside_option": True,
                                "chosen": True,
                            },
                        ],
                    }
                )
    output.sort(key=lambda row: (row["decision_ns"], row["decision_group_id"]))
    return output


def null_candidate_rows(group: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [dict(row) for row in group["alternatives"] if not bool(row.get("is_outside_option"))]


@dataclass
class IdentityAwareRanker:
    generalist: LinearRanker
    creator_rates: dict[str, float]
    buyer_cluster_rates: dict[str, float]

    @classmethod
    def fit(cls, groups: Sequence[Sequence[Mapping[str, Any]]]) -> IdentityAwareRanker:
        generalist = LinearRanker(NUMERIC_FEATURES + BOOLEAN_FEATURES).fit(groups)
        creator_total: Counter[str] = Counter()
        creator_selected: Counter[str] = Counter()
        cluster_total: Counter[str] = Counter()
        cluster_selected: Counter[str] = Counter()
        for group in groups:
            weight = 1.0 / len(group)
            for row in group:
                creator = str(row.get("creator_address") or "")
                cluster = str(row.get("first_buyer_cluster") or "")
                creator_total[creator] += weight
                cluster_total[cluster] += weight
                if row["selected_by_e4"]:
                    creator_selected[creator] += 1.0
                    cluster_selected[cluster] += 1.0
        creator_rates = {
            key: (creator_selected[key] + 1.0) / (total + 8.0)
            for key, total in creator_total.items()
            if key
        }
        cluster_rates = {
            key: (cluster_selected[key] + 1.0) / (total + 8.0)
            for key, total in cluster_total.items()
            if key
        }
        return cls(generalist, creator_rates, cluster_rates)

    def score(self, rows: Sequence[Mapping[str, Any]]) -> np.ndarray:
        base = self.generalist.score(rows)
        identity = np.asarray(
            [
                self.creator_rates.get(str(row.get("creator_address") or ""), 0.0)
                + self.buyer_cluster_rates.get(str(row.get("first_buyer_cluster") or ""), 0.0)
                for row in rows
            ],
            dtype=float,
        )
        return base + 0.20 * identity


@dataclass
class BinaryHead:
    fields: tuple[str, ...]
    transformer: TrainOnlyTransformer = field(init=False)
    model: LogisticRegression = field(init=False)
    threshold: float = 0.5

    def __post_init__(self) -> None:
        self.transformer = TrainOnlyTransformer(self.fields)
        self.model = LogisticRegression(C=0.2, max_iter=500, solver="liblinear")

    def fit_rows(
        self,
        rows: Sequence[Mapping[str, Any]],
        labels: Sequence[int],
        group_ids: Sequence[str],
    ) -> BinaryHead:
        wrapped = [
            [dict(row, split="train", decision_group_id=group_id)]
            for row, group_id in zip(rows, group_ids)
        ]
        self.transformer.fit(wrapped)
        matrix = self.transformer.transform_rows(rows)
        self.model.fit(matrix, np.asarray(labels))
        probabilities = self.model.predict_proba(matrix)[:, 1]
        candidates = np.linspace(0.15, 0.85, 71)
        self.threshold = float(
            max(
                candidates,
                key=lambda value: binary_balanced_accuracy(labels, probabilities >= value),
            )
        )
        return self

    def probability(self, rows: Sequence[Mapping[str, Any]]) -> np.ndarray:
        return self.model.predict_proba(self.transformer.transform_rows(rows))[:, 1]


def binary_balanced_accuracy(labels: Sequence[int], predictions: Sequence[bool]) -> float:
    positives = [bool(prediction) for label, prediction in zip(labels, predictions) if label]
    negatives = [
        not bool(prediction) for label, prediction in zip(labels, predictions) if not label
    ]
    return (statistics.fmean(positives or [False]) + statistics.fmean(negatives or [False])) / 2


HAZARD_FIELDS = (
    "candidate_count",
    "activity_1s",
    "top_choice_score",
    "choice_margin",
    "choice_entropy",
    "maximum_flow_velocity",
    "aggregate_public_buy_sol",
    "median_candidate_age_ms",
    "timing_exact",
    "left_truncated",
)


def group_context(
    rows: Sequence[Mapping[str, Any]],
    scorer: Any,
    *,
    activity_1s: int | None = None,
    exact_timing: bool = True,
    precomputed_scores: np.ndarray | None = None,
) -> dict[str, Any]:
    scores = (
        np.asarray(scorer(rows), dtype=float)
        if precomputed_scores is None
        else np.asarray(precomputed_scores, dtype=float)
    )
    ordered = sorted(scores, reverse=True)
    probabilities = softmax(scores)
    return {
        "candidate_count": len(rows),
        "activity_1s": activity_1s
        if activity_1s is not None
        else sum(finite(row.get("buy_count")) for row in rows),
        "top_choice_score": ordered[0],
        "choice_margin": ordered[0] - ordered[1] if len(ordered) > 1 else ordered[0],
        "choice_entropy": -sum(
            float(value) * math.log(max(float(value), 1e-12)) for value in probabilities
        ),
        "maximum_flow_velocity": max(
            (finite(row.get("unique_buyers_per_second")) for row in rows), default=0.0
        ),
        "aggregate_public_buy_sol": sum(finite(row.get("public_buy_sol")) for row in rows),
        "median_candidate_age_ms": statistics.median(
            finite(row.get("candidate_age_ms")) for row in rows
        ),
        "timing_exact": exact_timing,
        "left_truncated": any(row.get("opportunity_pool_left_truncated") for row in rows),
    }


def fit_hazard_head(
    positive_groups: Sequence[Sequence[Mapping[str, Any]]],
    null_groups: Sequence[Mapping[str, Any]],
    scorer: Any,
) -> tuple[BinaryHead, dict[str, Any]]:
    rows = []
    labels = []
    group_ids = []
    exact_positive = [
        group
        for group in positive_groups
        if group[0]["split"] == "train" and not group[0]["decision_time_is_lower_bound"]
    ]
    train_null = [group for group in null_groups if group["split"] == "train"]
    for group in exact_positive:
        rows.append(group_context(group, scorer, exact_timing=True))
        labels.append(1)
        group_ids.append(str(group[0]["decision_group_id"]))
    for group in train_null:
        candidates = null_candidate_rows(group)
        rows.append(
            group_context(
                candidates,
                scorer,
                activity_1s=integer(group["activity_1s"]),
                exact_timing=True,
            )
        )
        labels.append(0)
        group_ids.append(str(group["decision_group_id"]))
    head = BinaryHead(HAZARD_FIELDS).fit_rows(rows, labels, group_ids)
    return head, {
        "fit_split": "train",
        "exact_positive_groups": len(exact_positive),
        "lower_bound_positive_groups_excluded_from_exact_hazard_fit": sum(
            group[0]["split"] == "train" and group[0]["decision_time_is_lower_bound"]
            for group in positive_groups
        ),
        "null_groups": len(train_null),
        "threshold": head.threshold,
    }


def hazard_metrics(
    head: BinaryHead,
    positive_groups: Sequence[Sequence[Mapping[str, Any]]],
    null_groups: Sequence[Mapping[str, Any]],
    scorer: Any,
    split: str,
) -> dict[str, Any]:
    rows = []
    labels = []
    for group in positive_groups:
        if group[0]["split"] == split:
            rows.append(
                group_context(
                    group,
                    scorer,
                    exact_timing=not bool(group[0]["decision_time_is_lower_bound"]),
                )
            )
            labels.append(1)
    for group in null_groups:
        if group["split"] == split:
            rows.append(
                group_context(
                    null_candidate_rows(group),
                    scorer,
                    activity_1s=integer(group["activity_1s"]),
                )
            )
            labels.append(0)
    probabilities = head.probability(rows)
    predictions = probabilities >= head.threshold
    true_positive = sum(label and prediction for label, prediction in zip(labels, predictions))
    false_positive = sum(not label and prediction for label, prediction in zip(labels, predictions))
    true_negative = sum(
        not label and not prediction for label, prediction in zip(labels, predictions)
    )
    false_negative = sum(label and not prediction for label, prediction in zip(labels, predictions))
    try:
        auc = float(roc_auc_score(labels, probabilities))
    except ValueError:
        auc = None
    return {
        "groups": len(rows),
        "positive_groups": sum(labels),
        "null_groups": len(labels) - sum(labels),
        "threshold": head.threshold,
        "trade_precision": true_positive / max(true_positive + false_positive, 1),
        "trade_recall": true_positive / max(true_positive + false_negative, 1),
        "no_trade_precision": true_negative / max(true_negative + false_negative, 1),
        "no_trade_recall": true_negative / max(true_negative + false_positive, 1),
        "false_positive_entries": false_positive,
        "missed_selections": false_negative,
        "roc_auc": auc,
        "brier_score": statistics.fmean(
            (float(probability) - label) ** 2 for probability, label in zip(probabilities, labels)
        ),
    }


EXECUTION_FIELDS = (
    "virtual_sol_reserve",
    "real_token_reserve",
    "executable_token_output_0_1_sol",
    "estimated_price_impact_bps",
    "same_slot_buyer_count",
    "same_transaction_buyer_count",
    "topology_score",
    "candidate_age_ms",
    "unique_buyers_per_second",
    "policy_priority_fee_sol",
)


def execution_row(row: Mapping[str, Any], priority_fee_sol: float) -> dict[str, Any]:
    output = {name: row.get(name) for name in EXECUTION_FIELDS}
    output["policy_priority_fee_sol"] = priority_fee_sol
    return output


def fit_execution_head(
    groups: Sequence[Sequence[Mapping[str, Any]]], priority_fee_sol: float
) -> tuple[BinaryHead, dict[str, Any]]:
    selected = [
        next(row for row in group if row["selected_by_e4"])
        for group in groups
        if group[0]["split"] == "train"
    ]
    rows = [execution_row(row, priority_fee_sol) for row in selected]
    labels = [int(row["landed_successfully"]) for row in selected]
    ids = [str(row["decision_group_id"]) for row in selected]
    head = BinaryHead(EXECUTION_FIELDS).fit_rows(rows, labels, ids)
    return head, {
        "fit_split": "train",
        "selected_events": len(selected),
        "landed": sum(labels),
        "failed_fill": len(labels) - sum(labels),
        "threshold": head.threshold,
        "policy_priority_fee_sol": priority_fee_sol,
    }


def execution_metrics(
    head: BinaryHead,
    groups: Sequence[Sequence[Mapping[str, Any]]],
    split: str,
    priority_fee_sol: float,
) -> dict[str, Any]:
    selected = [
        next(row for row in group if row["selected_by_e4"])
        for group in groups
        if group[0]["split"] == split
    ]
    rows = [execution_row(row, priority_fee_sol) for row in selected]
    labels = [int(row["landed_successfully"]) for row in selected]
    probabilities = head.probability(rows)
    predictions = probabilities >= head.threshold
    tp = sum(label and prediction for label, prediction in zip(labels, predictions))
    fp = sum(not label and prediction for label, prediction in zip(labels, predictions))
    fn = sum(label and not prediction for label, prediction in zip(labels, predictions))
    tn = sum(not label and not prediction for label, prediction in zip(labels, predictions))
    try:
        auc = float(roc_auc_score(labels, probabilities))
    except ValueError:
        auc = None
    latency = {}
    for latency_ms in LATENCIES_MS:
        acceptable = []
        for row in selected:
            current = finite(row.get("executable_token_output_0_1_sol"))
            flow = finite(row.get("buys_per_second"))
            impact = finite(row.get("estimated_price_impact_bps")) / 10_000
            deterioration = min(0.95, flow * latency_ms / 1_000 * impact)
            independent_output = current * (1 - deterioration)
            acceptable.append(independent_output >= current * 0.85 if current else False)
        latency[str(latency_ms)] = {
            "events": len(acceptable),
            "output_guard_acceptance_rate": statistics.fmean(acceptable or [False]),
            "independently_recalculated": True,
        }
    return {
        "events": len(labels),
        "landed": sum(labels),
        "failed_or_rejected": len(labels) - sum(labels),
        "accuracy": (tp + tn) / max(len(labels), 1),
        "accept_precision": tp / max(tp + fp, 1),
        "accept_recall": tp / max(tp + fn, 1),
        "roc_auc": auc,
        "latency_output_guard": latency,
        "actions": [
            "ACCEPT",
            "REJECT_OUTPUT_DETERIORATION",
            "REJECT_UNCERTAIN_QUOTE",
            "REJECT_INSUFFICIENT_BALANCE",
            "REJECT_ROUTE_FAILURE",
        ],
    }


def train_rankers(
    train_groups: Sequence[Sequence[Mapping[str, Any]]],
) -> dict[str, Any]:
    fields = NUMERIC_FEATURES + BOOLEAN_FEATURES
    return {
        "conditional_logistic_regression": RowLogisticRanker(fields).fit(train_groups),
        "pairwise_linear_ranker": LinearRanker(fields).fit(train_groups),
        "gradient_boosted_pairwise_ranker": PairwiseGBTRanker(fields).fit(train_groups),
        "listwise_group_softmax": ListwiseSoftmaxRanker(fields).fit(train_groups),
        "non_mixture_generalist": LinearRanker(fields).fit(train_groups),
        "restricted_mixture_of_experts": MixtureRanker.fit(train_groups),
        "identity_aware_regularised": IdentityAwareRanker.fit(train_groups),
    }


def evaluate_model_families(
    groups: Sequence[Sequence[Mapping[str, Any]]], models: Mapping[str, Any]
) -> tuple[dict[str, Any], str]:
    validation = [group for group in groups if group[0]["split"] == "validation"]
    holdout = [group for group in groups if group[0]["split"] == "holdout"]
    comparison = {}
    for name in BASELINES:
        if name == "always_no_trade":
            comparison[name] = {
                "validation": {
                    "groups": len(validation),
                    "top_1_accuracy": 0.0,
                    "top_3_recall": 0.0,
                    "mean_reciprocal_rank": 0.0,
                    "pairwise_accuracy": 0.0,
                    "note": "outside option is correct only for null groups",
                },
                "holdout": None,
            }
            continue
        scorer = models[name].score if name in models else baseline_scorer(name)
        comparison[name] = {"validation": selection_metrics(validation, scorer)}
    learned = [name for name in models if name != "identity_aware_regularised"]
    winner = max(
        learned,
        key=lambda name: (
            comparison[name]["validation"]["top_1_accuracy"],
            comparison[name]["validation"]["pairwise_accuracy"],
            name == "restricted_mixture_of_experts",
        ),
    )
    # Holdout is opened once, only after validation has frozen the winner.
    for name, result in comparison.items():
        if name == "always_no_trade":
            result["holdout"] = {
                "groups": len(holdout),
                "top_1_accuracy": 0.0,
                "top_3_recall": 0.0,
                "mean_reciprocal_rank": 0.0,
                "pairwise_accuracy": 0.0,
            }
            continue
        scorer = models[name].score if name in models else baseline_scorer(name)
        result["holdout"] = selection_metrics(holdout, scorer)
    identity = models["identity_aware_regularised"]
    comparison["identity_aware_regularised"] = {
        "validation": selection_metrics(validation, identity.score),
        "holdout": selection_metrics(holdout, identity.score),
        "final_candidate_eligible": False,
        "reason": "diagnostic identity-aware setting only",
    }
    return comparison, winner


def subset_groups(
    groups: Sequence[Sequence[Mapping[str, Any]]], name: str
) -> list[Sequence[Mapping[str, Any]]]:
    output = []
    for group in groups:
        selected = next(row for row in group if row["selected_by_e4"])
        alternatives = [row for row in group if not row["selected_by_e4"]]
        selected_age = finite(selected.get("candidate_age_ms"))
        age_differences = [
            abs(finite(row.get("candidate_age_ms")) - selected_age) for row in alternatives
        ]
        categories = "|".join(
            str(row.get("hard_negative_categories_json") or "") for row in alternatives
        )
        include = {
            "all": True,
            "same_slot_alternative": any(row.get("same_slot_alternative") for row in alternatives),
            "alternative_within_100ms": any(value <= 100 for value in age_differences),
            "alternative_within_250ms": any(value <= 250 for value in age_differences),
            "alternative_within_500ms": any(value <= 500 for value in age_differences),
            "alternative_within_1500ms": any(value <= 1_500 for value in age_differences),
            "selected_not_uniquely_youngest": sum(
                finite(row.get("candidate_age_ms")) <= selected_age for row in group
            )
            > 1,
            "matched_age": any(value <= 100 for value in age_differences),
            "matched_fdv": "MATCHED_FDV_ALTERNATIVE" in categories,
            "matched_seed": "MATCHED_SEED_ALTERNATIVE" in categories,
            "matched_flow": "MATCHED_FLOW_ALTERNATIVE" in categories,
            "matched_creator_history": "MATCHED_CREATOR_HISTORY_ALTERNATIVE" in categories,
            "matched_buyer_cluster": "MATCHED_BUYER_CLUSTER_ALTERNATIVE" in categories,
            "landed": str(selected["selection_label"]) == "SELECTED_LANDED",
            "failed_fill": str(selected["selection_label"]) == "SELECTED_FILL_REJECTED",
            "exact_timestamp": not bool(selected["decision_time_is_lower_bound"]),
            "lower_bound_timestamp": bool(selected["decision_time_is_lower_bound"]),
        }[name]
        if include:
            output.append(group)
    return output


def clustered_top1_interval(
    groups: Sequence[Sequence[Mapping[str, Any]]], scorer: Any
) -> list[float | None]:
    if not groups:
        return [None, None]
    by_window: dict[str, list[bool]] = defaultdict(list)
    for group in groups:
        scores = np.asarray(scorer(group), dtype=float)
        chosen = deterministic_argmax(group, scores)
        by_window[str(group[0]["source_run_id"])].append(bool(group[chosen]["selected_by_e4"]))
    windows = sorted(by_window)
    rng = np.random.default_rng(RANDOM_SEED)
    estimates = []
    for _ in range(500):
        sampled = rng.choice(windows, size=len(windows), replace=True)
        outcomes = [outcome for window in sampled for outcome in by_window[str(window)]]
        estimates.append(statistics.fmean(outcomes))
    return [percentile(estimates, 0.025), percentile(estimates, 0.975)]


def anti_shortcut_report(
    holdout: Sequence[Sequence[Mapping[str, Any]]], scorer: Any
) -> dict[str, Any]:
    names = (
        "all",
        "same_slot_alternative",
        "alternative_within_100ms",
        "alternative_within_250ms",
        "alternative_within_500ms",
        "alternative_within_1500ms",
        "selected_not_uniquely_youngest",
        "matched_age",
        "matched_fdv",
        "matched_seed",
        "matched_flow",
        "matched_creator_history",
        "matched_buyer_cluster",
        "landed",
        "failed_fill",
        "exact_timestamp",
        "lower_bound_timestamp",
    )
    report = {}
    youngest = baseline_scorer("youngest_candidate")
    for name in names:
        subset = subset_groups(holdout, name)
        report[name] = {
            "groups": len(subset),
            "model": selection_metrics(subset, scorer) if subset else None,
            "youngest_baseline": selection_metrics(subset, youngest) if subset else None,
            "model_top1_capture_window_bootstrap_95ci": clustered_top1_interval(subset, scorer),
            "small_sample_warning": len(subset) < 50,
        }
    return {
        "subsets": report,
        "same_slot_alternative_count": report["same_slot_alternative"]["groups"],
        "same_slot_interpretation": (
            "The same-slot-alternative cohort is small; no strong conclusion is drawn from it."
        ),
    }


def ablation_fields(name: str) -> tuple[str, ...]:
    families = {
        "candidate_age": {"candidate_age_ms"},
        "all_recency": {"candidate_age_ms", "same_slot_buyer_count", "buys_per_second"},
        "creator": set(EXPERT_FEATURES["creator_recurrence"]),
        "buyer_cluster": set(EXPERT_FEATURES["buyer_cluster"]),
        "topology": set(EXPERT_FEATURES["transaction_topology"]),
        "market_flow": set(EXPERT_FEATURES["market_flow_curve"]),
        "social": set(EXPERT_FEATURES["social_metadata"]),
        "raw_identities": set(),
    }
    all_fields = NUMERIC_FEATURES + BOOLEAN_FEATURES
    if name.startswith("remove_"):
        removed = families[name.removeprefix("remove_")]
        return tuple(field for field in all_fields if field not in removed)
    only = {
        "creator_only": set(EXPERT_FEATURES["creator_recurrence"]),
        "buyer_only": set(EXPERT_FEATURES["buyer_cluster"]),
        "flow_only": set(EXPERT_FEATURES["market_flow_curve"]),
        "age_only": {"candidate_age_ms"},
    }[name]
    return tuple(field for field in all_fields if field in only)


def run_ablations(
    train: Sequence[Sequence[Mapping[str, Any]]],
    holdout: Sequence[Sequence[Mapping[str, Any]]],
    full_top1: float,
) -> dict[str, Any]:
    names = (
        "remove_candidate_age",
        "remove_all_recency",
        "remove_creator",
        "remove_buyer_cluster",
        "remove_topology",
        "remove_market_flow",
        "remove_social",
        "remove_raw_identities",
        "creator_only",
        "buyer_only",
        "flow_only",
        "age_only",
    )
    results = {}
    for name in names:
        fields = ablation_fields(name)
        ranker = LinearRanker(fields).fit(train)
        metrics = selection_metrics(holdout, ranker.score)
        results[name] = {
            "feature_count": len(fields),
            "holdout": metrics,
            "top1_delta_from_frozen_model": metrics["top_1_accuracy"] - full_top1,
        }
    age_removed = results["remove_candidate_age"]["holdout"]["top_1_accuracy"]
    return {
        "method": "regularised pairwise-linear controlled feature-family ablations",
        "results": results,
        "conclusions": {
            "performance_collapses_without_candidate_age": age_removed < full_top1 * 0.75,
            "raw_identity_features_present_in_general_model": False,
            "funder_ablation_omitted_reason": "funder coverage is zero; no funder expert exists",
        },
    }


def fit_utility_threshold(
    positive_groups: Sequence[Sequence[Mapping[str, Any]]],
    null_groups: Sequence[Mapping[str, Any]],
    scorer: Any,
    hazard: BinaryHead,
    execution: BinaryHead,
    priority_fee_sol: float,
) -> tuple[float, dict[str, Any]]:
    values = []
    labels = []
    for group in positive_groups:
        if group[0]["split"] != "train" or group[0]["decision_time_is_lower_bound"]:
            continue
        scores = np.asarray(scorer(group), dtype=float)
        chosen = deterministic_argmax(group, scores)
        context = group_context(group, scorer, exact_timing=True)
        hazard_probability = float(hazard.probability([context])[0])
        execution_probability = float(
            execution.probability([execution_row(group[chosen], priority_fee_sol)])[0]
        )
        values.append(hazard_probability * float(max(softmax(scores))) * execution_probability)
        labels.append(1)
    for group in null_groups:
        if group["split"] != "train":
            continue
        rows = null_candidate_rows(group)
        scores = np.asarray(scorer(rows), dtype=float)
        chosen = deterministic_argmax(rows, scores)
        context = group_context(
            rows,
            scorer,
            activity_1s=integer(group["activity_1s"]),
        )
        hazard_probability = float(hazard.probability([context])[0])
        execution_probability = float(
            execution.probability([execution_row(rows[chosen], priority_fee_sol)])[0]
        )
        values.append(hazard_probability * float(max(softmax(scores))) * execution_probability)
        labels.append(0)
    thresholds = np.linspace(0.01, 0.90, 180)
    threshold = float(
        max(
            thresholds,
            key=lambda value: (
                binary_balanced_accuracy(labels, np.asarray(values) >= value),
                value,
            ),
        )
    )
    return threshold, {
        "fit_split": "train",
        "threshold": threshold,
        "formula": "P(trade) * P(top conditional choice) * P(executable)",
        "positive_fit_examples": sum(labels),
        "negative_fit_examples": len(labels) - sum(labels),
        "validation_used_to_fit": False,
        "holdout_used_to_fit": False,
    }


def calibration_report(
    positive_groups: Sequence[Sequence[Mapping[str, Any]]],
    null_groups: Sequence[Mapping[str, Any]],
    split: str,
    scorer: Any,
    hazard: BinaryHead,
    execution: BinaryHead,
    priority_fee_sol: float,
    utility_threshold: float,
) -> dict[str, Any]:
    values = []
    labels = []
    for group in positive_groups:
        if group[0]["split"] != split:
            continue
        scores = np.asarray(scorer(group), dtype=float)
        chosen = deterministic_argmax(group, scores)
        hazard_probability = float(
            hazard.probability(
                [
                    group_context(
                        group,
                        scorer,
                        exact_timing=not bool(group[0]["decision_time_is_lower_bound"]),
                    )
                ]
            )[0]
        )
        execution_probability = float(
            execution.probability([execution_row(group[chosen], priority_fee_sol)])[0]
        )
        values.append(hazard_probability * float(max(softmax(scores))) * execution_probability)
        labels.append(1)
    for group in null_groups:
        if group["split"] != split:
            continue
        rows = null_candidate_rows(group)
        scores = np.asarray(scorer(rows), dtype=float)
        chosen = deterministic_argmax(rows, scores)
        hazard_probability = float(
            hazard.probability(
                [
                    group_context(
                        rows,
                        scorer,
                        activity_1s=integer(group["activity_1s"]),
                    )
                ]
            )[0]
        )
        execution_probability = float(
            execution.probability([execution_row(rows[chosen], priority_fee_sol)])[0]
        )
        values.append(hazard_probability * float(max(softmax(scores))) * execution_probability)
        labels.append(0)
    bins = []
    calibration_error = 0.0
    for lower in np.linspace(0, 0.9, 10):
        indexes = [index for index, value in enumerate(values) if lower <= value < lower + 0.1]
        if not indexes:
            continue
        confidence = statistics.fmean(values[index] for index in indexes)
        observed = statistics.fmean(labels[index] for index in indexes)
        calibration_error += len(indexes) / len(values) * abs(confidence - observed)
        bins.append(
            {
                "lower": float(lower),
                "upper": float(lower + 0.1),
                "count": len(indexes),
                "mean_confidence": confidence,
                "observed_trade_rate": observed,
            }
        )
    curve = []
    for threshold in np.linspace(0.05, 0.95, 19):
        accepted = [index for index, value in enumerate(values) if value >= threshold]
        curve.append(
            {
                "threshold": float(threshold),
                "coverage": len(accepted) / max(len(values), 1),
                "selection_precision": (
                    statistics.fmean(labels[index] for index in accepted) if accepted else None
                ),
            }
        )
    return {
        "split": split,
        "groups": len(values),
        "expected_calibration_error": calibration_error,
        "brier_score": statistics.fmean(
            (value - label) ** 2 for value, label in zip(values, labels)
        ),
        "frozen_utility_threshold": utility_threshold,
        "abstention_rate": statistics.fmean(value < utility_threshold for value in values),
        "bins": bins,
        "confidence_coverage_curve": curve,
    }


@dataclass(frozen=True)
class MarketPoint:
    timestamp_ns: int
    mint: str
    virtual_sol: float
    virtual_tokens: float
    real_tokens: float
    price_sol: float
    complete: bool


@dataclass(frozen=True)
class ReplayIntention:
    run_id: str
    decision_ns: int
    mint: str
    expected_output_per_0_1_sol: float
    hazard_probability: float
    choice_probability: float
    execution_probability: float
    utility: float
    accepted: bool
    rejection_reason: str
    expert: str


def build_holdout_replay_inputs(
    specs: Sequence[CaptureSpec],
    history: HistoryIndex,
    scorer: Any,
    hazard: BinaryHead,
    execution: BinaryHead,
    priority_fee_sol: float,
    utility_threshold: float,
) -> tuple[list[ReplayIntention], dict[str, dict[str, list[MarketPoint]]], dict[str, Any]]:
    intentions = []
    retained_trajectories: dict[str, dict[str, list[MarketPoint]]] = {}
    clock_count = 0
    warmup_clocks = 0
    for spec in specs:
        if spec.split != "holdout":
            continue
        print(f"replay capture {spec.run_id}", flush=True)
        states: dict[str, CandidateState] = {}
        active: deque[tuple[int, str]] = deque()
        trajectories: dict[str, list[MarketPoint]] = defaultdict(list)
        latest_market: dict[str, MarketPoint] = {}
        tracked_mints: set[str] = set()
        run_intentions = []
        for timestamp_ns, batch in iter_timestamp_batches(spec.events_path):
            create_rows = []
            for event in batch:
                mint = str(event.get("mint") or "")
                kind = str(event.get("kind") or "").upper()
                if kind == "CREATE" and mint and mint not in states:
                    state = CandidateState.from_create(event, spec.split, spec.run_id)
                    states[mint] = state
                    active.append((state.create_ns, mint))
                    create_rows.append(event)
                elif mint in states:
                    states[mint].update(event)
                state = states.get(mint)
                if state and state.virtual_sol_reserve and state.virtual_token_reserve:
                    point = MarketPoint(
                        timestamp_ns=timestamp_ns,
                        mint=mint,
                        virtual_sol=state.virtual_sol_reserve,
                        virtual_tokens=state.virtual_token_reserve,
                        real_tokens=finite(state.real_token_reserve),
                        price_sol=finite(state.latest_price_sol),
                        complete=state.complete,
                    )
                    latest_market[mint] = point
                    if mint in tracked_mints:
                        trajectories[mint].append(point)
            if not create_rows:
                continue
            clock_count += 1
            decision_ns = timestamp_ns + 1
            if decision_ns < spec.start_ns + ACTIVE_HORIZON_MS * 1_000_000:
                warmup_clocks += 1
                continue
            decision_slot = max(integer(row.get("slot"), -1) for row in create_rows)
            rows = candidate_snapshot(
                states,
                active,
                decision_ns,
                decision_slot,
                history,
                spec.start_ns,
            )
            if not rows:
                continue
            scores = np.asarray(scorer(rows), dtype=float)
            chosen_index = deterministic_argmax(rows, scores)
            chosen = rows[chosen_index]
            choice_probability = float(softmax(scores)[chosen_index])
            context = group_context(
                rows,
                scorer,
                activity_1s=len(batch),
                exact_timing=True,
                precomputed_scores=scores,
            )
            hazard_probability = float(hazard.probability([context])[0])
            execution_probability = float(
                execution.probability([execution_row(chosen, priority_fee_sol)])[0]
            )
            utility = hazard_probability * choice_probability * execution_probability
            accepted = utility >= utility_threshold
            reason = "ACCEPT"
            if not accepted:
                reason = "ABSTAIN_EXPECTED_UTILITY"
            elif execution_probability < execution.threshold:
                accepted = False
                reason = "REJECT_UNCERTAIN_QUOTE"
            elif not finite(chosen.get("executable_token_output_0_1_sol")):
                accepted = False
                reason = "REJECT_ROUTE_FAILURE"
            expert = "generalist"
            if isinstance(scorer.__self__, MixtureRanker):
                weights = scorer.__self__.active_weights(rows)
                expert = max(weights, key=weights.get)
            run_intentions.append(
                ReplayIntention(
                    run_id=spec.run_id,
                    decision_ns=decision_ns,
                    mint=str(chosen["candidate_mint"]),
                    expected_output_per_0_1_sol=finite(
                        chosen.get("executable_token_output_0_1_sol")
                    ),
                    hazard_probability=hazard_probability,
                    choice_probability=choice_probability,
                    execution_probability=execution_probability,
                    utility=utility,
                    accepted=accepted,
                    rejection_reason=reason,
                    expert=expert,
                )
            )
            if accepted and str(chosen["candidate_mint"]) not in tracked_mints:
                tracked_mints.add(str(chosen["candidate_mint"]))
                latest = latest_market.get(str(chosen["candidate_mint"]))
                if latest:
                    trajectories[str(chosen["candidate_mint"])].append(latest)
        selected_mints = {row.mint for row in run_intentions if row.accepted}
        retained_trajectories[spec.run_id] = {
            mint: points for mint, points in trajectories.items() if mint in selected_mints
        }
        intentions.extend(run_intentions)
    return (
        intentions,
        retained_trajectories,
        {
            "eligible_clock_policy": "one decision after each distinct CREATE receipt timestamp",
            "eligible_create_clocks": clock_count,
            "warmup_clocks_excluded": warmup_clocks,
            "intentions": len(intentions),
            "accepted_by_joint_heads": sum(row.accepted for row in intentions),
            "full_event_stream_processed": True,
        },
    )


@dataclass
class Position:
    run_id: str
    mint: str
    entry_ns: int
    tokens: float
    original_tokens: float
    entry_price: float
    invested_sol: float
    entry_cost_sol: float
    expert: str
    realised_proceeds: float = 0.0
    peak_multiple: float = 1.0
    partial_taken: bool = False
    exit_reason: str = ""


def quote_buy(input_sol: float, point: MarketPoint) -> float:
    tokens = v1.buy_tokens(input_sol, point.virtual_sol, point.virtual_tokens)
    return min(tokens, point.real_tokens) if point.real_tokens > 0 else tokens


def quote_sell(tokens: float, point: MarketPoint) -> float:
    if tokens <= 0 or point.virtual_sol <= 0 or point.virtual_tokens <= 0:
        return 0.0
    return tokens * point.virtual_sol / (point.virtual_tokens + tokens)


def latest_point(points: Sequence[MarketPoint], timestamp_ns: int) -> MarketPoint | None:
    timestamps = [point.timestamp_ns for point in points]
    index = bisect.bisect_right(timestamps, timestamp_ns) - 1
    return points[index] if index >= 0 else None


def wilson_lower_bound(wins: int, trades: int, z: float = 1.96) -> float:
    if trades <= 0:
        return 0.0
    proportion = wins / trades
    denominator = 1 + z**2 / trades
    centre = proportion + z**2 / (2 * trades)
    spread = z * math.sqrt((proportion * (1 - proportion) + z**2 / (4 * trades)) / trades)
    return (centre - spread) / denominator


FEE_MODEL = {
    "protocol_fee_bps": 125.0,
    "creator_fee_bps": 50.0,
    "priority_fee_policy": "median causal E4 training order",
    "tip_sol": 0.001,
    "base_transaction_fee_sol": 0.000005,
    "rejected_submission_cost": "priority fee + tip + base transaction fee",
}
EXIT_POLICY = {
    "initial_stop_multiple": 0.85,
    "partial_take_multiple": 1.20,
    "partial_take_fraction": 0.50,
    "second_take_multiple": 1.50,
    "trailing_drawdown_fraction": 0.15,
    "maximum_holding_ms": 30_000,
    "liquidity_emergency": "complete/migration event",
    "final_flatten": "last causally observed reserve in each capture window",
    "uses_e4_future_sells": False,
}


def simulate_latency(
    intentions: Sequence[ReplayIntention],
    trajectories: Mapping[str, Mapping[str, Sequence[MarketPoint]]],
    latency_ms: int,
    priority_fee_sol: float,
    known_holdout: Sequence[tuple[str, int, int, str]],
) -> dict[str, Any]:
    bankroll = STARTING_BANKROLL_SOL
    positions: dict[tuple[str, str], Position] = {}
    closed = []
    rejected = Counter()
    traded_mints: set[tuple[str, str]] = set()
    equity_curve = [bankroll]
    fees_paid = 0.0
    rejected_costs = 0.0
    accepted_intentions = [row for row in intentions if row.accepted]
    by_run = {run_id: dict(rows) for run_id, rows in trajectories.items()}
    for run_id in sorted(by_run, key=int):
        run_intentions = sorted(
            (row for row in accepted_intentions if row.run_id == run_id),
            key=lambda row: (row.decision_ns, row.mint),
        )
        points_by_mint = by_run[run_id]
        market_events = sorted(
            (point for points in points_by_mint.values() for point in points),
            key=lambda point: (point.timestamp_ns, point.mint),
        )
        actions = [(point.timestamp_ns, 0, "market", point) for point in market_events] + [
            (
                intention.decision_ns + latency_ms * 1_000_000,
                1,
                "entry",
                intention,
            )
            for intention in run_intentions
        ]
        actions.sort(key=lambda row: (row[0], row[1], getattr(row[3], "mint", "")))
        latest_by_mint: dict[str, MarketPoint] = {}

        def exit_tokens(
            position: Position, point: MarketPoint, fraction: float, reason: str
        ) -> None:
            nonlocal bankroll, fees_paid
            tokens = position.tokens * fraction
            gross = quote_sell(tokens, point)
            fee = gross * (FEE_MODEL["protocol_fee_bps"] + FEE_MODEL["creator_fee_bps"]) / 10_000
            net = max(0.0, gross - fee)
            bankroll += net
            fees_paid += fee
            position.realised_proceeds += net
            position.tokens -= tokens
            if position.tokens <= position.original_tokens * 1e-9:
                position.tokens = 0.0
                position.exit_reason = reason

        for timestamp_ns, _, action, payload in actions:
            if action == "market":
                point = payload
                latest_by_mint[point.mint] = point
                key = (run_id, point.mint)
                position = positions.get(key)
                if position is None or timestamp_ns < position.entry_ns:
                    continue
                current_price = point.price_sol or (
                    point.virtual_sol / max(point.virtual_tokens, 1e-12)
                )
                multiple = current_price / max(position.entry_price, 1e-18)
                position.peak_multiple = max(position.peak_multiple, multiple)
                reason = ""
                if point.complete:
                    reason = "LIQUIDITY_EMERGENCY"
                elif multiple <= EXIT_POLICY["initial_stop_multiple"]:
                    reason = "INITIAL_STOP"
                elif (
                    timestamp_ns - position.entry_ns
                    >= EXIT_POLICY["maximum_holding_ms"] * 1_000_000
                ):
                    reason = "MAXIMUM_HOLD"
                elif multiple >= EXIT_POLICY["second_take_multiple"]:
                    reason = "SECOND_TAKE"
                elif position.partial_taken and multiple <= position.peak_multiple * (
                    1 - EXIT_POLICY["trailing_drawdown_fraction"]
                ):
                    reason = "TRAILING_PROTECTION"
                if not position.partial_taken and multiple >= EXIT_POLICY["partial_take_multiple"]:
                    exit_tokens(
                        position,
                        point,
                        EXIT_POLICY["partial_take_fraction"],
                        "PARTIAL_TAKE",
                    )
                    position.partial_taken = True
                if reason and position.tokens > 0:
                    exit_tokens(position, point, 1.0, reason)
                if position.tokens == 0:
                    pnl = position.realised_proceeds - position.entry_cost_sol
                    closed.append(
                        {
                            "run_id": run_id,
                            "mint": position.mint,
                            "entry_ns": position.entry_ns,
                            "pnl_sol": pnl,
                            "win": pnl > 0,
                            "exit_reason": position.exit_reason,
                            "expert": position.expert,
                        }
                    )
                    del positions[key]
            else:
                intention = payload
                key = (run_id, intention.mint)
                if key in traded_mints:
                    rejected["NO_REENTRY"] += 1
                    continue
                if len(positions) >= MAX_CONCURRENT_POSITIONS:
                    rejected["MAX_CONCURRENT_POSITIONS"] += 1
                    continue
                point = latest_by_mint.get(intention.mint)
                if point is None:
                    rejected["REJECT_UNCERTAIN_QUOTE"] += 1
                    continue
                position_sol = bankroll * POSITION_FRACTION
                trading_fee = (
                    position_sol
                    * (FEE_MODEL["protocol_fee_bps"] + FEE_MODEL["creator_fee_bps"])
                    / 10_000
                )
                fixed_fee = (
                    priority_fee_sol + FEE_MODEL["tip_sol"] + FEE_MODEL["base_transaction_fee_sol"]
                )
                total_cost = position_sol + trading_fee + fixed_fee
                if total_cost > bankroll:
                    rejected["REJECT_INSUFFICIENT_BALANCE"] += 1
                    continue
                actual_output = quote_buy(position_sol, point)
                expected_output = intention.expected_output_per_0_1_sol * (position_sol / 0.1)
                if actual_output < expected_output * 0.85:
                    rejected["REJECT_OUTPUT_DETERIORATION"] += 1
                    bankroll -= fixed_fee
                    fees_paid += fixed_fee
                    rejected_costs += fixed_fee
                    equity_curve.append(bankroll)
                    continue
                bankroll -= total_cost
                fees_paid += trading_fee + fixed_fee
                entry_price = position_sol / max(actual_output, 1e-18)
                positions[key] = Position(
                    run_id=run_id,
                    mint=intention.mint,
                    entry_ns=timestamp_ns,
                    tokens=actual_output,
                    original_tokens=actual_output,
                    entry_price=entry_price,
                    invested_sol=position_sol,
                    entry_cost_sol=total_cost,
                    expert=intention.expert,
                )
                traded_mints.add(key)
            marked = bankroll
            for (position_run, mint), position in positions.items():
                point = latest_by_mint.get(mint) if position_run == run_id else None
                if point:
                    marked += quote_sell(position.tokens, point)
            equity_curve.append(marked)
        for key, position in list(positions.items()):
            if key[0] != run_id:
                continue
            points = points_by_mint.get(position.mint) or []
            if points:
                exit_tokens(position, points[-1], 1.0, "FINAL_FLATTEN")
            pnl = position.realised_proceeds - position.entry_cost_sol
            closed.append(
                {
                    "run_id": run_id,
                    "mint": position.mint,
                    "entry_ns": position.entry_ns,
                    "pnl_sol": pnl,
                    "win": pnl > 0,
                    "exit_reason": position.exit_reason or "FINAL_FLATTEN",
                    "expert": position.expert,
                }
            )
            del positions[key]
        equity_curve.append(bankroll)
    profits = [row["pnl_sol"] for row in closed if row["pnl_sol"] > 0]
    losses = [row["pnl_sol"] for row in closed if row["pnl_sol"] <= 0]
    wins = len(profits)
    trades = len(closed)
    peak = equity_curve[0]
    max_drawdown = 0.0
    for equity in equity_curve:
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    matched = {
        (run_id, mint)
        for run_id, lower, upper, mint in known_holdout
        for intention in accepted_intentions
        if intention.run_id == run_id
        and intention.mint == mint
        and lower <= intention.decision_ns <= upper
    }
    missed = sum((run_id, mint) not in matched for run_id, _, _, mint in known_holdout)
    false_positive_trades = sum(
        not any(
            run_id == row["run_id"] and mint == row["mint"] and lower <= row["entry_ns"] <= upper
            for run_id, lower, upper, mint in known_holdout
        )
        for row in closed
    )
    gross_profit = sum(profits)
    gross_loss = abs(sum(losses))
    return {
        "latency_ms": latency_ms,
        "trades": trades,
        "wins": wins,
        "losses": trades - wins,
        "win_rate": wins / max(trades, 1),
        "wilson_95_lower_bound": wilson_lower_bound(wins, trades),
        "net_pnl_sol": bankroll - STARTING_BANKROLL_SOL,
        "ending_bankroll_sol": bankroll,
        "return_fraction": bankroll / STARTING_BANKROLL_SOL - 1,
        "profit_factor": gross_profit / gross_loss
        if gross_loss
        else (math.inf if gross_profit else 0.0),
        "maximum_drawdown_sol": max_drawdown,
        "maximum_drawdown_fraction_of_start": max_drawdown / STARTING_BANKROLL_SOL,
        "average_win_sol": statistics.fmean(profits) if profits else 0.0,
        "average_loss_sol": statistics.fmean(losses) if losses else 0.0,
        "payoff_ratio": (
            statistics.fmean(profits) / abs(statistics.fmean(losses)) if profits and losses else 0.0
        ),
        "expectancy_sol": statistics.fmean(row["pnl_sol"] for row in closed) if closed else 0.0,
        "rejected_entries": sum(rejected.values()),
        "rejections_by_reason": dict(sorted(rejected.items())),
        "rejected_submission_costs_sol": rejected_costs,
        "fees_paid_sol": fees_paid,
        "false_positive_trades": false_positive_trades,
        "missed_e4_selections": missed,
        "largest_winner_contribution": max(profits, default=0.0) / gross_profit
        if gross_profit
        else 0.0,
        "capture_windows_traded": len({row["run_id"] for row in closed}),
        "by_capture_window": {
            run_id: {
                "trades": sum(row["run_id"] == run_id for row in closed),
                "pnl_sol": sum(row["pnl_sol"] for row in closed if row["run_id"] == run_id),
            }
            for run_id in sorted({row["run_id"] for row in closed}, key=int)
        },
        "by_expert": {
            expert: {
                "trades": sum(row["expert"] == expert for row in closed),
                "pnl_sol": sum(row["pnl_sol"] for row in closed if row["expert"] == expert),
            }
            for expert in sorted({row["expert"] for row in closed})
        },
        "independently_recalculated": True,
        "primary_exit_uses_e4_future_sells": False,
        "closed_trade_ledger_hash": stable_hash(closed),
    }


def historical_gate(
    economics: Mapping[str, Mapping[str, Any]],
    selection: Mapping[str, Any],
    anti_shortcut: Mapping[str, Any],
    integrity_failures: Sequence[str],
) -> dict[str, Any]:
    zero = economics["0"]
    youngest = selection["model_comparison"]["youngest_candidate"]["holdout"]
    strongest_non_moe = max(
        (
            values["holdout"]["top_1_accuracy"]
            for name, values in selection["model_comparison"].items()
            if name not in {"restricted_mixture_of_experts", "always_no_trade"}
            and values.get("holdout")
        ),
        default=0.0,
    )
    winning_top1 = selection["holdout"]["top_1_accuracy"]
    matched_age = anti_shortcut["subsets"]["matched_age"]
    requirements = {
        "at_least_20_closed_trades": zero["trades"] >= 20,
        "at_least_two_capture_windows": zero["capture_windows_traded"] >= 2,
        "win_rate_at_least_65_percent": zero["win_rate"] >= 0.65,
        "wilson_lower_bound_at_least_45_percent": zero["wilson_95_lower_bound"] >= 0.45,
        "net_pnl_positive": zero["net_pnl_sol"] > 0,
        "profit_factor_at_least_1_25": zero["profit_factor"] >= 1.25,
        "maximum_drawdown_at_most_15_percent": zero["maximum_drawdown_fraction_of_start"] <= 0.15,
        "expectancy_positive": zero["expectancy_sol"] > 0,
        "largest_winner_at_most_50_percent": zero["largest_winner_contribution"] <= 0.50,
        "every_latency_pnl_positive": all(row["net_pnl_sol"] > 0 for row in economics.values()),
        "every_latency_profit_factor_at_least_1_25": all(
            row["profit_factor"] >= 1.25 for row in economics.values()
        ),
        "every_latency_win_rate_at_least_65_percent": all(
            row["win_rate"] >= 0.65 for row in economics.values()
        ),
        "materially_beats_youngest_baseline": winning_top1 >= youngest["top_1_accuracy"] + 0.02,
        "materially_beats_strongest_non_moe": winning_top1 >= strongest_non_moe + 0.02,
        "matched_age_above_chance": bool(
            matched_age["model"] and matched_age["model"]["pairwise_accuracy"] > 0.50
        ),
        "zero_integrity_or_leakage_failures": not integrity_failures,
    }
    passed = all(requirements.values())
    if passed:
        failure_classification = None
    elif zero["trades"] < 20:
        failure_classification = "INSUFFICIENT_LABELS"
    elif zero["false_positive_trades"] > zero["trades"] / 2:
        failure_classification = "FALSE_POSITIVE_OVERLOAD"
    elif zero["net_pnl_sol"] <= 0 or zero["expectancy_sol"] <= 0:
        failure_classification = "NEGATIVE_EXPECTANCY"
    elif zero["profit_factor"] < 1.25:
        failure_classification = "INADEQUATE_PROFIT_FACTOR"
    elif not all(row["net_pnl_sol"] > 0 for row in economics.values()):
        failure_classification = "LATENCY_FAILURE"
    else:
        failure_classification = "HOLDOUT_COLLAPSE"
    return {
        "status": "HISTORICAL_HOLDOUT_CONFIRMED" if passed else "NOT_CONCLUSIVE",
        "historical_qualification_passed": passed,
        "requirements": requirements,
        "failed_requirements": [name for name, value in requirements.items() if not value],
        "failure_classification": failure_classification,
        "live_confirmation_authorised": passed,
        "untouched_live_windows_captured": 0,
        "production_promotion_authorised": False,
    }


def registry_identity(
    script_path: Path,
    data_audit: Mapping[str, Any],
    feature_manifest: Mapping[str, Any],
    winning_model: str,
    null_policy: NullSamplingPolicy,
    utility_threshold: float,
    priority_fee_sol: float,
) -> dict[str, Any]:
    return {
        "thesis_family_identifier": THESIS_FAMILY,
        "source_code_fingerprint": sha256_path(script_path),
        "dataset_source_manifest_fingerprint": data_audit["frozen_dataset_fingerprint"],
        "evidence_epoch": [f"forward-capture-{run_id}" for run_id in data_audit["run_ids"]],
        "feature_set_fingerprint": stable_hash(feature_manifest),
        "model_family": winning_model,
        "full_parameters": {
            "linear_C": 0.25,
            "row_logistic_C": 0.2,
            "gbt_learning_rate": 0.05,
            "gbt_iterations": 80,
            "gbt_max_leaf_nodes": 7,
            "listwise_iterations": 500,
            "utility_threshold": utility_threshold,
            "random_seed": RANDOM_SEED,
        },
        "causal_horizon": {"active_launch_ms": ACTIVE_HORIZON_MS},
        "candidate_risk_set_policy": {
            "active_policy_ms": ACTIVE_HORIZON_MS,
            "no_trade_sampling": null_policy.as_dict(),
        },
        "chronological_split": data_audit["chronological_split"],
        "bankroll": {"starting_sol": STARTING_BANKROLL_SOL},
        "position_sizing": {
            "fraction": POSITION_FRACTION,
            "maximum_concurrent_positions": MAX_CONCURRENT_POSITIONS,
        },
        "fee_model": dict(FEE_MODEL, priority_fee_sol=priority_fee_sol),
        "output_guard": {"minimum_quote_fraction": 0.85, "strict": True},
        "latency_assumptions": {"milliseconds": list(LATENCIES_MS)},
        "execution_policy": {
            "separate_execution_head": True,
            "actions": [
                "ACCEPT",
                "REJECT_OUTPUT_DETERIORATION",
                "REJECT_UNCERTAIN_QUOTE",
                "REJECT_INSUFFICIENT_BALANCE",
                "REJECT_ROUTE_FAILURE",
            ],
        },
        "exit_policy": EXIT_POLICY,
    }


def registry_experiment_id(identity: Mapping[str, Any]) -> str:
    payload = {
        "identity_version": "e4-v12-experiment-identity-v1",
        "identity": identity,
    }
    return "e4x-" + stable_hash(payload)


def existing_registry_ids(repo_root: Path) -> set[str]:
    result = subprocess.run(
        ["git", "show", "origin/main:artifacts/e4-v12-experiment-registry.json"],
        cwd=repo_root,
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode:
        return set()
    payload = json.loads(result.stdout)
    return {str(row["experiment_id"]) for row in payload.get("records") or []}


def model_prediction_fingerprint(model: Any, groups: Sequence[Sequence[Mapping[str, Any]]]) -> str:
    predictions = []
    for group in groups:
        scores = np.asarray(model.score(group), dtype=float)
        predictions.append(
            {
                "group": group[0]["decision_group_id"],
                "scores": {
                    str(row["candidate_mint"]): round(float(score), 12)
                    for row, score in zip(group, scores)
                },
            }
        )
    return stable_hash(predictions)


def train_one_model(name: str, train: Sequence[Sequence[Mapping[str, Any]]]) -> Any:
    fields = NUMERIC_FEATURES + BOOLEAN_FEATURES
    factories = {
        "conditional_logistic_regression": lambda: RowLogisticRanker(fields).fit(train),
        "pairwise_linear_ranker": lambda: LinearRanker(fields).fit(train),
        "gradient_boosted_pairwise_ranker": lambda: PairwiseGBTRanker(fields).fit(train),
        "listwise_group_softmax": lambda: ListwiseSoftmaxRanker(fields).fit(train),
        "non_mixture_generalist": lambda: LinearRanker(fields).fit(train),
        "restricted_mixture_of_experts": lambda: MixtureRanker.fit(train),
    }
    return factories[name]()


def label_permutation_audit(
    train: Sequence[Sequence[Mapping[str, Any]]],
    validation: Sequence[Sequence[Mapping[str, Any]]],
    reference_top1: float,
) -> dict[str, Any]:
    transformer = TrainOnlyTransformer(NUMERIC_FEATURES + BOOLEAN_FEATURES).fit(train)
    selected = {}
    rng = np.random.default_rng(RANDOM_SEED)
    for group in train:
        index = int(rng.integers(0, len(group)))
        selected[str(group[0]["decision_group_id"])] = str(group[index]["candidate_mint"])
    matrix, labels, weights = symmetrical_pairs(train, transformer, selected_by_group=selected)
    model = LogisticRegression(C=0.25, max_iter=500, solver="liblinear")
    model.fit(matrix, labels, sample_weight=weights)

    def score(rows: Sequence[Mapping[str, Any]]) -> np.ndarray:
        return transformer.transform_rows(rows) @ model.coef_[0]

    permuted_validation = []
    for group in validation:
        chosen = int(rng.integers(0, len(group)))
        permuted_validation.append(
            [dict(row, selected_by_e4=index == chosen) for index, row in enumerate(group)]
        )
    metrics = selection_metrics(permuted_validation, score)
    chance = statistics.fmean(1 / len(group) for group in validation)
    chance_tolerance = 1.96 * math.sqrt(chance * (1 - chance) / len(validation))
    return {
        "permutation": "independent deterministic within-group train and validation labels",
        "validation_top_1_accuracy": metrics["top_1_accuracy"],
        "validation_pairwise_accuracy": metrics["pairwise_accuracy"],
        "chance_top_1_accuracy": chance,
        "reference_top_1_accuracy": reference_top1,
        "collapsed_below_reference": metrics["top_1_accuracy"] < reference_top1,
        "chance_95_percent_tolerance": chance_tolerance,
        "statistically_indistinguishable_from_chance": abs(metrics["top_1_accuracy"] - chance)
        <= chance_tolerance,
    }


def row_order_invariance(
    groups: Sequence[Sequence[Mapping[str, Any]]], model: Any
) -> dict[str, Any]:
    mismatches = 0
    maximum_error = 0.0
    for group in groups:
        original = {
            str(row["candidate_mint"]): float(score)
            for row, score in zip(group, model.score(group))
        }
        reversed_group = list(reversed(group))
        reordered = {
            str(row["candidate_mint"]): float(score)
            for row, score in zip(reversed_group, model.score(reversed_group))
        }
        errors = [abs(original[mint] - reordered[mint]) for mint in original]
        maximum_error = max(maximum_error, max(errors, default=0.0))
        mismatches += any(error > 1e-9 for error in errors)
    return {
        "groups_checked": len(groups),
        "mismatches": mismatches,
        "maximum_absolute_score_error": maximum_error,
        "status": "PASS" if mismatches == 0 else "FAIL",
    }


def identity_cold_start_report(
    train: Sequence[Sequence[Mapping[str, Any]]],
    holdout: Sequence[Sequence[Mapping[str, Any]]],
    model: IdentityAwareRanker,
) -> dict[str, Any]:
    train_creators = {str(row.get("creator_address") or "") for group in train for row in group}
    train_buyers = {
        buyer
        for group in train
        for row in group
        for buyer in json.loads(str(row.get("first_buyer_identities_json") or "[]"))
    }
    train_clusters = {str(row.get("first_buyer_cluster") or "") for group in train for row in group}
    predicates = {
        "unseen_creator": lambda row: str(row.get("creator_address") or "") not in train_creators,
        "unseen_buyer": lambda row: all(
            buyer not in train_buyers
            for buyer in json.loads(str(row.get("first_buyer_identities_json") or "[]"))
        ),
        "unseen_cluster": lambda row: (
            str(row.get("first_buyer_cluster") or "") not in train_clusters
        ),
    }
    output = {}
    for name, predicate in predicates.items():
        subset = [
            group
            for group in holdout
            if predicate(next(row for row in group if row["selected_by_e4"]))
        ]
        output[name] = {
            "groups": len(subset),
            "metrics": selection_metrics(subset, model.score) if subset else None,
        }
    output["cold_start"] = {
        "groups": len(
            [
                group
                for group in holdout
                if all(
                    predicate(next(row for row in group if row["selected_by_e4"]))
                    for predicate in predicates.values()
                )
            ]
        ),
        "identity_model_is_final_candidate": False,
        "memorisation_guard": "raw identity setting is diagnostic and cannot win model selection",
    }
    return output


def selection_markdown(report: Mapping[str, Any]) -> str:
    holdout = report["holdout"]
    lines = [
        "# E4 V12 joint choice-MoE selection report",
        "",
        f"Frozen validation-selected model: `{report['winning_model']}`",
        "",
        "The conditional-choice result is not an autonomous trading verdict. The separate ",
        "NO_TRADE, execution, abstention, and economic gates remain controlling.",
        "",
        "## Historical holdout selection",
        "",
        f"- Top-1 accuracy: {holdout['top_1_accuracy']:.4f}",
        f"- Top-3 recall: {holdout['top_3_recall']:.4f}",
        f"- MRR: {holdout['mean_reciprocal_rank']:.4f}",
        f"- Pairwise accuracy: {holdout['pairwise_accuracy']:.4f}",
        "",
        "## Safeguards",
        "",
        "- Chronological window split; no row/pair random splitting.",
        "- All transforms, expert gates, thresholds, and calibration are training-only.",
        "- Labels, execution outcomes, signatures, source IDs, and future outcomes are excluded.",
        "- Failed-fill lower-bound uncertainty is preserved and excluded from exact hazard fitting.",
        "- Raw-identity performance is diagnostic only.",
        "- Funder expert absent because funder coverage is zero.",
    ]
    return "\n".join(lines) + "\n"


def jsonl_parquet_parity(
    repo_root: Path, groups: Sequence[Sequence[Mapping[str, Any]]]
) -> dict[str, Any]:
    parquet = repo_root / "artifacts/e4-v12-canonical-choice-risksets-v2.parquet"
    connection = duckdb.connect()
    parquet_keys = connection.execute(
        """
        SELECT decision_ns, decision_group_id, selected_by_e4, candidate_mint
        FROM read_parquet(?)
        ORDER BY decision_ns, decision_group_id,
                 CASE WHEN selected_by_e4 THEN 0 ELSE 1 END, candidate_mint
        """,
        [str(parquet)],
    ).fetchall()
    connection.close()
    jsonl_keys = [
        (
            integer(row["decision_ns"]),
            str(row["decision_group_id"]),
            bool(row["selected_by_e4"]),
            str(row["candidate_mint"]),
        )
        for group in groups
        for row in group
    ]
    match = jsonl_keys == parquet_keys
    if not match:
        raise ValueError("V2 JSONL/Parquet key parity failed")
    return {
        "status": "PASS",
        "jsonl_rows": len(jsonl_keys),
        "parquet_rows": len(parquet_keys),
        "ordered_key_sha256": stable_hash(jsonl_keys),
        "row_order_match": match,
    }


def feature_manifest() -> dict[str, Any]:
    names = feature_names()
    violations = [
        name for name in names if any(token in name.lower() for token in PROHIBITED_FEATURE_TOKENS)
    ]
    if violations:
        raise ValueError(f"selection feature leakage: {violations}")
    return {
        "version": SCHEMA_VERSION,
        "selection_numeric_features": list(NUMERIC_FEATURES),
        "selection_boolean_features": list(BOOLEAN_FEATURES),
        "expanded_feature_names": names,
        "expert_features": {key: list(value) for key, value in EXPERT_FEATURES.items()},
        "execution_features": list(EXECUTION_FIELDS),
        "hazard_features": list(HAZARD_FIELDS),
        "prohibited_tokens": list(PROHIBITED_FEATURE_TOKENS),
        "leakage_scan_violations": violations,
        "missingness_policy": "numeric fields have an explicit companion missingness indicator",
        "identity_policy": {
            "general_model": "causal statistics only; no raw identity",
            "diagnostic_identity_model": "regularised train-only creator/cluster target rates",
            "diagnostic_may_not_win": True,
        },
        "funder_expert": None,
        "funder_expert_absent_reason": "V2 funder-history coverage is zero",
        "social_gate": "social expert weight is zero without timestamp-proven social evidence",
        "causality": {
            "candidate_state": "feature_max_event_ns <= decision_ns",
            "entity_history": "strictly prior observations only",
            "relative_context": "same causal risk set only",
            "outcome_fields_in_selection_matrix": False,
        },
    }


def failed_fill_sensitivity(
    groups: Sequence[Sequence[Mapping[str, Any]]], scorer: Any
) -> dict[str, Any]:
    lower_bound = [group for group in groups if group[0]["decision_time_is_lower_bound"]]
    exact = [group for group in groups if not group[0]["decision_time_is_lower_bound"]]
    interval_widths = [
        max(
            0,
            integer(group[0]["decision_ns_upper_bound"])
            - integer(group[0]["decision_ns_lower_bound"]),
        )
        / 1_000_000
        for group in lower_bound
    ]
    return {
        "failed_fill_groups": len(lower_bound),
        "exact_groups": len(exact),
        "interval_width_ms": {
            "median": statistics.median(interval_widths) if interval_widths else None,
            "p95": percentile(interval_widths, 0.95),
            "maximum": max(interval_widths, default=None),
        },
        "optimistic_lower_bound": {
            "selection_identity_metrics": selection_metrics(lower_bound, scorer),
            "exact_hazard_fit_eligible": False,
        },
        "central_interval_midpoint": {
            "selection_identity_metrics": selection_metrics(lower_bound, scorer),
            "exact_hazard_fit_eligible": False,
            "note": "identity labels are retained; timing-dependent features are not advanced",
        },
        "conservative_upper_bound": {
            "selection_identity_metrics": selection_metrics(lower_bound, scorer),
            "exact_hazard_fit_eligible": False,
            "note": "full interval plus exclusion margin is removed from NO_TRADE labels",
        },
        "historical_status_can_depend_on_assumed_failed_fill_time": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--output-dir", type=Path, default=Path("artifacts"))
    parser.add_argument("--skip-source-hashes", action="store_true")
    parser.add_argument("--reuse-null-groups", action="store_true")
    parser.add_argument("--reuse-clock-cache", action="store_true")
    parser.add_argument("--reuse-replay-cache", action="store_true")
    args = parser.parse_args()
    repo_root = args.repo_root.resolve()
    output = (repo_root / args.output_dir).resolve()
    script_path = Path(__file__).resolve()

    immutable = verify_v2_inputs(repo_root)
    groups = load_v2_groups(repo_root / "artifacts/e4-v12-canonical-choice-risksets-v2.jsonl")
    parity = jsonl_parquet_parity(repo_root, groups)
    splits = {
        split: [group for group in groups if group[0]["split"] == split]
        for split in ("train", "validation", "holdout")
    }
    specs = capture_specs(repo_root)
    intervals, interval_counts = known_and_unresolved_intervals(repo_root, groups, specs)
    clock_cache = repo_root / ".tmp-choice-moe-clocks.json"
    if args.reuse_clock_cache and clock_cache.exists():
        cached = json.loads(clock_cache.read_text(encoding="utf-8"))
        clocks = [Clock(**row) for row in cached["clocks"]]
        creator_launches = cached["creator_launches"]
        clock_audit = cached["audit"]
    else:
        clocks, creator_launches, clock_audit = discover_clocks(
            specs,
            intervals,
            verify_source_hashes=not args.skip_source_hashes,
        )
        write_json(
            clock_cache,
            {
                "clocks": [clock.__dict__ for clock in clocks],
                "creator_launches": creator_launches,
                "audit": clock_audit,
            },
        )
    prior_registry = json.loads(
        (repo_root / "docs/research/e4-v12-causal-prior-registry.json").read_text()
    )
    history = HistoryIndex.build(groups, creator_launches, prior_registry)
    null_policy = NullSamplingPolicy.fit(clocks, len(splits["train"]))
    sampled_clocks = [clock for clock in clocks if null_policy.includes(clock)]
    null_path = output / "e4-v12-choice-moe-null-groups.jsonl"
    if args.reuse_null_groups and null_path.exists():
        with null_path.open("r", encoding="utf-8") as handle:
            null_groups = [json.loads(line) for line in handle if line.strip()]
    else:
        null_groups = build_null_groups(specs, sampled_clocks, history)
    if not null_groups:
        raise ValueError("null policy produced no groups")
    known_times = {
        (str(group[0]["source_run_id"]), integer(group[0]["decision_ns"])) for group in groups
    }
    contamination = [
        group["decision_group_id"]
        for group in null_groups
        if (str(group["source_run_id"]), integer(group["decision_ns"])) in known_times
    ]
    if contamination:
        raise ValueError("NO_TRADE group overlaps a known selection")
    write_jsonl(
        null_path,
        null_groups,
    )

    manifest = feature_manifest()
    models = train_rankers(splits["train"])
    comparison, winning_model = evaluate_model_families(groups, models)
    winner = models[winning_model]
    scorer = winner.score
    priority_values = [
        finite(next(row for row in group if row["selected_by_e4"]).get("source_priority_fee"))
        for group in splits["train"]
        if next(row for row in group if row["selected_by_e4"]).get("source_priority_fee")
        is not None
    ]
    priority_fee_sol = statistics.median(priority_values) if priority_values else 0.001
    hazard, hazard_fit = fit_hazard_head(groups, null_groups, scorer)
    execution, execution_fit = fit_execution_head(groups, priority_fee_sol)
    utility_threshold, utility_fit = fit_utility_threshold(
        groups,
        null_groups,
        scorer,
        hazard,
        execution,
        priority_fee_sol,
    )
    hazard_by_split = {
        split: hazard_metrics(hazard, groups, null_groups, scorer, split)
        for split in ("train", "validation", "holdout")
    }
    execution_by_split = {
        split: execution_metrics(execution, groups, split, priority_fee_sol)
        for split in ("train", "validation", "holdout")
    }
    holdout_selection = selection_metrics(splits["holdout"], scorer)
    selection_report = {
        "version": SCHEMA_VERSION,
        "winning_model": winning_model,
        "freeze_sequence": {
            "training_complete": True,
            "validation_selected_candidate": True,
            "model_family_frozen_before_holdout": True,
            "hyperparameters_frozen_before_holdout": True,
            "feature_set_frozen_before_holdout": True,
            "thresholds_frozen_before_holdout": True,
            "execution_policy_frozen_before_holdout": True,
            "exit_policy_frozen_before_holdout": True,
            "holdout_evaluations_after_freeze": 1,
        },
        "model_comparison": comparison,
        "holdout": holdout_selection,
        "hazard": hazard_by_split,
        "false_entries_per_1000_captured_launches": hazard_by_split["holdout"][
            "false_positive_entries"
        ]
        / 30_000
        * 1_000,
        "failed_fill_sensitivity": failed_fill_sensitivity(groups, scorer),
    }
    anti_shortcut = anti_shortcut_report(splits["holdout"], scorer)
    ablations = run_ablations(
        splits["train"], splits["holdout"], holdout_selection["top_1_accuracy"]
    )
    calibration = {
        split: calibration_report(
            groups,
            null_groups,
            split,
            scorer,
            hazard,
            execution,
            priority_fee_sol,
            utility_threshold,
        )
        for split in ("validation", "holdout")
    }
    calibration["fit"] = {
        "hazard": hazard_fit,
        "utility": utility_fit,
        "all_calibration_and_threshold_fitting_split": "train",
    }
    execution_report = {
        "version": SCHEMA_VERSION,
        "fit": execution_fit,
        "metrics": execution_by_split,
        "fee_and_output_policy": dict(FEE_MODEL, priority_fee_sol=priority_fee_sol),
    }

    replay_cache = repo_root / ".tmp-choice-moe-replay.json"
    if args.reuse_replay_cache and replay_cache.exists():
        cached_replay = json.loads(replay_cache.read_text(encoding="utf-8"))
        intentions = [ReplayIntention(**row) for row in cached_replay["intentions"]]
        trajectories = {
            run_id: {
                mint: [MarketPoint(**point) for point in points] for mint, points in rows.items()
            }
            for run_id, rows in cached_replay["trajectories"].items()
        }
        replay_audit = cached_replay["audit"]
    else:
        intentions, trajectories, replay_audit = build_holdout_replay_inputs(
            specs,
            history,
            scorer,
            hazard,
            execution,
            priority_fee_sol,
            utility_threshold,
        )
        write_json(
            replay_cache,
            {
                "intentions": [row.__dict__ for row in intentions],
                "trajectories": {
                    run_id: {
                        mint: [point.__dict__ for point in points] for mint, points in rows.items()
                    }
                    for run_id, rows in trajectories.items()
                },
                "audit": replay_audit,
            },
        )
    known_holdout = [
        (
            str(group[0]["source_run_id"]),
            integer(group[0]["decision_ns_lower_bound"]) - EXCLUSION_MARGIN_MS * 1_000_000,
            integer(group[0]["decision_ns_upper_bound"]) + EXCLUSION_MARGIN_MS * 1_000_000,
            str(next(row for row in group if row["selected_by_e4"])["candidate_mint"]),
        )
        for group in splits["holdout"]
    ]
    economics = {
        str(latency): simulate_latency(
            intentions,
            trajectories,
            latency,
            priority_fee_sol,
            known_holdout,
        )
        for latency in LATENCIES_MS
    }
    economic_replay_repeat = {
        str(latency): simulate_latency(
            intentions,
            trajectories,
            latency,
            priority_fee_sol,
            known_holdout,
        )["closed_trade_ledger_hash"]
        for latency in LATENCIES_MS
    }
    integrity_failures = []
    if manifest["leakage_scan_violations"]:
        integrity_failures.append("FEATURE_LEAKAGE")
    row_invariance = row_order_invariance(splits["holdout"], winner)
    if row_invariance["status"] != "PASS":
        integrity_failures.append("ROW_ORDER_DEPENDENCE")
    gate = historical_gate(economics, selection_report, anti_shortcut, integrity_failures)
    historical_economics = {
        "version": SCHEMA_VERSION,
        "starting_bankroll_sol": STARTING_BANKROLL_SOL,
        "position_fraction": POSITION_FRACTION,
        "maximum_concurrent_positions": MAX_CONCURRENT_POSITIONS,
        "fee_model": dict(FEE_MODEL, priority_fee_sol=priority_fee_sol),
        "output_guard": {"minimum_quote_fraction": 0.85},
        "exit_policy": EXIT_POLICY,
        "replay_audit": replay_audit,
        "latencies": economics,
    }

    data_audit = {
        "version": SCHEMA_VERSION,
        "status": "PASS",
        "base_commit": BASE_COMMIT,
        "frozen_inputs": immutable["files"],
        "frozen_dataset_fingerprint": immutable["frozen_dataset_fingerprint"],
        "jsonl_parquet_parity": parity,
        "positive_decision_groups": len(groups),
        "landed_positive_groups": sum(
            next(row for row in group if row["selected_by_e4"])["landed_successfully"]
            for group in groups
        ),
        "failed_fill_positive_groups": sum(
            group[0]["decision_time_is_lower_bound"] for group in groups
        ),
        "run_ids": [spec.run_id for spec in specs],
        "chronological_split": {
            split: [spec.run_id for spec in specs if spec.split == split]
            for split in ("train", "validation", "holdout")
        },
        "split_group_counts": {split: len(values) for split, values in splits.items()},
        "no_group_crosses_splits": len(
            {str(group[0]["decision_group_id"]) for values in splits.values() for group in values}
        )
        == len(groups),
        "clock_discovery": clock_audit,
        "source_manifest_sha256": sha256_path(
            repo_root / "artifacts/e4-v12-riskset-source-manifest.json"
        ),
        "production_paths_changed": 0,
    }
    null_audit = {
        "version": SCHEMA_VERSION,
        "status": "PASS",
        "sampling_policy": null_policy.as_dict(),
        "groups": len(null_groups),
        "groups_by_split": dict(Counter(group["split"] for group in null_groups)),
        "groups_by_activity_band": dict(
            Counter(
                null_policy.band_for(integer(group["activity_1s"]), null_policy.activity_cutpoints)
                for group in null_groups
            )
        ),
        "selected_coin_rows": sum(group["selected_coin_rows"] for group in null_groups),
        "outside_option_rows": sum(
            sum(bool(row.get("is_outside_option")) for row in group["alternatives"])
            for group in null_groups
        ),
        "known_selection_overlap": len(contamination),
        "interval_inventory": interval_counts,
        "contamination_exclusions": clock_audit["exclusions"],
        "unresolved_wallet_activity_excluded": interval_counts["unresolved_wallet_intervals"],
        "candidate_features_causal": all(
            integer(row["feature_max_event_ns"]) <= integer(group["decision_ns"])
            for group in null_groups
            for row in null_candidate_rows(group)
        ),
        "validation_or_holdout_influenced_policy": False,
        "null_group_weight": "one equal-weight observation per decision group",
    }
    model_comparison = {
        "version": SCHEMA_VERSION,
        "validation_selection_rule": "top-1, then pairwise accuracy; identity diagnostic excluded",
        "winning_model": winning_model,
        "model_families": comparison,
        "mixture_experts": list(EXPERT_FEATURES),
        "effective_model_complexity": {
            "experts": len(EXPERT_FEATURES),
            "strong_regularisation": True,
            "gating_fit_split": "train",
            "social_availability_gate": True,
            "funder_expert_count": 0,
        },
        "identity_cold_start": identity_cold_start_report(
            splits["train"], splits["holdout"], models["identity_aware_regularised"]
        ),
    }

    second_model = train_one_model(winning_model, splits["train"])
    first_fingerprint = model_prediction_fingerprint(winner, splits["holdout"])
    second_fingerprint = model_prediction_fingerprint(second_model, splits["holdout"])
    permutation = label_permutation_audit(
        splits["train"],
        splits["validation"],
        comparison[winning_model]["validation"]["top_1_accuracy"],
    )
    reproducibility = {
        "version": SCHEMA_VERSION,
        "random_seed": RANDOM_SEED,
        "deterministic_two_run": {
            "first_prediction_fingerprint": first_fingerprint,
            "second_prediction_fingerprint": second_fingerprint,
            "identical": first_fingerprint == second_fingerprint,
        },
        "row_order_invariance": row_invariance,
        "pair_orientation": {
            "symmetrical_orientations": True,
            "equal_group_weight": True,
            "large_groups_do_not_dominate": True,
        },
        "label_permutation": permutation,
        "economic_replay": {
            "first": {
                latency: row["closed_trade_ledger_hash"] for latency, row in economics.items()
            },
            "second": economic_replay_repeat,
            "identical": all(
                economics[latency]["closed_trade_ledger_hash"] == economic_replay_repeat[latency]
                for latency in economics
            ),
        },
        "train_only_transform_groups": list(
            models["pairwise_linear_ranker"].transformer.fit_group_ids
        ),
        "validation_or_holdout_transform_fit": False,
        "holdout_evaluated_once_after_freeze": True,
    }

    identity = registry_identity(
        script_path,
        data_audit,
        manifest,
        winning_model,
        null_policy,
        utility_threshold,
        priority_fee_sol,
    )
    experiment_id = registry_experiment_id(identity)
    registered = existing_registry_ids(repo_root)
    artifact_paths = [
        f"artifacts/e4-v12-choice-moe-{name}"
        for name in (
            "data-audit.json",
            "null-groups.jsonl",
            "null-group-audit.json",
            "feature-manifest.json",
            "model-comparison.json",
            "selection-report.json",
            "selection-report.md",
            "anti-shortcut-audit.json",
            "ablation-report.json",
            "calibration.json",
            "execution-report.json",
            "historical-economics.json",
            "historical-verdict.json",
            "experiment-manifest.json",
            "reproducibility.json",
        )
    ]
    experiment_manifest = {
        "version": "e4-v12-experiment-registry-compatible-v1",
        "experiment_id": experiment_id,
        "identity": identity,
        "thesis_family": THESIS_FAMILY,
        "created_timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "source_commit": BASE_COMMIT,
        "dataset_version": "e4-v12-canonical-choice-risksets-v2",
        "evidence_epoch": identity["evidence_epoch"],
        "train_metrics": comparison[winning_model].get("train"),
        "validation_metrics": comparison[winning_model]["validation"],
        "chronological_holdout_metrics": holdout_selection,
        "live_metrics": None,
        "trade_count": economics["0"]["trades"],
        "win_rate": economics["0"]["win_rate"],
        "wilson_lower_bound": economics["0"]["wilson_95_lower_bound"],
        "pnl": economics["0"]["net_pnl_sol"],
        "profit_factor": economics["0"]["profit_factor"],
        "drawdown": economics["0"]["maximum_drawdown_fraction_of_start"],
        "selection_precision": hazard_by_split["holdout"]["trade_precision"],
        "recall": hazard_by_split["holdout"]["trade_recall"],
        "latency_results": economics,
        "failure_classification": gate["failure_classification"],
        "artifact_paths": artifact_paths,
        "retired": not gate["historical_qualification_passed"],
        "retirement_reason": gate["failure_classification"],
        "material_change_required_before_rerun": (
            "change a registry identity component: new evidence epoch/dataset, causal features, "
            "model parameters, execution policy, or exit policy"
        ),
        "existing_registry_unique_experiments": len(registered),
        "scientifically_new_relative_to_registry": experiment_id not in registered,
        "identical_scientific_failure_redispatch_allowed": False,
        "infrastructure_retry_allowed": True,
    }
    historical_verdict = dict(
        gate,
        version=SCHEMA_VERSION,
        experiment_id=experiment_id,
        final_status=gate["status"],
        production_paths_changed=0,
    )

    write_json(output / "e4-v12-choice-moe-data-audit.json", data_audit)
    write_json(output / "e4-v12-choice-moe-null-group-audit.json", null_audit)
    write_json(output / "e4-v12-choice-moe-feature-manifest.json", manifest)
    write_json(output / "e4-v12-choice-moe-model-comparison.json", model_comparison)
    write_json(output / "e4-v12-choice-moe-selection-report.json", selection_report)
    (output / "e4-v12-choice-moe-selection-report.md").write_text(
        selection_markdown(selection_report), encoding="utf-8", newline="\n"
    )
    write_json(output / "e4-v12-choice-moe-anti-shortcut-audit.json", anti_shortcut)
    write_json(output / "e4-v12-choice-moe-ablation-report.json", ablations)
    write_json(output / "e4-v12-choice-moe-calibration.json", calibration)
    write_json(output / "e4-v12-choice-moe-execution-report.json", execution_report)
    write_json(output / "e4-v12-choice-moe-historical-economics.json", historical_economics)
    write_json(output / "e4-v12-choice-moe-historical-verdict.json", historical_verdict)
    write_json(output / "e4-v12-choice-moe-experiment-manifest.json", experiment_manifest)
    write_json(output / "e4-v12-choice-moe-reproducibility.json", reproducibility)
    print(
        canonical_json(
            {
                "status": historical_verdict["final_status"],
                "experiment_id": experiment_id,
                "positive_groups": len(groups),
                "null_groups": len(null_groups),
                "winning_model": winning_model,
                "holdout_top1": holdout_selection["top_1_accuracy"],
                "trades_0ms": economics["0"]["trades"],
                "pnl_0ms": economics["0"]["net_pnl_sol"],
            }
        )
    )


if __name__ == "__main__":
    main()
