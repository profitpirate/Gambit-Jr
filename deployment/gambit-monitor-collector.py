"""Fixed read-only SSH report. No wallet/env access, network calls or controls."""

import json
import math
import os
import re
import sqlite3
import subprocess
import time
from contextlib import closing
from pathlib import Path

DATA = Path("/opt/gambit-v12-staging/prearmed-data")
SERVICE = "gambit-prearmed-live"
STATUS_FIELDS = {
    "mode",
    "feed_connected",
    "feed_events",
    "signals",
    "positions",
    "unresolved_orders",
    "entries_allowed",
    "safety_mode",
    "safety_reason",
    "closed_trades",
    "net_realized_sol",
    "updated_ns",
    "position_percent",
    "maximum_positions",
    "source_sha256",
}
EVENTS = {
    "DECISION_EVIDENCE",
    "BALANCE_REFRESH_DELAYED",
    "FEED_SOURCE_CONNECTED",
    "FEED_SOURCE_ROTATION",
    "DISCOVERY_BUDGET_EXHAUSTED",
    "FEED_STREAM_ENDED",
    "CANDIDATE_FILTERED",
    "FILL_CONFIRMED",
    "PENDING_MARKET_INCOMPLETE",
    "POSITION_RECOVERY_EXIT",
    "ORDER_FAILED",
    "EXECUTION_OUTCOME_UNKNOWN",
    "EXECUTION_EXPIRED",
    "RUNTIME_TASK_FAILED",
    "SAFETY_MODE_CHANGED",
    "NOTIFICATION_DELIVERY_FAILED",
    "RUNNER_STARTED",
    "CANDIDATE_REJECTED",
    "CANDIDATE_STARTED",
    "CANDIDATE_PROGRESS",
    "FROZEN_SIGNAL",
    "CANDIDATE_SHADOW_COMPLETE",
    "ORDER_INTENT",
    "FEED_RECONNECT",
    "BALANCE_REFRESH_FAILED",
    "BACKGROUND_WORKER_STOPPED",
    "RPC_REQUEST_FAILED",
    "EXECUTION_STARTED",
    "EXECUTION_PROGRESS",
    "EXECUTION_FAILED",
    "ROUTE_ACCEPTED",
    "ROUTE_FAILED",
    "RECEIPT_CHECK_FAILED",
    "POSITION_DATA_REJECTED",
    "LIVE_TEST_STARTED",
    "WALLET_OPERATION_RESULT",
    "CREATOR_LIBRARY_LOADED",
}


def scalar(value):
    if value is None or type(value) in (bool, int):
        return value
    if type(value) is float:
        return value if math.isfinite(value) else None
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_:.\-]{1,128}", value):
        return value
    return "[redacted]"


def select(row, names):
    return {
        name: (
            [scalar(value) for value in row[name][:30]]
            if name in {"blockers", "failed_channels"} and isinstance(row[name], list)
            else scalar(row[name])
        )
        for name in names
        if name in row
    }


def regular(path):
    if path.is_symlink() or path.resolve() != path:
        raise ValueError("Unexpected report path")
    return path


