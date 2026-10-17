"""Persistent health checks independent of the trading process and laptop."""

import asyncio
import json
import os
import shutil
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import aiohttp

from .v12_prearmed import Notices
from .v12_prearmed_reporting import DailyReporter


def assessment(report, providers, *, intentionally_stopped=False, free_bytes=10**12):
    problems = []
    current = report.get("status", {})
    age = report.get("status_age_seconds")
    if not intentionally_stopped:
        if report.get("service", {}).get("ActiveState") != "active":
            problems.append("Trading service is not running")
        elif not isinstance(age, (int, float)) or not 0 <= age <= 10:
            problems.append("Trading status is stale or missing")
        if current.get("safety_mode") not in (None, "ACTIVE"):
            problems.append("Safety mode: " + str(current["safety_mode"]))
        if current.get("diagnostics", {}).get("pipeline_health") == "DEGRADED":
            problems.append("Recent processing failures need review")
        feed_timing = current.get('diagnostics', {}).get('timing', {}).get('feed', {})
        if (feed_timing.get('sustained_lag') is True
                and feed_timing.get('reference_fresh') is True
                and isinstance(age, (int, float)) and 0 <= age <= 5):
            problems.append('Incoming feed is persistently behind the confirmed chain')
    for provider in providers:
        if provider["health"] != "AVAILABLE":
            reason = (
                "request limit reached" if provider.get("http_status") == 429 else "unavailable"
            )
            problems.append(f"RPC provider {provider['provider_index'] + 1}: {reason}")
    if report.get("database", {}).get("pending_orders"):
        problems.append("A transaction is awaiting reconciliation")
    if (
        report.get("database", {}).get("positions")
        and report.get("execution_worker_running") is not True
    ):
        problems.append("Open positions remain but a running execution worker is not confirmed")
    if free_bytes < 1_000_000_000:
        problems.append("Server disk has less than 1 GB free")
    return problems


async def monitor(backend):
    path = backend.stage / "operator-data/health.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    previous = None
    if path.exists():
        try:
            previous = json.loads(path.read_text()).get("problems")
        except (ValueError, OSError):
            pass
    urls = list(
        dict.fromkeys(
            [os.environ["E4_PRIMARY_RPC_URL"], *os.getenv("E4_FALLBACK_RPC_URLS", "").split(",")]
        )
    )
    async with aiohttp.ClientSession(trust_env=False) as session:
        notices = Notices(session)
        daily = DailyReporter(backend.stage)
        while True:
            started = time.monotonic()
            providers = []
            for index, url in enumerate(filter(None, urls)):
                row = {
                    "provider_index": index,
                    "health": "UNAVAILABLE",
                    "checked_ns": time.time_ns(),
                }
                try:
                    async with session.post(
                        url,
                        json={
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "getSlot",
                            "params": [{"commitment": "confirmed"}],
                        },
                        timeout=aiohttp.ClientTimeout(total=8),
                    ) as response:
                        row["http_status"] = response.status
                        value = await response.json()
                        if response.status == 200 and type(value.get("result")) is int:
                            row.update(health="AVAILABLE", slot=value["result"])
                        else:
                            row["rpc_code"] = (value.get("error") or {}).get("code")
                except Exception as error:  # noqa: BLE001 - Boundary records failure and preserves recovery state.
                    row["error_type"] = type(error).__name__
                providers.append(row)
            report = {}
            intentionally_stopped = None
            try:
                report = await asyncio.to_thread(backend.status)
                intentionally_stopped = (
                    backend.stage / "prearmed-data/STOP"
                ).exists() or not backend.trading_expected()
                problems = assessment(
                    report,
                    providers,
                    intentionally_stopped=intentionally_stopped,
                    free_bytes=shutil.disk_usage(backend.stage).free,
                )
            except Exception as error:  # noqa: BLE001 - Boundary records failure and preserves recovery state.
                problems = ["Health collector failed: " + type(error).__name__]
            try:
                failed = await daily.tick(notices)
                if failed or daily.outbox.pending():
                    raise RuntimeError("DAILY_NOTIFICATION_DELIVERY_FAILED")
            except Exception as error:
                problems.append("Daily P/L reporting or delivery needs attention")
                print(json.dumps({"event": "DAILY_NOTIFICATION_FAILED", "ts_ns": time.time_ns(),
                                  "error_type": type(error).__name__}), flush=True)
            document = {
                "observed_ns": time.time_ns(),
                "providers": providers,
                "problems": problems,
                "severity": "WARNING" if problems else "OK",
                "status_age_seconds": report.get("status_age_seconds"),
                "status_updated_ns": report.get("status", {}).get("updated_ns"),
                "collector_observed_ns": report.get("observed_ns"),
                "service_state": report.get("service", {}).get("ActiveState"),
                "execution_worker_running": report.get("execution_worker_running"),
                "intentionally_stopped": intentionally_stopped,
            }
            temp = path.with_suffix(".tmp")
            temp.write_text(json.dumps(document, indent=2))
            temp.replace(path)
            print(
                json.dumps(
                    {"event": "OPERATOR_HEALTH_CHECK", "ts_ns": document["observed_ns"], **document}
                ),
                flush=True,
            )
            if previous != problems:
                stamp = datetime.now(ZoneInfo("Europe/London")).strftime("%d %b %Y · %H:%M:%S %Z")
                message = (
                    (
                        "[URGENT] Gambit needs attention"
                        if problems
                        else (
                            "[UPDATE] Gambit health monitoring started"
                            if previous is None
                            else "[RECOVERED] Gambit health checks recovered"
                        )
                    )
                    + "\n"
                    + stamp
                )
                message += (
                    "\n" + "\n".join("• " + problem for problem in problems)
                    if problems
                    else "\nNo current infrastructure faults reported."
                )
                message += "\nOpen /bot status and /bot logs for details."
                try:
                    await notices.send(message)
                    previous = problems
                except Exception as error:  # noqa: BLE001 - Boundary records failure and preserves recovery state.
                    print(
                        json.dumps(
                            {
                                "event": "HEALTH_NOTIFICATION_FAILED",
                                "ts_ns": time.time_ns(),
                                "error_type": type(error).__name__,
                            }
                        ),
                        flush=True,
                    )
            await asyncio.sleep(max(1, 60 - (time.monotonic() - started)))
