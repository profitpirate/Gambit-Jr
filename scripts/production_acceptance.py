#!/usr/bin/env python3
"""Deterministic, secret-safe V1.5 acceptance checks for the running VPS image."""

from __future__ import annotations

import json
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

import discord

from memecoin_bot.config import Settings
from memecoin_bot.database import Store
from memecoin_bot.historical import ApprovedFeatureStore

EXPECTED_DISCORD_VERSION = "2.7.1"
VALID_PROVIDER_STATES = {
    "HEALTHY",
    "DEGRADED",
    "DOWN",
    "DISABLED",
    "UNKNOWN",
    "RATE_LIMITED",
    "CIRCUIT_OPEN",
}
V15_TABLES = {
    "v15_decisions",
    "v15_t0_calls",
    "provider_evidence_v15",
    "tradeability_v15",
}
V15_TRIGGERS = {"immutable_v15_t0_update", "immutable_v15_t0_delete"}


class Acceptance:
    def __init__(self) -> None:
        self.checks: dict[str, Any] = {}
        self.failures: list[str] = []

    def require(self, name: str, condition: bool, detail: Any) -> None:
        self.checks[name] = detail
        if not condition:
            self.failures.append(name)


def _validate_sniper_surfaces(root: Path) -> dict[str, Any]:
    portal = root / "src" / "memecoin_bot" / "portal_web"
    assets = {
        name: (portal / name).is_file()
        for name in ("index.html", "app.js", "styles.css")
    }
    legacy = [
        str(path.relative_to(root))
        for path in (
            root / "src/memecoin_bot/discord/bot_runtime.py",
            root / "src/memecoin_bot/discord/command_center.py",
            root / "src/memecoin_bot/discord/responses.py",
            root / "src/memecoin_bot/discord/cards.py",
        )
        if path.exists()
    ]
    notifier = root / "src/memecoin_bot/discord/notifier.py"
    control_plane = root / "src/memecoin_bot/control_plane.py"
    return {
        "portal_assets": assets,
        "portal_asset_count": sum(assets.values()),
        "legacy_command_files_present": legacy,
        "outbound_notifier": notifier.is_file(),
        "control_plane": control_plane.is_file(),
    }


