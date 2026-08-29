from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class EvaluationUniverse:
    window_start: str
    window_end: str
    decision_horizon_seconds: int
    quality_filter_version: str
    outcome_maturity_seconds: int
    frequency: str
    target_definition: str
    copyability_definition: str
    token_universe_version: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "window_start": self.window_start,
            "window_end": self.window_end,
            "decision_horizon_seconds": self.decision_horizon_seconds,
            "quality_filter_version": self.quality_filter_version,
            "outcome_maturity_seconds": self.outcome_maturity_seconds,
            "frequency": self.frequency,
            "target_definition": self.target_definition,
            "copyability_definition": self.copyability_definition,
            "token_universe_version": self.token_universe_version,
        }


def evaluation_universe_hash(
    universe: EvaluationUniverse,
    rows: Iterable[Mapping[str, Any]],
) -> str:
    """Hash the exact ordered entities and immutable evaluation contract."""
    members = sorted(
        {
            (
                str(row["entity_key"]),
                str(row["decision_at"]),
                str(row.get("outcome_available_at") or ""),
            )
            for row in rows
        }
    )
    payload = {"contract": universe.to_dict(), "members": members}
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def require_same_universe(*experiments: Mapping[str, Any]) -> str:
    hashes = {str(row.get("evaluation_universe_hash") or "") for row in experiments}
    if "" in hashes:
        raise ValueError("every experiment must declare an evaluation_universe_hash")
    if len(hashes) != 1:
        raise ValueError("model comparison refused: evaluation universe hashes differ")
    return hashes.pop()


def same_universe_delta(
    candidate: Mapping[str, Any],
    control: Mapping[str, Any],
    metric: str,
) -> dict[str, Any]:
    universe_hash = require_same_universe(candidate, control)
    candidate_value = float(candidate[metric])
    control_value = float(control[metric])
    return {
        "metric": metric,
        "candidate": candidate_value,
        "control": control_value,
        "absolute_delta": candidate_value - control_value,
        "evaluation_universe_hash": universe_hash,
        "comparison": "SAME_UNIVERSE_DELTA",
    }
