"""Fail-closed live-trading readiness gate for V12."""
from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse

from .v12_backup import decode_aes_key
from .v12_security import (
    IntegrityVerifier,
    SingleInstanceLock,
    decode_secret_key,
    ensure_private_file,
    validate_live_arm_token,
)


@dataclass(frozen=True, slots=True)
class ReadinessCheck:
    name: str
    passed: bool
    detail: str


class LiveReadinessError(RuntimeError):
    def __init__(self, checks: list[ReadinessCheck]):
        self.checks = checks
        failed = [item for item in checks if not item.passed]
        super().__init__(
            "V12 live readiness failed: "
            + "; ".join(f"{item.name}={item.detail}" for item in failed)
        )


def _truthy(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


EXPECTED_FROZEN_MODEL_SHA256 = "69df86eaf386fd37d699928a95d25aaeb2053e743b59c9e687c8a7c49d14f977"
EXPECTED_BASELINE = {
    "closed_trades": 50,
    "wins": 35,
    "losses": 15,
    "win_rate": 0.7,
    "net_pnl_sol": 2.086015102090403,
    "profit_factor": 6.00625698411455,
    "maximum_closed_equity_drawdown_fraction": 0.029421494624602456,
}


def _resolve(root: Path, value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else root / path


def _json_sha256(path: Path) -> str:
    payload = json.loads(path.read_text(encoding="utf-8"))
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _same_number(actual: object, expected: float | int) -> bool:
    try:
        return math.isclose(float(actual), float(expected), rel_tol=1e-12, abs_tol=1e-12)
    except (TypeError, ValueError):
        return False


def _causal_gate(path: Path, *, repository_root: Path) -> tuple[bool, str]:
    causal_path = _resolve(repository_root, path)
    status_path = _resolve(
        repository_root,
        os.getenv(
            "V12_CAUSAL_CERTIFICATION_STATUS_PATH",
            "research/v12-pre-armed-certification-status.json",
        ),
    )
    model_path = _resolve(
        repository_root,
        os.getenv(
            "V12_CAUSAL_FROZEN_MODEL_PATH",
            "research/v12-pre-armed-100-trade-frozen-model.json",
        ),
    )

    try:
        state = json.loads(causal_path.read_text(encoding="utf-8"))
        certification = json.loads(status_path.read_text(encoding="utf-8"))
        frozen_digest = _json_sha256(model_path)
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        return False, f"cannot verify causal certification bundle: {exc}"

    failures: list[str] = []
    metrics = state.get("metrics") or {}
    completion = state.get("completion") or {}
    ledger = state.get("ledger") or []

    closed = int(metrics.get("closed_trades") or 0)
    wins = int(metrics.get("wins") or 0)
    losses = int(metrics.get("losses") or 0)

    if completion.get("reached") is not True:
        failures.append("100-trade completion not reached")
    if int(completion.get("required_closed_trades") or 0) != 100:
        failures.append("required trade count is not exactly 100")
    if closed < 100:
        failures.append(f"closed_trades={closed}")
    if metrics.get("acceptance_gate_passed") is not True:
        failures.append("acceptance gate not passed")
    if state.get("real_money_execution") is not False:
        failures.append("causal state is not paper-live only")
    if int(state.get("production_paths_changed") or 0) != 0:
        failures.append("production paths changed during causal test")
    if str(state.get("model_sha256") or "") != EXPECTED_FROZEN_MODEL_SHA256:
        failures.append("causal state model fingerprint mismatch")
    if frozen_digest != EXPECTED_FROZEN_MODEL_SHA256:
        failures.append("frozen model file fingerprint mismatch")

    if len(ledger) != closed:
        failures.append(f"ledger length {len(ledger)} != closed_trades {closed}")
    else:
        recomputed_wins = 0
        recomputed_losses = 0
        recomputed_pnl = 0.0
        for index, row in enumerate(ledger):
            try:
                decision_ns = int(row["decision_ns"])
                fill_ns = int(row["fill_ns"])
                exit_ns = int(row["exit_ns"])
                entry = float(row["entry_cost_sol"])
                proceeds = float(row["proceeds_sol"])
                pnl = float(row["pnl_sol"])
            except (KeyError, TypeError, ValueError):
                failures.append(f"ledger row {index} is malformed")
                break
            if not (decision_ns <= fill_ns <= exit_ns):
                failures.append(f"ledger row {index} has impossible causal timestamps")
                break
            if not all(math.isfinite(value) for value in (entry, proceeds, pnl)):
                failures.append(f"ledger row {index} has non-finite economics")
                break
            recomputed_pnl += pnl
            if pnl > 0:
                recomputed_wins += 1
            else:
                recomputed_losses += 1

        if recomputed_wins != wins or recomputed_losses != losses:
            failures.append(
                "ledger win/loss totals do not match metrics "
                f"({recomputed_wins}W/{recomputed_losses}L vs {wins}W/{losses}L)"
            )
        if not _same_number(recomputed_pnl, metrics.get("net_pnl_sol")):
            failures.append("ledger net P&L does not match metrics")
        if wins + losses != closed:
            failures.append("metric wins + losses != closed trades")

    if certification.get("official_50_trade_baseline_valid") is not True:
        failures.append("official 50-trade baseline is not valid")
    if certification.get("pre_fix_100_trade_sample_invalidated") is not True:
        failures.append("old pre-fix sample is not explicitly invalidated")
    if certification.get("pre_fix_100_trade_sample_imported") is not False:
        failures.append("old pre-fix sample was imported")
    if certification.get("current_100_trade_confirmation_valid") is not True:
        failures.append("fresh 100-trade confirmation generation is not valid")
    if certification.get("current_100_trade_confirmation_generation") != "causal-v2-fresh-zero":
        failures.append("unexpected 100-trade confirmation generation")
    if certification.get("current_100_trade_workflow") != (
        ".github/workflows/v12-pre-armed-100-trade-causal.yml"
    ):
        failures.append("unexpected causal confirmation workflow")

    recertification = certification.get("causal_exit_recertification") or {}
    if int(recertification.get("impossible_exit_count_after") or -1) != 0:
        failures.append("recertified baseline still has impossible exits")
    if recertification.get("performance_metrics_changed") is not False:
        failures.append("recertification unexpectedly changed baseline performance")

    baseline = certification.get("official_50_trade_metrics") or {}
    for key, expected in EXPECTED_BASELINE.items():
        if not _same_number(baseline.get(key), expected):
            failures.append(f"official baseline mismatch for {key}")

    if failures:
        detail = "; ".join(failures[:8])
        if len(failures) > 8:
            detail += f"; +{len(failures) - 8} more"
        return False, detail

    return (
        True,
        f"closed={closed} gate=true model={frozen_digest[:12]} "
        f"baseline=35W/15L invalidated_sample_excluded=true",
    )

def _secure_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"https", "wss"} and bool(parsed.hostname)


def run_live_readiness(
    settings: object,
    *,
    repository_root: Path,
    acquire_lock: bool = True,
) -> tuple[list[ReadinessCheck], SingleInstanceLock | None]:
    checks: list[ReadinessCheck] = []

    enabled = _truthy("V12_LIVE_TRADING_ENABLED")
    checks.append(ReadinessCheck("explicit_live_enable", enabled, str(enabled)))

    try:
        validate_live_arm_token(
            os.getenv("V12_LIVE_ARM_TOKEN", ""),
            os.getenv("V12_LIVE_ARM_TOKEN_SHA256", ""),
        )
        checks.append(ReadinessCheck("live_arm_token", True, "valid"))
    except (PermissionError, ValueError) as exc:
        checks.append(ReadinessCheck("live_arm_token", False, str(exc)))

    causal_path = Path(
        os.getenv(
            "V12_CAUSAL_CERTIFICATION_STATE_PATH",
            "research/v12-pre-armed-100-causal-paper-live.json",
        )
    )
    causal_ok, causal_detail = _causal_gate(causal_path, repository_root=repository_root)
    checks.append(ReadinessCheck("causal_100_gate", causal_ok, causal_detail))

    wallet = str(getattr(settings, "wallet", "") or "")
    vault = str(getattr(settings, "vault", "") or "")
    checks.append(ReadinessCheck("trading_wallet", bool(wallet), wallet or "missing"))
    checks.append(
        ReadinessCheck(
            "storage_wallet",
            bool(vault and vault != wallet),
            "configured and distinct" if vault and vault != wallet else "missing or same as trading wallet",
        )
    )

    signer_command = tuple(getattr(settings, "signer_command", ()) or ())
    keypair_path = getattr(settings, "keypair_path", None)
    allow_local = _truthy("V12_ALLOW_LOCAL_KEYPAIR")
    signer_ok = bool(signer_command) or bool(keypair_path and allow_local)
    detail = "external signer" if signer_command else ("local keypair explicitly allowed" if signer_ok else "missing secure signer")
    checks.append(ReadinessCheck("signer", signer_ok, detail))
    if signer_command:
        binary = shutil.which(signer_command[0])
        checks.append(ReadinessCheck("signer_binary", bool(binary), binary or "not found"))
    if keypair_path:
        try:
            ensure_private_file(Path(keypair_path))
            permission_ok = allow_local
            permission_detail = "0600 and explicitly allowed" if allow_local else "plaintext local keypair prohibited by default"
        except (OSError, PermissionError) as exc:
            permission_ok = False
            permission_detail = str(exc)
        checks.append(ReadinessCheck("local_keypair_policy", permission_ok, permission_detail))

    builder = tuple(getattr(settings, "builder_command", ()) or ())
    builder_ok = bool(builder and shutil.which(builder[0]))
    checks.append(ReadinessCheck("builder", builder_ok, builder[0] if builder else "missing"))

    rpc_urls = tuple(
        dict.fromkeys(
            (
                str(getattr(settings, "rpc_url", "") or ""),
                *tuple(getattr(settings, "fallback_rpcs", ()) or ()),
            )
        )
    )
    rpc_urls = tuple(url for url in rpc_urls if url)
    checks.append(
        ReadinessCheck(
            "rpc_redundancy",
            len(rpc_urls) >= 2,
            f"{len(rpc_urls)} RPC endpoints",
        )
    )
    checks.append(
        ReadinessCheck(
            "rpc_transport_security",
            all(_secure_url(url) for url in rpc_urls),
            "all TLS" if rpc_urls and all(_secure_url(url) for url in rpc_urls) else "non-TLS endpoint present",
        )
    )

    route_urls = dict(getattr(settings, "route_urls", {}) or {})
    route_count = len(route_urls) + int(bool(getattr(settings, "direct_rpc_route", False)))
    checks.append(ReadinessCheck("route_redundancy", route_count >= 2, f"{route_count} routes"))
    insecure_routes = [url for url in route_urls.values() if not _secure_url(str(url))]
    checks.append(
        ReadinessCheck(
            "route_transport_security",
            not insecure_routes,
            "all TLS" if not insecure_routes else f"insecure={insecure_routes}",
        )
    )

    execution_db = Path(settings.execution_db)
    execution_db.parent.mkdir(parents=True, exist_ok=True)
    try:
        probe = sqlite3.connect(execution_db, timeout=5)
        row = probe.execute("PRAGMA quick_check").fetchone()
        probe.close()
        db_ok = bool(row and str(row[0]).lower() == "ok")
        db_detail = str(row[0]) if row else "no result"
    except sqlite3.Error as exc:
        db_ok = False
        db_detail = str(exc)
    checks.append(ReadinessCheck("execution_database", db_ok, db_detail))

    notifications = _truthy("V12_NOTIFICATIONS_ENABLED")
    has_discord = bool(
        os.getenv("DISCORD_TOKEN")
        and (os.getenv("V12_DISCORD_USER_ID") or os.getenv("V12_DISCORD_NOTIFICATION_CHANNEL_ID"))
    )
    has_phone = bool(os.getenv("PUSHOVER_APP_TOKEN") and os.getenv("PUSHOVER_USER_KEY"))
    checks.append(
        ReadinessCheck(
            "notifications",
            notifications and has_discord and has_phone,
            f"enabled={notifications} discord={has_discord} phone={has_phone}",
        )
    )

    try:
        decode_secret_key(os.getenv("V12_AUDIT_HMAC_KEY", ""))
        checks.append(ReadinessCheck("audit_hmac", True, "configured"))
    except ValueError as exc:
        checks.append(ReadinessCheck("audit_hmac", False, str(exc)))

    try:
        decode_aes_key(os.getenv("V12_BACKUP_AES_KEY", ""))
        checks.append(ReadinessCheck("encrypted_backups", True, "AES-256 key configured"))
    except ValueError as exc:
        checks.append(ReadinessCheck("encrypted_backups", False, str(exc)))

    manifest = Path(os.getenv("V12_INTEGRITY_MANIFEST", "security/v12-integrity-manifest.json"))
    try:
        result = IntegrityVerifier(
            repository_root,
            manifest,
            public_key=os.getenv("V12_INTEGRITY_PUBLIC_KEY", ""),
            signature=os.getenv("V12_INTEGRITY_SIGNATURE", ""),
            require_signature=not _truthy("V12_ALLOW_UNSIGNED_INTEGRITY"),
        ).verify()
        checks.append(
            ReadinessCheck(
                "code_integrity",
                True,
                f"files={result.files_checked} signed={result.signature_verified}",
            )
        )
    except (OSError, ValueError, RuntimeError) as exc:
        checks.append(ReadinessCheck("code_integrity", False, str(exc)))

    kill_switch = Path(os.getenv("V12_KILL_SWITCH_PATH", "run/V12_KILL"))
    checks.append(
        ReadinessCheck(
            "kill_switch_clear",
            not kill_switch.exists(),
            "clear" if not kill_switch.exists() else str(kill_switch),
        )
    )

    failed = [item for item in checks if not item.passed]
    if failed:
        raise LiveReadinessError(checks)

    lock: SingleInstanceLock | None = None
    if acquire_lock:
        lock = SingleInstanceLock(Path(os.getenv("V12_INSTANCE_LOCK", "run/v12-live.lock")))
        lock.acquire()
    return checks, lock


def readiness_json(checks: list[ReadinessCheck]) -> str:
    return json.dumps([asdict(item) for item in checks], indent=2, sort_keys=True)
