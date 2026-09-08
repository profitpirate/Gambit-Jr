#!/usr/bin/env python3
"""Evolve transparent OR-of-AND formulae over launch-time causal features.

This is deliberately a separate scientific lane from the black-box models.  It
searches small disjunctive rule sets because E4 may be a union of several simple
entry modes rather than one universal score.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from scripts import e4_v12_allout_profit_hazard as base
from scripts.e4_v12_allout_profit_hazard_stream import load_corpus_stream

VERSION = "e4-v12-symbolic-dnf-v1"


@dataclass(frozen=True)
class Predicate:
    name: str
    operator: str
    threshold: float
    mask: np.ndarray

    def text(self) -> str:
        return f"{self.name} {self.operator} {self.threshold:.12g}"


@dataclass(frozen=True)
class Individual:
    policy: str
    clauses: tuple[tuple[int, ...], ...]

    def canonical(self) -> "Individual":
        clauses = tuple(
            sorted(
                {
                    tuple(sorted(set(clause)))
                    for clause in self.clauses
                    if clause
                }
            )
        )
        return Individual(self.policy, clauses)

    @property
    def complexity(self) -> int:
        return sum(len(clause) for clause in self.clauses) + max(0, len(self.clauses) - 1)


def safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [safe(item) for item in value]
    if isinstance(value, np.generic):
        return safe(value.item())
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(safe(payload), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def predicates(corpus: base.Corpus) -> list[Predicate]:
    train = corpus.splits == "train"
    output: list[Predicate] = []
    for column, name in enumerate(corpus.feature_names_general):
        values = corpus.x_general[:, column]
        train_values = values[train]
        if not len(train_values) or np.nanstd(train_values) <= 1e-12:
            continue
        quantiles = sorted(
            {
                float(np.quantile(train_values, quantile))
                for quantile in (
                    0.01,
                    0.025,
                    0.05,
                    0.10,
                    0.20,
                    0.30,
                    0.40,
                    0.50,
                    0.60,
                    0.70,
                    0.80,
                    0.90,
                    0.95,
                    0.975,
                    0.99,
                )
            }
        )
        for threshold in quantiles:
            for operator, mask in (
                (">=", values >= threshold),
                ("<=", values <= threshold),
            ):
                support = int((train & mask).sum())
                if 5 <= support <= int(train.sum()) - 5:
                    output.append(Predicate(name, operator, threshold, mask))
    return output


def formula_mask(individual: Individual, pool: Sequence[Predicate]) -> np.ndarray:
    if not individual.clauses:
        return np.zeros(len(pool[0].mask), dtype=bool)
    result = np.zeros(len(pool[0].mask), dtype=bool)
    for clause in individual.clauses:
        current = np.ones(len(pool[0].mask), dtype=bool)
        for index in clause:
            current &= pool[index].mask
        result |= current
    return result


def raw_metrics(indices: np.ndarray, pnl: np.ndarray) -> dict[str, float]:
    if not len(indices):
        return {
            "trades": 0,
            "minimum_win_rate": 0.0,
            "minimum_profit_factor": 0.0,
            "minimum_pnl": -999.0,
            "minimum_expectancy": -999.0,
        }
    blocks = []
    for column in range(pnl.shape[1]):
        values = pnl[indices, column]
        wins = int((values > 0).sum())
        gains = float(values[values > 0].sum())
        losses = float(-values[values < 0].sum())
        blocks.append(
            {
                "wr": wins / len(values),
                "pf": gains / losses if losses > 0 else (999.0 if gains > 0 else 0.0),
                "pnl": float(values.sum()),
                "expectancy": float(values.mean()),
            }
        )
    return {
        "trades": int(len(indices)),
        "minimum_win_rate": min(row["wr"] for row in blocks),
        "minimum_profit_factor": min(row["pf"] for row in blocks),
        "minimum_pnl": min(row["pnl"] for row in blocks),
        "minimum_expectancy": min(row["expectancy"] for row in blocks),
    }


def fitness(
    individual: Individual,
    pool: Sequence[Predicate],
    corpus: base.Corpus,
    train: np.ndarray,
) -> tuple[float, dict[str, Any]]:
    mask = formula_mask(individual, pool) & train
    indices = np.flatnonzero(mask)
    metrics = raw_metrics(indices, corpus.pnl[individual.policy])
    trades = int(metrics["trades"])
    runs = len(set(corpus.run_ids[indices])) if trades else 0
    if trades < 12 or trades > 2_000 or runs < 3:
        return -1e9 - abs(20 - trades), {**metrics, "capture_windows": runs}
    wr = metrics["minimum_win_rate"]
    pf = min(20.0, metrics["minimum_profit_factor"])
    pnl = metrics["minimum_pnl"]
    expectancy = metrics["minimum_expectancy"]
    complexity_penalty = individual.complexity * 0.015
    excess_penalty = max(0, trades - 500) / 2_000
    score = (
        wr * 6.0
        + math.log1p(max(0.0, pf))
        + max(-2.0, min(2.0, pnl * 4.0))
        + max(-1.0, min(1.0, expectancy * 100.0))
        + min(0.5, runs / 20.0)
        - complexity_penalty
        - excess_penalty
    )
    if wr < 0.55:
        score -= (0.55 - wr) * 10.0
    if pnl <= 0:
        score -= 2.0
    return score, {**metrics, "capture_windows": runs}


def random_individual(rng: random.Random, policies: Sequence[str], predicate_count: int) -> Individual:
    clause_count = rng.randint(1, 3)
    clauses = []
    for _ in range(clause_count):
        size = rng.randint(1, 3)
        clauses.append(tuple(rng.sample(range(predicate_count), size)))
    return Individual(rng.choice(list(policies)), tuple(clauses)).canonical()


def mutate(
    individual: Individual,
    rng: random.Random,
    policies: Sequence[str],
    predicate_count: int,
) -> Individual:
    clauses = [list(clause) for clause in individual.clauses]
    policy = individual.policy
    move = rng.randrange(7)
    if move == 0:
        policy = rng.choice(list(policies))
    elif move == 1 and len(clauses) < 4:
        clauses.append([rng.randrange(predicate_count)])
    elif move == 2 and len(clauses) > 1:
        clauses.pop(rng.randrange(len(clauses)))
    elif move == 3 and clauses:
        clause = clauses[rng.randrange(len(clauses))]
        if len(clause) < 4:
            clause.append(rng.randrange(predicate_count))
    elif move == 4 and clauses:
        clause = clauses[rng.randrange(len(clauses))]
        if len(clause) > 1:
            clause.pop(rng.randrange(len(clause)))
        else:
            clause[0] = rng.randrange(predicate_count)
    elif move == 5 and clauses:
        clause = clauses[rng.randrange(len(clauses))]
        clause[rng.randrange(len(clause))] = rng.randrange(predicate_count)
    else:
        return random_individual(rng, policies, predicate_count)
    return Individual(policy, tuple(tuple(clause) for clause in clauses)).canonical()


def crossover(left: Individual, right: Individual, rng: random.Random) -> Individual:
    policy = left.policy if rng.random() < 0.5 else right.policy
    source = list(left.clauses) + list(right.clauses)
    rng.shuffle(source)
    count = rng.randint(1, min(4, len(source)))
    return Individual(policy, tuple(source[:count])).canonical()


def formula_text(individual: Individual, pool: Sequence[Predicate]) -> str:
    clauses = []
    for clause in individual.clauses:
        clauses.append("(" + " AND ".join(pool[index].text() for index in clause) + ")")
    return " OR ".join(clauses)


def evolve(
    corpus: base.Corpus,
    seed: int,
    population_size: int,
    generations: int,
) -> dict[str, Any]:
    rng = random.Random(seed)
    train = corpus.splits == "train"
    validation = corpus.splits == "validation"
    holdout = corpus.splits == "holdout"
    pool = predicates(corpus)
    if len(pool) < 20:
        raise ValueError("too few nonconstant causal predicates")

    population = [
        random_individual(rng, corpus.policies, len(pool))
        for _ in range(population_size)
    ]
    archive: dict[Individual, tuple[float, dict[str, Any]]] = {}
    generation_log = []

    for generation in range(generations):
        scored = []
        for individual in population:
            if individual not in archive:
                archive[individual] = fitness(individual, pool, corpus, train)
            score, metrics = archive[individual]
            scored.append((score, individual, metrics))
        scored.sort(reverse=True, key=lambda item: item[0])
        elite = scored[: max(12, population_size // 8)]
        generation_log.append(
            {
                "generation": generation,
                "best_fitness": elite[0][0],
                "best_formula": formula_text(elite[0][1], pool),
                "best_policy": elite[0][1].policy,
                "best_train": elite[0][2],
                "unique_formulae": len(archive),
            }
        )
        next_population = [item[1] for item in elite]
        while len(next_population) < population_size:
            left = rng.choice(elite)[1]
            if rng.random() < 0.35:
                right = rng.choice(elite)[1]
                child = crossover(left, right, rng)
            else:
                child = left
            if rng.random() < 0.90:
                child = mutate(child, rng, corpus.policies, len(pool))
            next_population.append(child)
        population = next_population

    train_ranked = sorted(
        ((score, individual, metrics) for individual, (score, metrics) in archive.items()),
        reverse=True,
        key=lambda item: item[0],
    )
    validation_rows = []
    for score, individual, train_metrics in train_ranked[:1_000]:
        mask = formula_mask(individual, pool)
        indices = np.flatnonzero(validation & mask)
        if len(indices) < 8:
            continue
        exact = base.multi_latency_economics(
            indices, mask.astype(float), corpus, individual.policy
        )
        validation_rows.append(
            {
                "individual": individual,
                "formula": formula_text(individual, pool),
                "train_fitness": score,
                "train": train_metrics,
                "validation": exact,
                "eligible": base.validation_eligible(exact),
            }
        )
    if not validation_rows:
        return {
            "version": VERSION,
            "seed": seed,
            "status": "NOT_CONCLUSIVE",
            "reason": "no evolved formula produced eight validation trades",
            "predicates": len(pool),
            "unique_formulae": len(archive),
            "generation_log": generation_log,
        }
    winner = max(
        validation_rows,
        key=lambda row: (
            1 if row["eligible"] else 0,
            *base.objective(row["validation"]),
            -row["individual"].complexity,
        ),
    )
    winner_mask = formula_mask(winner["individual"], pool)
    holdout_indices = np.flatnonzero(holdout & winner_mask)
    holdout_metrics = base.multi_latency_economics(
        holdout_indices,
        winner_mask.astype(float),
        corpus,
        winner["individual"].policy,
    )
    passed, failures = base.historical_pass(holdout_metrics)
    top = []
    for row in sorted(
        validation_rows,
        key=lambda value: (
            1 if value["eligible"] else 0,
            *base.objective(value["validation"]),
        ),
        reverse=True,
    )[:100]:
        top.append(
            {
                "formula": row["formula"],
                "policy": row["individual"].policy,
                "complexity": row["individual"].complexity,
                "train_fitness": row["train_fitness"],
                "train": row["train"],
                "validation": {
                    key: value
                    for key, value in row["validation"].items()
                    if key != "latencies"
                },
                "eligible": row["eligible"],
            }
        )
    return {
        "version": VERSION,
        "seed": seed,
        "status": "HISTORICAL_CANDIDATE" if passed else "NOT_CONCLUSIVE",
        "predicates": len(pool),
        "unique_formulae": len(archive),
        "population_size": population_size,
        "generations": generations,
        "winner": {
            "formula": winner["formula"],
            "policy": winner["individual"].policy,
            "complexity": winner["individual"].complexity,
            "train": winner["train"],
            "validation": winner["validation"],
            "holdout": holdout_metrics,
            "historical_pass": passed,
            "failed_requirements": failures,
        },
        "top_validation_formulae": top,
        "generation_log": generation_log,
        "production_paths_changed": 0,
        "live_trading": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--population", type=int, default=256)
    parser.add_argument("--generations", type=int, default=160)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    corpus = load_corpus_stream(args.corpus, 0)
    report = evolve(corpus, args.seed, args.population, args.generations)
    corpus_hash = hashlib.sha256(args.corpus.read_bytes()).hexdigest()
    report["corpus_sha256"] = corpus_hash
    report["experiment_id"] = "e4x-symbolic-" + hashlib.sha256(
        json.dumps(
            {
                "version": VERSION,
                "seed": args.seed,
                "population": args.population,
                "generations": args.generations,
                "corpus": corpus_hash,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()
    write_json(args.output, report)
    print(
        json.dumps(
            {
                "status": report.get("status"),
                "seed": args.seed,
                "experiment_id": report["experiment_id"],
                "winner": {
                    key: value
                    for key, value in (report.get("winner") or {}).items()
                    if key in {"formula", "policy", "historical_pass", "failed_requirements"}
                },
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