def service_status():
    result = subprocess.run(
        [
            "/usr/bin/systemctl",
            "show",
            SERVICE,
            "--property=ActiveState,SubState,NRestarts,ExecMainStatus,ActiveEnterTimestampMonotonic",
        ],
        capture_output=True,
        text=True,
        timeout=8,
        check=False,
    )
    if result.returncode:
        raise RuntimeError("Service query failed")
    return {
        key: scalar(value)
        for key, value in (line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    }


def database_snapshot(path, now):
    if not path.exists():
        return {"available": False}
    # mode=ro retains correct WAL visibility; immutable=1 would miss active writes.
    with closing(sqlite3.connect(regular(path).as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        row = db.execute("SELECT data FROM frozen_state WHERE id=1").fetchone()
        state = json.loads(row[0]) if row else {}
        result = {
            "available": True,
            "state": select(state, {"mode", "model", "closed", "realized_lamports"}),
        }
        result["positions"] = [
            dict(
                mint=scalar(mint),
                **select(position, {"original", "remaining", "forced_exit", "last_attempt_ns"}),
                fill_ns=scalar(position.get("exit", {}).get("fill_ns")),
            )
            for mint, position in list(state.get("positions", {}).items())[:20]
        ]
        result["pending_orders"] = [
            dict(
                request_id=scalar(request_id),
                **select(order.get("payload", {}), {"side", "mint", "submitted_ns"}),
                reason=scalar(order.get("reason")),
            )
            for request_id, order in list(state.get("pending", {}).items())[:100]
        ]
        result["journal_state_counts"] = {
            row[0]: row[1]
            for row in db.execute(
                "SELECT state, COUNT(*) FROM v12_execution_journal GROUP BY state"
            )
        }
        # Never select signed transaction bytes, payload_json, or arbitrary errors.
        rows = db.execute(
            "SELECT request_id,side,mint,state,signature,attempts,created_ns,updated_ns "
            "FROM v12_execution_journal WHERE updated_ns>=? ORDER BY updated_ns DESC LIMIT 1001",
            (now - 86_400_000_000_000,),
        ).fetchall()
        result["journal_truncated"] = len(rows) > 1000
        result["recent_orders"] = [select(dict(row), row.keys()) for row in rows[:1000]]
        safety = db.execute(
            "SELECT mode,reason,updated_ns,peak_equity_sol,day_start_equity_sol,"
            "consecutive_losses,tx_failures_window FROM v12_safety_state WHERE singleton=1"
        ).fetchone()
        result["safety"] = select(dict(safety), safety.keys()) if safety else {}
        db.rollback()
        return result


def audit_tail(path, now):
    if not path.exists():
        return {"available": False, "events": []}
    limit = 2_097_152
    with regular(path).open("rb") as stream:
        size = os.fstat(stream.fileno()).st_size
        offset = max(0, size - limit)
        stream.seek(offset)
        if offset:
            stream.readline()  # Discard a potentially partial first record.
        lines = stream.read(limit).splitlines()
    events, malformed = [], 0
    for line in lines:
        try:
            row = json.loads(line)
            if row.get("event") not in EVENTS or row.get("ts_ns", 0) < now - 86_400_000_000_000:
                continue
            events.append(
                {
                    "ts_ns": row["ts_ns"],
                    "event": row["event"],
                    "payload": select(
                        row.get("payload", {}),
                        {
                            "mint",
                            "side",
                            "tokens_raw",
                            "signature",
                            "request_id",
                            "mode",
                            "reason",
                            "stage",
                            "category",
                            "candidate_id",
                            "run_id",
                            "elapsed_ms",
                            "error_type",
                            "http_status",
                            "metadata_host",
                            "metadata_uri_sha256",
                            "metadata_attempt",
                            "metadata_attempt_ms",
                            "metadata_dns_ms",
                            "metadata_connect_ms",
                            "metadata_ttfb_ms",
                            "metadata_body_ms",
                            "metadata_reused_connection",
                            "metadata_gateway_fallback",
                            "metadata_cid_verified",
                            "retry_after_seconds",
                            "builder_state_read_ms",
                            "builder_total_ms",
            'builder_blockhash_ready_ms',
            'builder_balance_read_ms',
            'builder_rpc_route_ms',
            'decision_accepted',
            'model_sha256',
            'cash_lamports',
            'occupied_positions',
            'pending_orders',
            'fact_creator',
            'fact_social_handle',
            'fact_social_status_ns',
            'fact_create_ns',
            'fact_decision_ns',
            'fact_metadata_observed_ns',
            'fact_creator_seed_sol',
            'fact_mayhem_mode',
            'fact_expected_token_output',
            'fact_current_token_output',
            'fact_create_price',
            'fact_current_price',

                            "quote_prefetch_reused",
                            "quote_prefetch_age_ms",
                            "builder_simulation_ms",
                            "fill_clock_source",
                            "fill_observed_ns",
                            "hold_deadline_ns",
                            "receipt_observed_ns",
                            "pending_market_events",
                            "replayed_market_events",
                            "recovery_exit_reason",
                            "exit_latched_reason",
                            "retrying",
                            "creator_verification",
                            "curve_creator",
                            "fee_sharing_admin",
                            "fee_sharing_admin_revoked",
                            "provider_index",
                            "observed_ns",
                            "rpc_method",
                            "rpc_code",
                            "rpc_instruction_index", "rpc_custom_error",
                            "social_age_ms",
                            "source_location",
                            "remote_error_type",
                            "websocket_close_code",
                            "creator",
                            "creator_seed_lamports",
                            "minimum_seed_lamports",
                            "blockers",
                            "balance_refresh_gap_ms",
                            "failed_channels",
                            "social_window_ms",
                            "price_multiple",
                            "maximum_price_multiple",
                            "launch_price",
                            "curve_price",
                            "state_slot",
                            "feed_slot",
                            "feed_received_ns",
                            "candidate_started_ns",
                            "chain_timestamp_seconds",
                            "metadata_shared_request",
                            "budget_lamports",
                            "observed_to_decision_ms",
                            "operation_id",
                            "state",
                            "action",
                            "library_status",
                            "library_tier",
                            "creator_recognised",
                            "creator_source",
                            "prearmed_social_available",
                            "historical_social_required",
                            "library_history_certified",
                            "library_sha256",
                            "promoted",
                            "shortlisted",
                            "additional_creators",
                            "recognised_creators",
                            "promoted_missing_social_history",
                            "promoted_social_history_required",
                            "history_certified",
                            "sha256",
                            "activation_basis",
                        },
                    ),
                }
            )
        except (ValueError, TypeError, KeyError):
            malformed += 1
    return {
        "available": True,
        "bytes_read_limit": limit,
        "tail_only": offset > 0,
        "events_truncated": len(events) > 500,
        "malformed_records": malformed,
        "hmac_verification_performed": False,
        "events": events[-500:],
    }


def status_snapshot(path):
    value = json.loads(regular(path).read_text())
    result = select(value, STATUS_FIELDS)
    result.update(select(value, {"balance_updated_ns", "balance_age_ms", "balance_refresh_started_ns",
                                 "balance_refresh_duration_ms", "balance_refresh_inflight"}))
    if isinstance(value.get("discovery"), dict):
        result["discovery"] = select(value["discovery"], {
            "mode", "stream_provider", "stream_provider_index", "stream_bytes_received",
            "stream_messages_received", "creates_decoded", "utc_day", "paid_stream_daily_limit_bytes",
            "paid_balance_daily_limit_requests", "scope", "helius_stream_reserved_bytes",
            "helius_balance_requests", "quicknode_stream_reserved_bytes", "quicknode_balance_requests",
        })
    if isinstance(value.get("creator_library"), dict):
        result["creator_library"] = select(
            value["creator_library"],
            {
                "state",
                "mode",
                "activation_basis",
                "history_certified",
                "sha256",
                "promoted",
                "shortlisted",
                "elite",
                "additional_creators",
                "recognised_creators",
                "promoted_missing_social_history",
                "promoted_social_history_required",
                "auto_buy",
            },
        )
    if isinstance(value.get("entry_blockers"), list):
        result["entry_blockers"] = [scalar(v) for v in value["entry_blockers"][:30]]
    if isinstance(value.get("provider_health"), list):
        result["provider_health"] = [
            select(
                row,
                {
                    "provider_index",
                    "health",
                    "failures",
                    "last_success_ns",
                    "last_failure_ns",
                    "http_status",
                },
            )
            for row in value["provider_health"][:10]
            if isinstance(row, dict)
        ]
    diagnostics = value.get("diagnostics")
    if isinstance(diagnostics, dict):
        safe = select(
            diagnostics,
            {
                "schema",
                "run_id",
                "recent_fault_count",
                "last_progress_ns",
                "last_fault_ns",
                "pipeline_health",
                "execution_verification",
            },
        )
        for field in ("counters", "reasons"):
            safe[field] = {
                k: v
                for k, v in list(diagnostics.get(field, {}).items())[:100]
                if re.fullmatch(r"[A-Z0-9_]{1,100}", k) and type(v) is int and v >= 0
            }
        timing = diagnostics.get('timing')
        if isinstance(timing, dict):
            allowed_metrics = {'metadata_body_ms', 'build_rpc_route_ms', 'build_state_read_ms', 'build_total_ms', 'metadata_connect_ms', 'quote_rpc_route_ms', 'balance_refresh_ms', 'metadata_attempt_ms', 'metadata_ttfb_ms', 'feed_dispatch_ms', 'candidate_queue_ms', 'build_simulation_ms', 'quote_total_ms', 'feed_decode_ms', 'create_age_approx_ms', 'build_blockhash_ready_ms', 'event_loop_lag_ms', 'metadata_dns_ms', 'quote_state_read_ms', 'observed_to_decision_ms', 'build_balance_read_ms'}
            rows = timing.get('metrics', {})
            safe['timing'] = {'window_samples': 512, 'metrics': {
                name: select(row, {'count','samples','latest_ns','last','max','p50','p95','p99'})
                for name, row in rows.items() if name in allowed_metrics and isinstance(row, dict)
            } if isinstance(rows, dict) else {}}
            feed = timing.get('feed', {})
            if isinstance(feed, dict):
                safe['timing']['feed'] = select(feed, {'reference','chain_age_resolution_ms',
                    'head_slot','head_observed_ns','feed_slot','feed_observed_ns',
                    'reference_fresh','lag_slots','lag_since_ns','sustained_lag'})
        result["diagnostics"] = safe
    return result


def health_record(row):
    result = select(
        row,
        {
            "observed_ns",
            "severity",
            "status_age_seconds",
            "status_updated_ns",
            "collector_observed_ns",
            "service_state",
            "execution_worker_running",
            "intentionally_stopped",
        },
    )
    fixed = {
        "Trading service is not running",
        "Trading status is stale or missing",
        "Recent processing failures need review",
        "Incoming feed is persistently behind the confirmed chain",
        "A transaction is awaiting reconciliation",
        "Open positions remain but a running execution worker is not confirmed",
        "Server disk has less than 1 GB free",
        "Daily P/L reporting or delivery needs attention",
    }
    result["problems"] = [
        value
        if isinstance(value, str)
        and (
            value in fixed
            or re.fullmatch(
                r"(?:RPC provider \d+: (?:request limit reached|unavailable)|Health collector failed: [A-Za-z_]{1,64}|Safety mode: [A-Z_]{1,32})",
                value,
            )
        )
        else "Unrecognized health problem"
        for value in row.get("problems", [])[:20]
    ]
    result["providers"] = [
        select(
            provider,
            {
                "provider_index",
                "health",
                "checked_ns",
                "http_status",
                "rpc_code",
                "rpc_instruction_index", "rpc_custom_error",
                "slot",
                "error_type",
            },
        )
        for provider in row.get("providers", [])[:10]
        if isinstance(provider, dict)
    ]
    return result


def health_history():
    # Read fixed service logs only. Raw journal text and tracebacks are never exported.
    result = subprocess.run(
        [
            "/usr/bin/journalctl",
            "-u",
            "gambit-operator",
            "--since=-2h",
            "-n",
            "300",
            "--no-pager",
            "-o",
            "cat",
        ],
        capture_output=True,
        text=True,
        timeout=8,
        check=False,
    )
    if result.returncode:
        raise RuntimeError("Health history query failed")
    rows = []
    for line in result.stdout.splitlines():
        try:
            row = json.loads(line)
            if isinstance(row, dict) and row.get("event") == "OPERATOR_HEALTH_CHECK":
                rows.append(health_record(row))
        except (ValueError, TypeError):
            continue
    return rows[-120:]


def collect(data=DATA):
    now = time.time_ns()
    report = {"schema": 1, "observed_ns": now, "read_only": True, "errors": {}}
    for key, read in (
        ("service", service_status),
        ("status", lambda: status_snapshot(data / "live-status.json")),
        ("database", lambda: database_snapshot(data / "live.sqlite", now)),
        ("audit", lambda: audit_tail(data / "live-audit.jsonl", now)),
        ("operator_health_history", health_history),
    ):
        try:
            report[key] = read()
        except Exception as error:  # noqa: BLE001 - Independent report sections must expose any failed reader.
            report["errors"][key] = type(error).__name__  # Never emit exception text.
    updated = report.get("status", {}).get("updated_ns")
    proof_path = data / "execution-test-pass.json"
    installed_path = data.parent / "prearmed-install.json"
    if proof_path.exists() and installed_path.exists():
        try:
            proof = json.loads(regular(proof_path).read_text())
            installed = json.loads(regular(installed_path).read_text())
            report["execution_test"] = select(
                proof,
                {
                    "state",
                    "mint",
                    "buy_signature",
                    "sell_signature",
                    "residual_tokens_raw",
                    "maximum_total_lamports",
                    "net_wallet_cost_lamports",
                    "verified_ns",
                    "source_sha256",
                },
            )
            report["execution_test"]["matches_installed_image"] = proof.get(
                "image_id"
            ) == installed.get("image_id")
        except (OSError, ValueError, TypeError) as error:
            report["errors"]["execution_test"] = type(error).__name__
    # The worker can publish a newer heartbeat while service/database reads run.
    # Measure its age after collecting it, never against collection-start time.
    observed = time.time_ns()
    report["observed_ns"] = observed
    report["status_age_seconds"] = (
        round((observed - updated) / 1e9, 3) if type(updated) is int else None
    )
    report["limitations"] = [
        "No provider balance/credit queries or blockchain receipt verification.",
        "Realized P&L is the bot database value; individual closed-trade P&L is not stored in this export.",
        "Audit events are bounded and sanitized; no HMAC key is accessed to verify their chain.",
    ]
    return report


if __name__ == "__main__":
    if os.getenv("SSH_ORIGINAL_COMMAND") != "snapshot":
        print(json.dumps({"error": "READ_ONLY_SNAPSHOT_COMMAND_REQUIRED"}))
        raise SystemExit(2)
    print(json.dumps(collect(), separators=(",", ":"), allow_nan=False))
