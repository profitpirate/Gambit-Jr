"""Multi-user isolated V12 runtime orchestrator for small beta deployments."""
from __future__ import annotations

import asyncio
import json
import os
import signal
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .access_store import AccessStore


@dataclass(slots=True)
class AccountProcess:
    user_id: int
    process: asyncio.subprocess.Process
    runtime_dir: Path
    started_ns: int


class AccountRuntimeManager:
    def __init__(
        self,
        access_store: AccessStore,
        *,
        state_root: Path,
        repository_root: Path,
        max_workers: int = 8,
    ):
        self.store = access_store
        self.state_root = state_root
        self.repository_root = repository_root
        self.max_workers = max(1, int(max_workers))
        self.processes: dict[int, AccountProcess] = {}
        self.stop_event = asyncio.Event()
        state_root.mkdir(parents=True, exist_ok=True)
        os.chmod(state_root, 0o700)

    def _desired_accounts(self) -> list[sqlite3.Row]:
        return self.store.conn.execute(
            """
            SELECT u.id AS user_id,u.discord_user_id,m.*,
                   c.provider,c.mode,c.public_identifier,c.secret_ref,c.state AS connection_state
            FROM access_users u
            JOIN access_mandates m ON m.user_id=u.id
            JOIN access_execution_connections c ON c.user_id=u.id
            WHERE u.enabled=1 AND m.enabled=1 AND c.state='READY'
              AND c.provider IN ('NATIVE_WALLET','MANAGED_WALLET')
            ORDER BY u.id
            """
        ).fetchall()

    def _environment(self, row: sqlite3.Row, runtime: Path) -> dict[str, str]:
        provider = str(row["provider"])
        secret_ref = str(row["secret_ref"] or "")
        if not secret_ref.startswith("vault://"):
            raise RuntimeError(
                f"user {row['user_id']} live signer is not backed by Vault Transit"
            )
        wallet = str(row["public_identifier"] or "")
        storage = str(row["storage_wallet"] or "")
        if not wallet or not storage or wallet == storage:
            raise RuntimeError(
                f"user {row['user_id']} requires distinct trading/storage wallets"
            )

        env = dict(os.environ)
        env.update(
            {
                "V12_ACCOUNT_ID": str(row["user_id"]),
                "DATABASE_PATH": os.getenv(
                    "V12_MARKETDATA_DB",
                    "/var/lib/gambit/marketdata.db",
                ),
                "E4_DATABASE_PATH": str(runtime / "execution.db"),
                "V12_AUDIT_LOG": str(runtime / "audit.jsonl"),
                "V12_BACKUP_DIR": str(runtime / "backups"),
                "V12_HEARTBEAT_PATH": str(runtime / "heartbeat.json"),
                "V12_KILL_SWITCH_PATH": str(runtime / "KILL"),
                "V12_INSTANCE_LOCK": str(runtime / "live.lock"),
                "E4_WALLET_PUBLIC_KEY": wallet,
                "E4_VAULT_PUBLIC_KEY": storage,
                "V12_ACCOUNT_MAX_BANKROLL_SOL": str(
                    max(0.0, float(row["max_active_bankroll_sol"]))
                ),
                "E4_MAX_POSITION_FRACTION": str(
                    min(0.20, float(row["max_position_fraction"]))
                ),
                "E4_MAX_CONCURRENT_POSITIONS": str(
                    min(2, int(row["max_concurrent_positions"]))
                ),
                "V12_SIGNER_SECRET_REF": secret_ref,
                "E4_SIGNER_COMMAND": (
                    f"{sys.executable} -m memecoin_bot.v12_vault_signer"
                ),
                "V12_DISCORD_USER_ID": str(row["discord_user_id"]),
            }
        )
        # Managed-wallet execution uses the same secure signer path; the provider
        # label affects provisioning/UX, not execution isolation.
        env["V12_ACCOUNT_PROVIDER"] = provider
        return env

    async def start_account(self, row: sqlite3.Row) -> AccountProcess:
        user_id = int(row["user_id"])
        runtime = self.state_root / str(user_id)
        runtime.mkdir(parents=True, exist_ok=True)
        os.chmod(runtime, 0o700)
        env = self._environment(row, runtime)
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(self.repository_root / "scripts/v12_supervisor.py"),
            "--heartbeat",
            str(runtime / "heartbeat.json"),
            "--kill-switch",
            str(runtime / "KILL"),
            "--",
            sys.executable,
            "-m",
            "memecoin_bot.e4_exec",
            "run",
            "--live",
            cwd=str(self.repository_root),
            env=env,
        )
        record = AccountProcess(user_id, process, runtime, time.time_ns())
        self.processes[user_id] = record
        self.store.audit(
            user_id,
            "ACCOUNT_RUNTIME_STARTED",
            json.dumps({"pid": process.pid, "provider": row["provider"]}),
        )
        return record

    async def stop_account(self, user_id: int, reason: str) -> None:
        record = self.processes.pop(int(user_id), None)
        if record is None:
            return
        if record.process.returncode is None:
            record.process.terminate()
            try:
                await asyncio.wait_for(record.process.wait(), timeout=15)
            except TimeoutError:
                record.process.kill()
                await record.process.wait()
        self.store.audit(
            int(user_id),
            "ACCOUNT_RUNTIME_STOPPED",
            json.dumps({"reason": reason}),
        )

    async def reconcile(self) -> None:
        desired = self._desired_accounts()
        desired_by_id = {int(row["user_id"]): row for row in desired[: self.max_workers]}

        for user_id in list(self.processes):
            record = self.processes[user_id]
            if user_id not in desired_by_id:
                await self.stop_account(user_id, "mandate_or_connection_disabled")
            elif record.process.returncode is not None:
                self.processes.pop(user_id, None)

        for user_id, row in desired_by_id.items():
            if user_id not in self.processes:
                try:
                    await self.start_account(row)
                except (RuntimeError, OSError, ValueError) as exc:
                    self.store.audit(
                        user_id,
                        "ACCOUNT_RUNTIME_START_FAILED",
                        json.dumps({"error": str(exc)}),
                    )

    async def run(self) -> None:
        while not self.stop_event.is_set():
            await self.reconcile()
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=5.0)
            except TimeoutError:
                pass
        await asyncio.gather(
            *[
                self.stop_account(user_id, "orchestrator_shutdown")
                for user_id in list(self.processes)
            ],
            return_exceptions=True,
        )

    def stop(self) -> None:
        self.stop_event.set()


async def run_from_env() -> None:
    store = AccessStore(Path(os.getenv("GAMBIT_ACCESS_DB", "data/v12-access.db")))
    manager = AccountRuntimeManager(
        store,
        state_root=Path(os.getenv("V12_ACCOUNTS_ROOT", "data/accounts")),
        repository_root=Path(os.getenv("GAMBIT_REPOSITORY_ROOT", ".")).resolve(),
        max_workers=int(os.getenv("V12_MAX_USER_WORKERS", "8")),
    )
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, manager.stop)
        except (NotImplementedError, RuntimeError):
            pass
    try:
        await manager.run()
    finally:
        store.close()


def main() -> int:
    asyncio.run(run_from_env())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
