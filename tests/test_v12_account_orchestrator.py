from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from memecoin_bot.access_store import AccessStore
from memecoin_bot.v12_account_orchestrator import AccountRuntimeManager


def row(**values):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    keys = list(values)
    placeholders = ",".join("?" for _ in keys)
    conn.execute(
        "CREATE TABLE x(" + ",".join(key + " TEXT" for key in keys) + ")"
    )
    conn.execute(
        "INSERT INTO x VALUES(" + placeholders + ")",
        tuple(str(values[key]) for key in keys),
    )
    return conn.execute("SELECT * FROM x").fetchone()


def test_user_runtime_environment_isolated_and_bounded(tmp_path: Path) -> None:
    store = AccessStore(tmp_path / "access.db")
    manager = AccountRuntimeManager(
        store,
        state_root=tmp_path / "accounts",
        repository_root=tmp_path,
    )
    values = row(
        user_id=7,
        provider="MANAGED_WALLET",
        secret_ref="vault://transit/user-7",
        public_identifier="TradeWallet",
        storage_wallet="StorageWallet",
        max_position_fraction="0.15",
        max_concurrent_positions="2",
        max_active_bankroll_sol="12.5",
        discord_user_id="123",
    )
    env = manager._environment(values, tmp_path / "accounts" / "7")
    assert env["E4_DATABASE_PATH"].endswith("/7/execution.db")
    assert env["E4_LIVE_CREATOR_OVERLAY"].endswith("/7/live-creator-overlay.json")
    assert env["E4_LIVE_OUTCOME_JOURNAL"].endswith("/7/live-outcomes.jsonl")
    assert env["E4_TEACHER_JOURNAL"].endswith("/7/teacher-observations.jsonl")
    assert env["E4_CREATOR_LEARNING_PATH"].endswith("/7/creator-learning.json")
    assert env["E4_DISCOVERY_QUEUE_PATH"].endswith("/7/discovery-queue.jsonl")
    assert env["V12_MARKETDATA_HEARTBEAT"].endswith("/run/gambit/v12-marketdata-heartbeat.json")
    assert env["V12_ACCOUNT_MAX_BANKROLL_SOL"] == "12.5"
    assert env["E4_MAX_POSITION_FRACTION"] == "0.15"
    assert env["E4_MAX_CONCURRENT_POSITIONS"] == "2"
    assert env["V12_ACCOUNT_MAX_CONCURRENT_POSITIONS"] == "2"
    assert env["V12_SIGNER_SECRET_REF"] == "vault://transit/user-7"


def test_non_vault_signer_is_rejected(tmp_path: Path) -> None:
    store = AccessStore(tmp_path / "access.db")
    manager = AccountRuntimeManager(
        store,
        state_root=tmp_path / "accounts",
        repository_root=tmp_path,
    )
    values = row(
        user_id=7,
        provider="MANAGED_WALLET",
        secret_ref="kms://not-implemented",
        public_identifier="TradeWallet",
        storage_wallet="StorageWallet",
        max_position_fraction="0.15",
        max_concurrent_positions="2",
        max_active_bankroll_sol="12.5",
        discord_user_id="123",
    )
    with pytest.raises(RuntimeError, match="Vault Transit"):
        manager._environment(values, tmp_path / "accounts" / "7")

def test_single_position_mandate_does_not_break_e4_invariant(tmp_path: Path) -> None:
    store = AccessStore(tmp_path / "access.db")
    manager = AccountRuntimeManager(
        store,
        state_root=tmp_path / "accounts",
        repository_root=tmp_path,
    )
    values = row(
        user_id=8,
        provider="NATIVE_WALLET",
        secret_ref="vault://transit/user-8",
        public_identifier="TradeWallet8",
        storage_wallet="StorageWallet8",
        max_position_fraction="0.10",
        max_concurrent_positions="1",
        max_active_bankroll_sol="5",
        discord_user_id="456",
    )
    env = manager._environment(values, tmp_path / "accounts" / "8")

    assert env["E4_MAX_CONCURRENT_POSITIONS"] == "2"
    assert env["V12_ACCOUNT_MAX_CONCURRENT_POSITIONS"] == "1"