def run_acceptance(settings: Settings) -> tuple[dict[str, Any], int]:
    acceptance = Acceptance()
    try:
        package_version = version("solana-memecoin-intelligence")
    except PackageNotFoundError:
        package_version = "NOT_INSTALLED"
    acceptance.require(
        "package_version",
        package_version == settings.software_version,
        package_version,
    )
    acceptance.require(
        "discord_py_version",
        discord.__version__ == EXPECTED_DISCORD_VERSION,
        discord.__version__,
    )
    separate_paths = {
        settings.database_path.resolve(),
        settings.historical_warehouse_path.resolve(),
        settings.approved_feature_store_path.resolve(),
    }
    acceptance.require(
        "storage_separation",
        len(separate_paths) == 3,
        sorted(str(path) for path in separate_paths),
    )
    approved_path = Path(settings.approved_feature_store_path)
    acceptance.require("approved_feature_store_exists", approved_path.is_file(), str(approved_path))
    if approved_path.is_file():
        feature_store = ApprovedFeatureStore(approved_path)
        try:
            feature_tables = {
                str(row[0])
                for row in feature_store.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            acceptance.require(
                "approved_feature_schema",
                {
                    "approved_feature_registry",
                    "production_feature_snapshots",
                    "production_context_audit",
                }
                <= feature_tables,
                sorted(feature_tables),
            )
        finally:
            feature_store.close()

    surface = _validate_sniper_surfaces(Path.cwd())
    acceptance.require(
        "discord_commands_removed",
        not surface["legacy_command_files_present"],
        {
            "legacy_files_present": surface["legacy_command_files_present"],
            "outbound_notifier": surface["outbound_notifier"],
        },
    )
    acceptance.require(
        "portal_assets",
        surface["portal_asset_count"] == 3
        and surface["control_plane"]
        and surface["outbound_notifier"],
        surface,
    )

    database_path = Path(settings.database_path)
    acceptance.require("database_exists", database_path.is_file(), str(database_path))
    store: Store | None = None
    if database_path.is_file():
        try:
            store = Store(database_path)
            quick_check = str(store.conn.execute("PRAGMA quick_check").fetchone()[0])
            acceptance.require("database_quick_check", quick_check == "ok", quick_check)
            journal_mode = str(store.conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
            acceptance.require("database_wal", journal_mode == "wal", journal_mode)

            expected_migrations = sorted(path.name for path in store.migrations_dir.glob("*.sql"))
            applied_migrations = sorted(
                str(row[0])
                for row in store.conn.execute("SELECT version FROM schema_migrations")
            )
            acceptance.require(
                "migration_status",
                applied_migrations == expected_migrations,
                {"applied": applied_migrations, "expected": expected_migrations},
            )
            reconciliation = store.state_reconciliation()
            acceptance.require(
                "state_reconciliation",
                reconciliation.get("difference") == 0 and reconciliation.get("reconciled") is True,
                reconciliation,
            )

            tables = {
                str(row[0])
                for row in store.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            triggers = {
                str(row[0])
                for row in store.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger'"
                )
            }
            acceptance.require(
                "v15_schema",
                V15_TABLES <= tables and V15_TRIGGERS <= triggers,
                {
                    "tables": sorted(V15_TABLES & tables),
                    "triggers": sorted(V15_TRIGGERS & triggers),
                },
            )
            fingerprint_count = int(
                store.conn.execute(
                    "SELECT COUNT(*) FROM config_fingerprints WHERE fingerprint=? "
                    "AND software_version=? AND scoring_version=? AND radar_version=?",
                    (
                        settings.config_fingerprint(),
                        settings.software_version,
                        settings.scoring_version,
                        settings.radar_version,
                    ),
                ).fetchone()[0]
            )
            acceptance.require(
                "v15_config_fingerprint",
                fingerprint_count >= 1,
                {
                    "software_version": settings.software_version,
                    "scoring_version": settings.scoring_version,
                    "feature_version": settings.feature_version,
                    "model_version": settings.model_version,
                    "radar_version": settings.radar_version,
                    "registered": fingerprint_count >= 1,
                },
            )
            provider_states = {
                str(row[0]): str(row[1])
                for row in store.conn.execute(
                    "SELECT provider,state FROM provider_health ORDER BY provider"
                )
            }
            acceptance.require(
                "provider_states",
                all(state in VALID_PROVIDER_STATES for state in provider_states.values()),
                provider_states,
            )
        except Exception as error:  # noqa: BLE001 - report a single deterministic gate failure
            acceptance.require(
                "acceptance_runtime",
                False,
                {"error_type": type(error).__name__, "message": str(error)[:300]},
            )
        finally:
            if store is not None:
                store.close()

    public_tiers = {"PREMIUM", "STRONG", "HIGH_RISK_MOMENTUM", "CATALYST_REVIVAL"}
    routing_ok = all(
        Store.alert_allowed("HOT_PLUS", "SIGNAL", {"v15_signal_tier": tier})
        for tier in public_tiers
    ) and not Store.alert_allowed(
        "HOT_PLUS", "SIGNAL", {"v15_signal_tier": "SILENT_WATCH"}
    )
    acceptance.require("v15_authoritative_signal_routing", routing_ok, sorted(public_tiers))

    provider_config = {
        "dexscreener": {"configured": True},
        "geckoterminal": {"configured": True},
        "solana_rpc": {"configured": True},
        "bsc_rpc": {"configured": True},
        "gmgn": {
            "enabled": settings.gmgn_enabled,
            "credential_configured": bool(settings.gmgn_api_key),
        },
        "direct_solana_launch": {"enabled": settings.pumpfun_discovery_enabled},
        "direct_bnb_launch": {"enabled": settings.bnb_launch_discovery_enabled},
        "discord": {
            "token_configured": bool(settings.discord_token),
            "webhook_configured": bool(settings.discord_webhook_url),
            "channel_count": len(settings.discord_channel_ids),
        },
    }
    result = {
        "result": "PASS" if not acceptance.failures else "FAIL",
        "checks": acceptance.checks,
        "provider_configuration_redacted": provider_config,
        "failed_checks": acceptance.failures,
    }
    return result, 0 if not acceptance.failures else 1


def main() -> int:
    result, exit_code = run_acceptance(Settings.from_env())
    print(json.dumps(result, indent=2, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
