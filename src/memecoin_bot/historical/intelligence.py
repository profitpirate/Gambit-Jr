from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from typing import Any


def _at(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("point-in-time intelligence requires timezone-aware timestamps")
    return parsed.astimezone(UTC)


def point_in_time_rows(rows: list[dict[str, Any]], decision_at: str) -> list[dict[str, Any]]:
    decision = _at(decision_at)
    return [
        row
        for row in rows
        if row.get("available_at") is not None and _at(row["available_at"]) <= decision
    ]


def empirical_wallet_reputation(
    outcomes: list[dict[str, Any]], decision_at: str, *, minimum_proven_sample: int = 20
) -> dict[str, Any]:
    known = [
        row
        for row in point_in_time_rows(outcomes, decision_at)
        if row.get("matured") is True and row.get("peak_multiple") is not None
    ]
    if not known:
        return {
            "state": "UNKNOWN",
            "score": None,
            "sample": 0,
            "grade": "UNKNOWN",
            "as_of": decision_at,
        }
    wins = sum(float(row["peak_multiple"]) >= 5 for row in known)
    rugs = sum(bool(row.get("rugged")) for row in known)
    early = sum(float(row.get("entry_age_minutes") or 10_000) <= 10 for row in known)
    # Beta priors shrink small samples toward neutral rather than producing fake certainty.
    runner_rate = (wins + 1) / (len(known) + 2)
    rug_rate = (rugs + 1) / (len(known) + 2)
    early_rate = (early + 1) / (len(known) + 2)
    score = max(0.0, min(100.0, 65 * runner_rate + 20 * early_rate - 45 * rug_rate + 20))
    grade = (
        "PROVEN"
        if len(known) >= minimum_proven_sample and score >= 70
        else "PROMISING"
        if score >= 55
        else "UNPROVEN"
    )
    return {
        "state": "KNOWN",
        "score": round(score, 2),
        "sample": len(known),
        "grade": grade,
        "runner_rate": round(runner_rate, 4),
        "rug_rate": round(rug_rate, 4),
        "as_of": decision_at,
    }


def creator_reputation(outcomes: list[dict[str, Any]], decision_at: str) -> dict[str, Any]:
    known = [row for row in point_in_time_rows(outcomes, decision_at) if row.get("matured")]
    if not known:
        return {"state": "UNKNOWN", "score": None, "sample": 0, "as_of": decision_at}
    survived = sum(float(row.get("survival_hours") or 0) >= 24 for row in known)
    runners = sum(float(row.get("peak_multiple") or 0) >= 5 for row in known)
    failed = sum(
        bool(row.get("rugged")) or float(row.get("peak_multiple") or 0) < 1 for row in known
    )
    score = max(
        0.0,
        min(
            100.0,
            45 * survived / len(known) + 40 * runners / len(known) - 55 * failed / len(known) + 30,
        ),
    )
    return {
        "state": "KNOWN",
        "score": round(score, 2),
        "sample": len(known),
        "runner_count": runners,
        "failure_count": failed,
        "as_of": decision_at,
    }


def buyer_quality(cohort: dict[str, Any] | None) -> dict[str, Any]:
    if not cohort:
        return {"state": "UNKNOWN", "score": None}
    size = int(cohort.get("cohort_size") or 0)
    if size <= 0:
        return {"state": "UNKNOWN", "score": None}
    independent = int(cohort.get("independent_buyers") or 0) / size
    retained = int(cohort.get("retained_buyers") or 0) / size
    alpha = int(cohort.get("independent_alpha_families") or 0)
    sybil = float(cohort.get("connected_actor_percent") or 0) / 100
    score = max(
        0.0, min(100.0, 35 * independent + 30 * retained + min(25, alpha * 5) - 35 * sybil + 10)
    )
    state = "HIGH" if score >= 70 else "MEDIUM" if score >= 45 else "LOW"
    return {"state": state, "score": round(score, 2), "sample": size}


def funding_relationship(evidence: dict[str, Any]) -> dict[str, Any]:
    """Use relationship language only; never infer ownership from graph proximity."""
    if evidence.get("direct_transfer"):
        relationship = "DIRECT_TRANSFER_OBSERVED"
    elif evidence.get("common_funder"):
        relationship = "COMMON_FUNDER"
    elif evidence.get("repeated_deployment_pattern"):
        relationship = "REPEATED_DEPLOYMENT_PATTERN"
    else:
        relationship = "UNKNOWN" if not evidence else "ASSOCIATED"
    return {
        "relationship": relationship,
        "same_owner": "UNKNOWN",
        "confidence": evidence.get("confidence"),
    }


def fingerprint_similarity(
    live: dict[str, Any], fingerprint: dict[str, dict[str, float]], weights: dict[str, float]
) -> dict[str, Any]:
    distances = []
    total_weight = 0.0
    used = []
    for name, weight in weights.items():
        value = live.get(name)
        profile = fingerprint.get(name) or {}
        median = profile.get("median")
        scale = profile.get("scale")
        if not isinstance(value, (int, float)) or median is None or not scale:
            continue
        bounded_distance = min(1.0, abs(float(value) - median) / abs(scale))
        distances.append(bounded_distance * weight)
        total_weight += weight
        used.append(name)
    if not total_weight:
        return {"state": "UNKNOWN", "score": None, "coverage": 0, "features": []}
    distance = sum(distances) / total_weight
    configured_weight = sum(max(0.0, value) for value in weights.values())
    coverage = total_weight / configured_weight * 100 if configured_weight else 0
    return {
        "state": "KNOWN",
        "score": round((1 - distance) * 100, 2),
        "coverage": round(coverage, 2),
        "features": used,
    }


def actor_clusters(edges: list[dict[str, Any]], decision_at: str) -> list[dict[str, Any]]:
    known = point_in_time_rows(edges, decision_at)
    graph: dict[str, set[str]] = defaultdict(set)
    for edge in known:
        left, right = str(edge["source"]), str(edge["target"])
        graph[left].add(right)
        graph[right].add(left)
    clusters = []
    visited: set[str] = set()
    for node in graph:
        if node in visited:
            continue
        stack = [node]
        members = []
        while stack:
            current = stack.pop()
            if current in visited:
                continue
            visited.add(current)
            members.append(current)
            stack.extend(graph[current] - visited)
        clusters.append(
            {
                "cluster_id": min(members),
                "members": sorted(members),
                "member_count": len(members),
                "relationship": "CONNECTED_EVIDENCE_NOT_OWNERSHIP",
            }
        )
    return sorted(clusters, key=lambda row: (-row["member_count"], row["cluster_id"]))


def hierarchical_prior(
    long_term: float | None,
    recent_regime: float | None,
    live: float | None,
    *,
    long_weight: float = 0.2,
    recent_weight: float = 0.3,
) -> dict[str, Any]:
    inputs = {
        "long_term": (long_term, long_weight),
        "recent_regime": (recent_regime, recent_weight),
        "live": (live, max(0.0, 1 - long_weight - recent_weight)),
    }
    known = [
        (name, float(value), weight)
        for name, (value, weight) in inputs.items()
        if value is not None
    ]
    if not known:
        return {"state": "UNKNOWN", "value": None, "components": {}}
    weight_sum = sum(weight for _, _, weight in known)
    value = sum(value * weight for _, value, weight in known) / weight_sum
    return {
        "state": "KNOWN",
        "value": round(value, 4),
        "components": {name: component for name, component, _weight in known},
    }
