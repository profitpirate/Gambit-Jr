"""Operator entry point: probe, shadow, live. Never enables live flags itself."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import signal
import time
from pathlib import Path

import aiohttp

from .v12_prearmed_diagnostics import failure
from .v12_prearmed_discovery_budget import DiscoveryBudget
from .v12_prearmed_evidence import evaluate_research_acceptance, read_evidence
from .v12_prearmed_io import BuilderLanes, LiveExecution, PublicResolver, Rpc, native_feed, metadata_trace_config
from .v12_prearmed_library import PrearmedLibrary
from .v12_prearmed_notices import format_notice
from .v12_prearmed_runtime import Runtime, State


def true(name):
    return os.getenv(name, "").lower() == "true"


def settings(root, data, mode):
    from .e4_live import Settings

    value = Settings.from_env()
    value.live = mode == "live"
    value.execution_db = data / (mode + ".sqlite")
    value.builder_command = ("node", str(root / "tools/e4-builder/frozen-prearmed.mjs"))
    # The new runner broadcasts identical bytes through its two JSON-RPC providers.
    # No remote transaction builder or non-RPC route is used.
    value.route_urls = {f"rpc_{n}": url for n, url in enumerate(value.fallback_rpcs)}
    value.direct_rpc_route = True
    return value


def verify_source(root):
    from .v12_security import IntegrityVerifier

    path = Path(os.environ["V12_INTEGRITY_MANIFEST"])
    document = json.loads(path.read_text())
    required = {p.relative_to(root).as_posix() for p in (root / "src/memecoin_bot").rglob("*.py")}
    required |= {
        "tools/e4-builder/frozen-prearmed.mjs",
        "tools/e4-builder/package-lock.json",
        "tools/e4-builder/package.json",
        "tools/e4-builder/patch-anchor-bn-import.mjs",
    }
    if not required.issubset(document.get("files", {})):
        raise RuntimeError("INCOMPLETE_CODE_INTEGRITY_COVERAGE")
    return IntegrityVerifier(
        root,
        path,
        public_key=os.environ["V12_INTEGRITY_PUBLIC_KEY"],
        signature=os.environ["V12_INTEGRITY_SIGNATURE"],
        require_signature=True,
    ).verify()


def local_readiness(config, root, model_path, *, live):
    from .v12_live_readiness import run_live_readiness

    verify_source(root)
    if live and not (true("E4_LIVE") and true("V12_LIVE_TRADING_ENABLED")):
        raise PermissionError("BOTH_LIVE_ENABLE_FLAGS_REQUIRED")
    return run_live_readiness(
        config,
        repository_root=root,
        frozen_model_path=model_path,
        acquire_lock=live,
        check_arming=live,
        inspect_signer_file=live,
    )


class NoticeDeliveryError(RuntimeError):
    def __init__(self, failed_channels):
        self.failed_channels = failed_channels
        super().__init__("NOTIFICATION_DELIVERY_FAILED")


class Notices:
    def __init__(self, session):
        self.session = session

    async def validate(self):
        headers = {"Authorization": "Bot " + os.environ["DISCORD_TOKEN"]}
        channel = os.getenv("V12_DISCORD_NOTIFICATION_CHANNEL_ID")
        target = ("channels/" + channel) if channel else "users/@me"
        async with self.session.get(
            "https://discord.com/api/v10/" + target,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=8),
        ) as response:
            if response.status != 200:
                raise RuntimeError("DISCORD_CONFIGURATION_REJECTED")
        async with self.session.post(
            "https://api.pushover.net/1/users/validate.json",
            data={
                "token": os.environ["PUSHOVER_APP_TOKEN"],
                "user": os.environ["PUSHOVER_USER_KEY"],
            },
            timeout=aiohttp.ClientTimeout(total=8),
        ) as response:
            value = await response.json()
            if response.status != 200 or value.get("status") != 1:
                raise RuntimeError("PUSHOVER_CONFIGURATION_REJECTED")

    async def send_discord(self, message):
        headers = {"Authorization": "Bot " + os.environ["DISCORD_TOKEN"]}
        channel = os.getenv("V12_DISCORD_NOTIFICATION_CHANNEL_ID")
        if not channel:
            async with self.session.post(
                "https://discord.com/api/v10/users/@me/channels",
                headers=headers,
                json={"recipient_id": os.environ["V12_DISCORD_USER_ID"]},
                timeout=aiohttp.ClientTimeout(total=8),
            ) as response:
                if response.status not in (200, 201):
                    raise RuntimeError("DISCORD_DELIVERY_FAILED")
                channel = (await response.json())["id"]
        async with self.session.post(
            "https://discord.com/api/v10/channels/" + channel + "/messages",
            headers=headers,
            json={"content": message, "allowed_mentions": {"parse": []}},
            timeout=aiohttp.ClientTimeout(total=8),
        ) as response:
            if response.status not in (200, 201):
                raise RuntimeError("DISCORD_DELIVERY_FAILED")

    async def send_pushover(self, message):
        async with self.session.post(
            "https://api.pushover.net/1/messages.json",
            data={
                "token": os.environ["PUSHOVER_APP_TOKEN"],
                "user": os.environ["PUSHOVER_USER_KEY"],
                "message": message,
                "title": "Gambit",
                "priority": 1 if message.startswith("[URGENT]") else 0,
            },
            timeout=aiohttp.ClientTimeout(total=8),
        ) as response:
            if response.status != 200 or (await response.json()).get("status") != 1:
                raise RuntimeError("PUSHOVER_DELIVERY_FAILED")

    async def send(self, message):
        results = await asyncio.gather(
            self.send_discord(message), self.send_pushover(message), return_exceptions=True
        )
        if any(isinstance(result, BaseException) for result in results):
            raise NoticeDeliveryError(
                [
                    name
                    for name, result in zip(("discord", "phone"), results, strict=True)
                    if isinstance(result, BaseException)
                ]
            )


async def startup_network(config, builder, rpc, session, *, live):
    try:
        if hasattr(builder, "warmup"):
            await builder.warmup()
        return await network_probe(config, builder, rpc, session, check_feed=not live), True
    except Exception:
        if not live:
            raise
        # A degraded provider must not prevent journal recovery and exit attempts.
        # New entries remain disabled until an operator investigates and resets.
        return {}, False


async def startup_balance(runtime, *, live):
    try:
        await runtime.refresh_balance()
    except Exception:
        if not live:
            raise
        runtime.balance_ns = 0
        runtime.breaker.exit_only("startup_balance_unavailable")


async def network_probe(config, builder, rpc, session, *, check_feed=True):
    slots = []
    unavailable = []
    for index, url in enumerate(rpc.urls):
        try:
            genesis = await rpc.call("getGenesisHash", [], url=url)
            slot = await rpc.call("getSlot", [{"commitment": "confirmed"}], url=url)
        except RuntimeError:
            unavailable.append(index)
            continue
        if genesis != "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d":
            raise RuntimeError("RPC_IS_NOT_SOLANA_MAINNET")
        slots.append(slot)
    if len(slots) < 2 or max(slots) - min(slots) > 64:
        raise RuntimeError("RPC_REDUNDANCY_OR_SLOT_DIVERGENCE")
    balance = await rpc.balance_lamports(config.wallet)
    if check_feed and balance < 100_100_000:
        raise RuntimeError("WALLET_BELOW_TRANSACTION_AND_RESERVE_MINIMUM")
    await builder.request({"action": "HEALTH"})
    if not check_feed:
        return {"unavailable_provider_indices": unavailable, "mainnet_rpc_endpoints": len(slots)}
    stop, received = asyncio.Event(), asyncio.Event()
    count = 0

    async def emit(_signature, events, _time):
        nonlocal count
        count += len(events)
        if events:
            received.set()

    feed = asyncio.create_task(native_feed(session, rpc.urls, emit, lambda _: None, stop))
    try:
        await asyncio.wait_for(received.wait(), 40)
    except TimeoutError:
        raise RuntimeError("LIVE_FEED_NOT_RECEIVING_DECODABLE_PUMP_EVENTS") from None
    finally:
        stop.set()
        feed.cancel()
        await asyncio.gather(feed, return_exceptions=True)
    return {
        "mainnet_rpc_endpoints": len(slots),
        "feed_events": count,
        "wallet_balance_sol": balance / 1e9,
        "builder_health": "PASS",
        "transaction_sent": False,
        "signer_opened": False,
        "unavailable_provider_indices": unavailable,
    }


async def run(args):
    from .v12_backup import EncryptedBackupManager, decode_aes_key
    from .v12_execution_journal import ExecutionJournal
    from .v12_security import TamperEvidentAuditLog, decode_secret_key

    root, data = Path(args.root).resolve(), Path(args.data).resolve()
    data.mkdir(parents=True, exist_ok=True)
    model_path = Path(os.environ["V12_FROZEN_MODEL_PATH"])
    model = read_evidence(model_path)
    evidence = read_evidence(Path(os.environ["V12_CAUSAL_CERTIFICATION_STATE_PATH"]))
    if not evaluate_research_acceptance(model, evidence).accepted:
        raise RuntimeError("FROZEN_RESEARCH_EVIDENCE_REJECTED")
    mode = args.mode
    config = settings(root, data, "shadow" if mode == "probe" else mode)
    _checks, lock = local_readiness(config, root, model_path, live=mode == "live")
    creator_library = PrearmedLibrary.from_root(root, model)
    builder = BuilderLanes(config.builder_command)
    runtime, journal, state, feed = None, None, None, None
    stop = asyncio.Event()
    connector = aiohttp.TCPConnector(resolver=PublicResolver(), use_dns_cache=True,
                                    ttl_dns_cache=15, keepalive_timeout=60, limit=12, limit_per_host=4)
    try:
        async with (
            aiohttp.ClientSession() as session,
            aiohttp.ClientSession(connector=connector, trust_env=False, trace_configs=[metadata_trace_config()]) as meta_session,
        ):
            discovery_budget = DiscoveryBudget(data / (mode + "-discovery-budget.json"))
            rpc = Rpc(session, (config.rpc_url, *config.fallback_rpcs), discovery_budget=discovery_budget)
            notices = Notices(session)
            notices_ok = True
            try:
                await notices.validate()
            except Exception:
                if mode != "live":
                    raise
                notices_ok = False
            report, network_ok = await startup_network(
                config, builder, rpc, session, live=mode == "live"
            )
            if mode == "probe":
                print(json.dumps({"status": "READ_ONLY_CONNECTIVITY_PASS", **report}, indent=2))
                return
            audit = TamperEvidentAuditLog(
                data / (mode + "-audit.jsonl"), decode_secret_key(os.environ["V12_AUDIT_HMAC_KEY"])
            )
            if not audit.verify():
                raise RuntimeError("AUDIT_CHAIN_INVALID")
            notice_ready = asyncio.Event()
            fault_notice_ns = 0

            def emit(event, payload):
                nonlocal fault_notice_ns
                audit.append(event, payload)
                print(json.dumps({"ts_ns": time.time_ns(), "event": event, **payload}), flush=True)
                notify_event = event
                notify = event in {
                    "FILL_CONFIRMED",
                    "ORDER_FAILED",
                    "EXECUTION_OUTCOME_UNKNOWN",
                    "RUNTIME_TASK_FAILED",
                    "BACKGROUND_WORKER_STOPPED",
                    "SAFETY_MODE_CHANGED",
                }
                if (
                    not notify
                    and payload.get("category") == "fault"
                    and time.time_ns() - fault_notice_ns >= 60_000_000_000
                ):
                    fault_notice_ns = time.time_ns()
                    notify, notify_event = True, (
                        "CANDIDATE_UNAVAILABLE" if event == "CANDIDATE_REJECTED" else "PIPELINE_DEGRADED"
                    )
                if notify:
                    if state.reports.outbox.pending() > 200:
                        raise RuntimeError("NOTIFICATION_BACKLOG")
                    # Fills/failures were persisted atomically with their applied
                    # state. Other diagnostics are durable before delivery too.
                    if event not in {"FILL_CONFIRMED", "ORDER_FAILED"}:
                        ns = time.time_ns()
                        message, _priority = format_notice(notify_event, payload, ts_ns=ns)
                        state.reports.outbox.enqueue(f"event:{event}:{ns}", message, ns)
                    notice_ready.set()

            state = State(config.execution_db, wallet=config.wallet, mode=mode)
            journal = ExecutionJournal(state.conn)
            execution = (
                LiveExecution(settings=config, rpc=rpc, builder=builder, journal=journal)
                if mode == "live"
                else None
            )
            runtime = Runtime(
                model=model,
                state=state,
                rpc=rpc,
                builder=builder,
                execution=execution,
                wallet=config.wallet,
                metadata_session=meta_session,
                emit=emit,
                kill_path=os.environ["V12_KILL_SWITCH_PATH"],
                position_percent=int(os.getenv("GAMBIT_POSITION_PERCENT", "10")),
                maximum_positions=int(os.getenv("GAMBIT_MAXIMUM_POSITIONS", "2")),
                creator_library=creator_library,
            )
            runtime.diagnostics.record(
                "CREATOR_LIBRARY_LOADED",
                stage="startup",
                reason="OWNER_ENABLED_PROMOTED_RECOGNITION",
                **creator_library.status(),
            )
            rpc.trace = runtime.diagnostics.record
            if mode == "live" and true("GAMBIT_RECOVERY_ONLY"):
                runtime.stopping = True
            runtime.source_sha256 = hashlib.sha256(
                Path(os.environ["V12_INTEGRITY_MANIFEST"]).read_bytes()
            ).hexdigest()
            if any(
                entry.request_id not in state.data["pending"] for entry in journal.recoverable()
            ):
                runtime.breaker.exit_only("orphaned_journal_transaction_requires_reconciliation")
            if not notices_ok:
                runtime.breaker.exit_only("notification_validation_failed")
            if not network_ok:
                runtime.breaker.exit_only("startup_rpc_validation_failed")
            if execution:
                await runtime.reconcile()
            await startup_balance(runtime, live=mode == "live")
            if execution:
                await runtime.tick()
                try:
                    ns = time.time_ns()
                    state.reports.outbox.enqueue(f"startup:{ns}",
                        "Gambit frozen Pre-Armed live process starting. "
                        f"Open positions: {len(state.data['positions'])}; unresolved orders: {len(state.data['pending'])}.", ns
                    )
                    if await state.reports.outbox.flush(notices):
                        raise NoticeDeliveryError(["startup_delivery"])
                except Exception:  # noqa: BLE001 - Boundary records failure and preserves recovery state.
                    runtime.breaker.exit_only("notification_delivery_failed")
            feed = asyncio.create_task(
                native_feed(
                    session,
                    rpc.urls,
                    runtime.on_batch,
                    runtime.feed_health,
                    stop,
                    runtime.diagnostics.record,
                    discovery_budget=discovery_budget,
                )
            )

            def stopping(*_):
                runtime.stopping = True

            for sig in (signal.SIGINT, signal.SIGTERM):
                signal.signal(sig, stopping)

            async def balances():
                while not stop.is_set():
                    # Consume any prior request before reading; if an order is
                    # resolved during this read, its generation change keeps
                    # the old snapshot stale and leaves a wakeup for the next
                    # immediate read.
                    runtime.balance_refresh_requested.clear()
                    try:
                        await runtime.refresh_balance()
                    except (RuntimeError, KeyError, ValueError) as error:
                        if runtime.balance_ns:
                            runtime.diagnostics.record(
                                "BALANCE_REFRESH_FAILED",
                                stage="balance",
                                category="fault",
                                **failure("balance", error),
                            )
                        runtime.balance_ns = 0
                    await runtime.wait_for_balance_refresh(timeout=1)

            async def notifications():
                while not stop.is_set():
                    notice_ready.clear()
                    try:
                        failed = await state.reports.outbox.flush(notices)
                        if failed:
                            raise NoticeDeliveryError(failed)
                    except Exception as error:  # noqa: BLE001 - Boundary records failure and preserves recovery state.
                        runtime.breaker.exit_only("notification_delivery_failed")
                        audit.append(
                            "NOTIFICATION_DELIVERY_FAILED",
                            {"failed_channels": getattr(error, "failed_channels", ["unknown"])},
                        )
                    try:
                        await asyncio.wait_for(notice_ready.wait(), timeout=2)
                    except TimeoutError:
                        pass

            balance_task = asyncio.create_task(balances())
            notice_task = asyncio.create_task(notifications())
            backup = EncryptedBackupManager(
                data / "backups", decode_aes_key(os.environ["V12_BACKUP_AES_KEY"])
            )
            next_report = next_backup = 0
            backup_task = None
            expected_wake = started = time.monotonic()
            emit("RUNNER_STARTED", {"mode": mode, "strategy": runtime.policy.family})
            try:
                while True:
                    runtime.diagnostics.timings.observe('event_loop_lag_ms',
                        max(0, (time.monotonic()-expected_wake)*1000), time.time_ns())
                    for name, task in (
                        ("feed", feed),
                        ("balance", balance_task),
                        ("notifications", notice_task),
                    ):
                        if task.done() and name not in runtime.failed_background_tasks:
                            runtime.failed_background_tasks.add(name)
                            error = None if task.cancelled() else task.exception()
                            runtime.breaker.exit_only("background_worker_stopped")
                            runtime.diagnostics.record(
                                "BACKGROUND_WORKER_STOPPED",
                                stage=name,
                                category="fault",
                                **failure(name, error or RuntimeError("worker stopped")),
                            )
                    await runtime.tick()
                    now = time.monotonic()
                    if now >= next_report:
                        status = runtime.status()
                        temp = data / (mode + "-status.tmp")
                        temp.write_text(json.dumps(status, indent=2))
                        temp.replace(data / (mode + "-status.json"))
                        next_report = now + 1
                    if now >= next_backup:
                        if backup_task is None or backup_task.done():
                            backup_task = runtime.spawn(
                                asyncio.to_thread(backup.backup, config.execution_db, label=mode)
                            )
                        next_backup = now + 60
                    if args.seconds and now - started >= args.seconds:
                        runtime.stopping = True
                    if (
                        runtime.stopping
                        and not state.data["pending"]
                        and not state.data["positions"]
                    ):
                        break
                    expected_wake = time.monotonic() + 0.025
                    await asyncio.sleep(0.025)
            finally:
                stop.set()
                for task in (balance_task, notice_task, feed):
                    task.cancel()
                await asyncio.gather(balance_task, notice_task, feed, return_exceptions=True)
                await asyncio.gather(*runtime.tasks, return_exceptions=True)
                if mode == "live":
                    try:
                        failed = await asyncio.wait_for(state.reports.outbox.flush(notices), timeout=10)
                        if failed:
                            audit.append("NOTIFICATION_DELIVERY_FAILED", {"failed_channels": failed})
                    except Exception:
                        # Undelivered messages remain durable for the next start.
                        audit.append("NOTIFICATION_DELIVERY_FAILED", {"failed_channels": ["shutdown"]})
                backup.backup(config.execution_db, label=mode)
    finally:
        await builder.close()
        if state:
            state.conn.close()
        if lock:
            lock.release()


def main(argv=None):
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("mode", choices=("probe", "shadow", "live"))
    parser.add_argument("--root", default="/app")
    parser.add_argument("--data", default="/data")
    parser.add_argument("--seconds", type=int, default=0)
    args = parser.parse_args(argv)
    try:
        asyncio.run(run(args))
    except Exception as exc:  # noqa: BLE001 - Boundary records failure and preserves recovery state.
        from .v12_live_readiness import LiveReadinessError

        if isinstance(exc, LiveReadinessError):
            print(
                json.dumps(
                    {
                        "status": "BLOCKED",
                        "failed_checks": [c.name for c in exc.checks if not c.passed],
                    }
                )
            )
        else:
            # Exceptions from RPC/SDK/HTTP can embed credentials. Print only the type.
            import re

            message = str(exc)
            safe = (
                message if re.fullmatch(r"[A-Z][A-Z0-9_:]{0,120}", message) else type(exc).__name__
            )
            print(json.dumps({"status": "BLOCKED", "reason": safe}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
