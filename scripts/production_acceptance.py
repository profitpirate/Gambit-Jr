#!/usr/bin/env python3
"""Deterministic, secret-safe V1.5 acceptance checks for the running VPS image."""

from __future__ import annotations

import ast
import json
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import discord

from memecoin_bot.config import Settings
from memecoin_bot.database import Store
from memecoin_bot.discord import bot_runtime
from memecoin_bot.discord.cards import (
    compare_card,
    creator_card,
    menu_card,
    narrative_card,
    performance_card,
    rows_card,
    scan_card,
    settings_card,
    smartmoney_card,
    status_card,
    token_card,
    wallet_card,
    watchlist_card,
)
from memecoin_bot.discord.cards import (
    test_alert_card as make_test_alert_card,
)
from memecoin_bot.discord.command_center import CommandCenterData, MenuView
from memecoin_bot.discord.validation import (
    validate_message,
    validate_view,
    validate_webhook_payload,
)
from memecoin_bot.signals import format_discord_event

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


def _registered_command_names() -> set[str]:
    source = Path(bot_runtime.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            function = decorator.func
            if not isinstance(function, ast.Attribute) or function.attr != "command":
                continue
            for keyword in decorator.keywords:
                if keyword.arg == "name" and isinstance(keyword.value, ast.Constant):
                    names.add(str(keyword.value.value))
    return names


def _validate_discord_artifacts(store: Store, settings: Settings) -> dict[str, int]:
    scan = scan_card(
        {
            "token_address": "So11111111111111111111111111111111111111112",
            "chain": "solana",
            "market": {},
            "survival": {},
            "payoff": {},
            "providers": {},
        }
    )
    token = {
        "token_address": "So11111111111111111111111111111111111111112",
        "chain": "solana",
        "symbol": "TEST",
        "name": "Acceptance Token",
        "wallet_intelligence": {},
    }
    cards = [
        menu_card(),
        status_card({}),
        scan,
        compare_card(scan, scan),
        watchlist_card([]),
        wallet_card({"wallet": "AcceptanceWallet"}),
        creator_card(None, "AcceptanceCreator"),
        narrative_card([]),
        token_card(token),
        smartmoney_card(token),
        rows_card("ACCEPTANCE", [], "No rows.", str),
        performance_card({}),
        settings_card(None),
        make_test_alert_card(),
    ]
    for payload in cards:
        validate_message(card_payload=payload)

    service = SimpleNamespace(
        started_at="1970-01-01T00:00:00+00:00",
        launch_queue=SimpleNamespace(stats=lambda: {"size": 0, "maxsize": 0}),
    )
    data = CommandCenterData(service, store, settings)
    views = [
        MenuView(data, timeout=900),
        MenuView(data, timeout=None),
        bot_runtime.ScanView(service, store, "So111", "solana", timeout=900),
        bot_runtime.ScanView(service, store, None, None, timeout=None),
    ]
    for view in views:
        validate_view(view)

    automatic = format_discord_event(
        "SIGNAL",
        {
            "classification": "STRONG",
            "chain": "solana",
            "token_address": token["token_address"],
            "name": token["name"],
            "symbol": token["symbol"],
            "component_scores": {
                name: 1
                for name in (
                    "narrative",
                    "social",
                    "onchain",
                    "developer",
                    "momentum",
                    "safety",
                )
            },
            "component_maxima": {
                name: 1
                for name in (
                    "narrative",
                    "social",
                    "onchain",
                    "developer",
                    "momentum",
                    "safety",
                )
            },
            "developer": {},
            "narrative": {},
            "social": {},
            "momentum": {},
            "v15_signal_tier": "STRONG",
            "runner_score": 80,
            "failure_score": 10,
            "evidence_coverage": 85,
            "entry_status": "OPEN",
            "survival_grade": "HIGH",
        },
    )
    validate_webhook_payload(automatic)
    return {
        "card_builders": len(cards),
        "views": len(views),
        "automatic_alert_payloads": 1,
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

    command_names = _registered_command_names()
    expected_names = set(bot_runtime.EXPECTED_COMMAND_NAMES)
    acceptance.require(
        "discord_commands",
        command_names == expected_names and len(command_names) == 24,
        {"count": len(command_names), "names": sorted(command_names)},
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
            payload_counts = _validate_discord_artifacts(store, settings)
            acceptance.require("discord_payloads", True, payload_counts)
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
