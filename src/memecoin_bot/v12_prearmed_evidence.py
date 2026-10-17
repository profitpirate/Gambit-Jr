"""Recognize the exact manually accepted research sample, without live arming."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

FROZEN_MODEL_SHA256 = '69df86eaf386fd37d699928a95d25aaeb2053e743b59c9e687c8a7c49d14f977'
APPROVED_STATE_SHA256 = '6bfbc8fbd55c392dc1fbd91d6cb6274a0acf3c658365858a1ccb3961a252fd55'
APPROVAL_COMMIT = '850acb61e56087cc8cb3b1deebb2e7032712bcd4'


def canonical_hash(value: Any) -> str:
    data = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True, slots=True)
class ResearchAcceptance:
    accepted: bool
    reason: str
    automatic_gate_passed: bool
    production_authorised: bool = False


def evaluate_research_acceptance(model: Mapping[str, Any], state: Mapping[str, Any]) -> ResearchAcceptance:
    """Only this recorded override is recognized; no environment flag can widen it.

    A successful result means research acceptance, not trading authorization.
    Original metrics and the state are never mutated. The full canonical state
    digest covers the ledger, completion, provenance, and exact waived criteria.
    """
    try:
        if canonical_hash(model) != FROZEN_MODEL_SHA256:
            return ResearchAcceptance(False, 'FROZEN_MODEL_MISMATCH', False)
        if canonical_hash(state) != APPROVED_STATE_SHA256:
            return ResearchAcceptance(False, 'APPROVED_SAMPLE_MISMATCH', False)
    except (TypeError, ValueError, OverflowError):
        return ResearchAcceptance(False, 'INVALID_EVIDENCE', False)
    return ResearchAcceptance(True, 'RECORDED_MANUAL_RESEARCH_PASS', False)


def read_evidence(path: Path) -> dict[str, Any]:
    def reject_constant(_value: str) -> None:
        raise ValueError('Non-finite JSON constant in evidence')
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate JSON key in evidence')
            result[key] = value
        return result
    payload = json.loads(path.read_text(encoding='utf-8'), parse_constant=reject_constant, object_pairs_hook=unique_object)
    if not isinstance(payload, dict):
        raise ValueError('Evidence must be a JSON object')
    return payload
