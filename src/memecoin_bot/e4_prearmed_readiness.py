from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any, Mapping

FROZEN_MODEL_SHA256 = "69df86eaf386fd37d699928a95d25aaeb2053e743b59c9e687c8a7c49d14f977"


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def resolve_path(env_name: str, default: str) -> Path:
    raw = os.getenv(env_name, default).strip()
    value = Path(raw)
    return value if value.is_absolute() else repo_root() / value


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists() or path.stat().st_size <= 0:
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError(f"expected JSON object: {path}")
    return dict(payload)


def canonical_json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def stable_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def validate_keypair_file(path: Path) -> list[str]:
    issues: list[str] = []
    if not path.exists():
        return ["KEYPAIR_FILE_MISSING"]
    if path.is_symlink():
        issues.append("KEYPAIR_FILE_IS_SYMLINK")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        issues.append("KEYPAIR_NOT_REGULAR_FILE")
    if info.st_size <= 0 or info.st_size > 4096:
        issues.append("KEYPAIR_FILE_SIZE_INVALID")
    if info.st_mode & 0o077:
        issues.append("KEYPAIR_PERMISSIONS_TOO_OPEN")
    return issues


def secret_environment_issues(environment: Mapping[str, str] | None = None) -> list[str]:
    values = environment if environment is not None else os.environ
    forbidden = (
        "E4_PRIVATE_KEY",
        "E4_SECRET_KEY",
        "E4_SEED_PHRASE",
        "SOLANA_PRIVATE_KEY",
        "PHANTOM_SEED_PHRASE",
    )
    return [f"FORBIDDEN_SECRET_ENV:{name}" for name in forbidden if values.get(name)]


def readiness(*, include_external: bool = False) -> dict[str, Any]:
    model_path = resolve_path("E4_PREARMED_MODEL_PATH", "research/v12-pre-armed-100-trade-frozen-model.json")
    certification_path = resolve_path("E4_PREARMED_CERTIFICATION_PATH", "research/v12-pre-armed-certification-status.json")
    causal_path = resolve_path("E4_PREARMED_CAUSAL_STATE_PATH", "research/v12-pre-armed-100-causal-paper-live.json")
    shadow_path = resolve_path("E4_PREARMED_SHADOW_PATH", "research/v12-pre-armed-100-causal-shadow-analytics.json")

    blockers: list[str] = []
    warnings: list[str] = []
    model = read_json(model_path)
    model_hash = stable_hash(model) if model else ""
    expected_hash = os.getenv("E4_PREARMED_MODEL_SHA256", FROZEN_MODEL_SHA256).strip().lower()
    if model_hash != expected_hash:
        blockers.append("FROZEN_MODEL_HASH_MISMATCH")
    selector = model.get("selector") or {}
    if selector.get("family") != "prearmed_repeat_creator_social_status":
        blockers.append("FROZEN_SELECTOR_FAMILY_MISMATCH")
    if model.get("production_paths_changed") not in (0, None):
        blockers.append("FROZEN_MODEL_PRODUCTION_PATHS_CHANGED")

    certification = read_json(certification_path)
    if not certification.get("official_50_trade_baseline_valid"):
        blockers.append("OFFICIAL_50_TRADE_BASELINE_INVALID")
    if not certification.get("pre_fix_100_trade_sample_invalidated"):
        blockers.append("INVALIDATED_19_TRADE_SAMPLE_NOT_MARKED_INVALID")
    if certification.get("pre_fix_100_trade_sample_imported"):
        blockers.append("INVALIDATED_19_TRADE_SAMPLE_IMPORTED")

    causal = read_json(causal_path)
    if causal.get("model_sha256") and causal.get("model_sha256") != model_hash:
        blockers.append("CAUSAL_STATE_MODEL_HASH_MISMATCH")
    completion = causal.get("completion") or {}
    metrics = causal.get("metrics") or {}
    if not completion.get("reached"):
        blockers.append("FRESH_100_TRADE_CONFIRMATION_INCOMPLETE")
    if completion.get("reached") and not metrics.get("acceptance_gate_passed"):
        blockers.append("FRESH_100_TRADE_ACCEPTANCE_GATE_FAILED")
    if completion.get("reached") and not causal.get("golden_thesis_approved"):
        blockers.append("FRESH_100_TRADE_NOT_APPROVED")

    shadow = read_json(shadow_path)
    guardrails = shadow.get("guardrails") or {}
    if not guardrails:
        blockers.append("SHADOW_GUARDRAIL_STATE_MISSING")
    else:
        guard_state = str(guardrails.get("state") or "UNKNOWN").upper()
        if guard_state == "HALT":
            blockers.append("SHADOW_GUARDRAIL_HALTED")
        elif guard_state not in {"CLEAR", "WATCH"}:
            blockers.append("SHADOW_GUARDRAIL_UNKNOWN")
        warnings.extend(str(item) for item in guardrails.get("warnings") or [])

    blockers.extend(secret_environment_issues())

    external: dict[str, Any] = {}
    if include_external:
        keypair_raw = os.getenv("E4_KEYPAIR_PATH", "").strip()
        external["keypair_configured"] = bool(keypair_raw)
        external["wallet_configured"] = bool(os.getenv("E4_WALLET_PUBLIC_KEY", "").strip())
        external["rpc_configured"] = bool(os.getenv("E4_PRIMARY_RPC_URL", "").strip())
        external["route_configured"] = bool(os.getenv("E4_ROUTE_URLS_JSON", "").strip())
        if keypair_raw:
            external["keypair_issues"] = validate_keypair_file(Path(keypair_raw))

    return {
        "ready_without_external_credentials": not blockers,
        "blockers": blockers,
        "warnings": warnings,
        "model_sha256": model_hash,
        "expected_model_sha256": expected_hash,
        "completion": completion,
        "metrics": metrics,
        "guardrails": guardrails,
        "external": external,
    }
