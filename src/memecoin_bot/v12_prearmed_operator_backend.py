"""Fixed local commands for the authenticated Discord gateway. No remote shell input."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import subprocess
import threading
import time


class ControlBlocked(RuntimeError):
    """A fixed local control reported a safe, operator-readable failure."""

    def __init__(self, step, output, code):
        detail = next(
            (line[9:] for line in reversed(output.splitlines()) if line.startswith("BLOCKED: ")), ""
        )
        if (
            not detail
            or len(detail) > 400
            or "://" in detail
            or any(ord(char) < 32 for char in detail)
        ):
            detail = "The server control did not complete successfully."
        self.public_description = f"{step.replace('-', ' ').title()} was blocked: {detail}"
        self.step, self.exit_code = step, code
        super().__init__(self.public_description)


from pathlib import Path


def module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


class OperatorBackend:
    def __init__(self, stage=Path("/opt/gambit-v12-staging"), runner=subprocess.run):
        self.stage, self.runner = Path(stage), runner
        self.control = self.stage / "prearmed-control.py"
        self._operation_lock = threading.Lock()
        self._generation_lock = threading.Lock()
        self._stop_generation = 0
        self._context = threading.local()

    def status(self):
        report = module(self.stage / "gambit-monitor-collector.py", "operator_collector").collect(
            self.stage / "prearmed-data"
        )
        path = self.stage / "operator-data/health.json"
        if path.exists():
            report["operator_health"] = json.loads(path.read_text())
        worker = self.runner(
            ["docker", "inspect", "--format", "{{.State.Running}}", "gambit-prearmed-live"],
            capture_output=True,
            text=True,
            timeout=8,
        )
        report["execution_worker_running"] = (
            worker.returncode == 0 and worker.stdout.strip() == "true"
        )
        return report

    def trading_expected(self):
        config = module(self.control, "health_configuration").env_read(self.stage / "prearmed.env")
        if "E4_LIVE" not in config or "V12_LIVE_TRADING_ENABLED" not in config:
            raise RuntimeError("TRADING_ENABLE_STATE_UNAVAILABLE")
        return any(
            config.get(name, "").lower() == "true"
            for name in ("E4_LIVE", "V12_LIVE_TRADING_ENABLED")
        )

    def library(self, creator=""):
        from .v12_prearmed_library import PrearmedLibrary, validate_creator_query

        if creator:
            validate_creator_query(creator)
        installed = json.loads((self.stage / "prearmed-install.json").read_text())
        root = Path(installed["release"]).resolve()
        model = json.loads(
            (root / "research/v12-pre-armed-100-trade-frozen-model.json").read_text()
        )
        library = PrearmedLibrary.from_root(root, model)
        report = {"library": library.status()}
        status = self.status()
        active = status.get("status", {}).get("creator_library", {})
        age = status.get("status_age_seconds")
        report["loaded_by_live_bot"] = (
            status.get("service", {}).get("ActiveState") == "active"
            and isinstance(age, (int, float))
            and 0 <= age <= 10
            and active.get("sha256") == library.sha256
            and active.get("state") == "CONNECTED"
        )
        if creator:
            report["creator"] = library.describe(creator)
        return report

    def releases(self):
        directory = self.stage / "staged-releases"
        return (
            sorted(
                p.name
                for p in directory.iterdir()
                if p.is_dir() and not p.is_symlink() and re.fullmatch(r"[a-f0-9]{64}", p.name)
            )
            if directory.exists()
            else []
        )

    def logs(self, hours=24, mint=""):
        if (
            type(hours) is not int
            or not 1 <= hours <= 168
            or (mint and not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", mint))
        ):
            raise ValueError("Use 1â€“168 hours and an optional full token address.")
        collector = module(self.stage / "gambit-monitor-collector.py", "log_export")
        events = []
        matching = 0
        malformed = 0
        cutoff = time.time_ns() - hours * 3_600_000_000_000
        fields = {
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
            "social_age_ms",
            "source_location",
            "remote_error_type",
            "websocket_close_code",
            "creator",
            "creator_seed_lamports",
            "minimum_seed_lamports",
            "blockers",
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
        }
        path = collector.regular(self.stage / "prearmed-data/live-audit.jsonl")
        with path.open() as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                    payload = row.get("payload", {})
                    if (
                        row.get("ts_ns", 0) < cutoff
                        or row.get("event") not in collector.EVENTS
                        or (mint and payload.get("mint") != mint)
                    ):
                        continue
                    matching += 1
                    events.append(
                        {
                            "ts_ns": row["ts_ns"],
                            "event": row["event"],
                            "payload": collector.select(payload, fields),
                        }
                    )
                    if len(events) > 5000:
                        events.pop(0)
                except (ValueError, TypeError):
                    malformed += 1
        return {
            "hours": hours,
            "matching_events": matching,
            "truncated": matching > len(events),
            "malformed_records": malformed,
            "events": events,
            "secrets_redacted": True,
        }

    def command(self, *arguments, timeout=240):
        self._check_cancelled()
        result = self.runner(
            ["/usr/bin/python3", str(self.control), *arguments],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode:
            # Raw child output can contain credentials from upstream exceptions.
            raise ControlBlocked(arguments[0], result.stdout, result.returncode)

    def execute(self, operation):
        # Stop immediately blocks entries, then runs again after any in-flight control
        # finishes. A previously confirmed start may never undo a completed stop.
        stopping = operation["action"] == "stop"
        with self._generation_lock:
            if stopping:
                self._stop_generation += 1
            generation = self._stop_generation
        if stopping:
            self.command("stop")
        with self._operation_lock:
            self._context.generation = None if stopping else generation
            try:
                self._check_cancelled()
                return self._execute(operation)
            finally:
                self._context.generation = None

    def _check_cancelled(self):
        expected = getattr(self._context, "generation", None)
        with self._generation_lock:
            cancelled = expected is not None and expected != self._stop_generation
        if cancelled:
            raise ControlBlocked("operation", "BLOCKED: Cancelled by a newer Stop request.", 1)

    def _execute(self, operation):
        action, payload = operation["action"], operation["payload"]
        if action not in {
            "start",
            "stop",
            "restart",
            "check",
            "configure",
            "update",
            "transfer",
            "live_test",
            "recover",
            "reset_safety",
            "acknowledge",
        }:
            return {"state": "FAILED", "message": "Unsupported operation."}
        if action == "acknowledge":
            self.command("stop")
            report = self.status()
            database = report.get("database", {})
            if (
                report.get("errors")
                or not database.get("available")
                or database.get("positions")
                or database.get("pending_orders")
                or any(
                    database.get("journal_state_counts", {}).get(key, 0)
                    for key in ("SIGNED", "SUBMITTED", "UNCERTAIN")
                )
            ):
                return {
                    "state": "FAILED",
                    "message": "An unresolved wallet state prevents acknowledgement. Use recovery and inspect the receipts first.",
                }
            return {
                "state": "COMPLETE",
                "message": "Unresolved operation reviewed with trading stopped and no open positions or unresolved signed transactions. The original action was not repeated.",
                "acknowledged_operation_id": payload["operation_id"],
                "evidence": report,
            }
        if action == "recover":
            self.command("recover")
            return {
                "state": "COMPLETE",
                "message": "Exit-only recovery requested. Check /bot status for remaining positions and unresolved orders.",
                "evidence": self.status(),
            }
        if action == "reset_safety":
            self.command("stop")
            self.command("reset-safety")
            return {
                "state": "COMPLETE",
                "message": "Safety trip acknowledged. Equity-loss limits remain in force and trading stays stopped.",
                "evidence": self.status(),
            }
        if action in {"start", "restart", "check"}:
            self.command("stop")
            self.command("clear-stop")
            self.command("final-check", "--send-test-alert", timeout=300)
            if action != "check":
                self.command("start-live")
                report = self.status()
                if report.get("service", {}).get("ActiveState") != "active":
                    return {
                        "state": "UNKNOWN",
                        "message": "Start was requested, but an active service is not yet confirmed.",
                        "evidence": report,
                    }
            return {
                "state": "COMPLETE",
                "message": "Technical checks passed."
                + (
                    " Trading start requested; inspect /bot status."
                    if action != "check"
                    else " Trading stays stopped."
                ),
                "evidence": self.status(),
            }
        self.command("stop")
        if action == "stop":
            return {
                "state": "COMPLETE",
                "message": "New entries blocked. The execution worker acknowledged no positions or pending orders before stopping.",
                "evidence": self.status(),
            }
        if action == "configure":
            limits = {
                "position_percent": ("GAMBIT_POSITION_PERCENT", 1, 10),
                "maximum_positions": ("GAMBIT_MAXIMUM_POSITIONS", 1, 2),
            }
            key, value = payload.get("setting"), payload.get("value")
            if (
                key not in limits
                or type(value) is not int
                or not limits[key][1] <= value <= limits[key][2]
            ):
                return {"state": "FAILED", "message": "Unsupported setting or value."}
            control = module(self.control, "operator_configuration")
            values = control.env_read(self.stage / "prearmed.env")
            before = values.get(limits[key][0])
            values[limits[key][0]] = str(value)
            control.env_write(self.stage / "prearmed.env", values)
            change = {
                "operation_id": operation["id"],
                "setting": key,
                "before": before,
                "after": value,
                "ts_ns": time.time_ns(),
            }
            history = self.stage / "operator-data/configuration-history.jsonl"
            history.parent.mkdir(parents=True, exist_ok=True)
            with history.open("a") as stream:
                stream.write(json.dumps(change) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            return {
                "state": "COMPLETE",
                "message": "Risk limit saved. Trading is stopped; /bot start must pass fresh readiness checks.",
                "evidence": change,
            }
        if action == "update":
            release = payload.get("release_id", "")
            if release not in self.releases():
                return {
                    "state": "FAILED",
                    "message": "That immutable release is not staged on this server.",
                }
            directory = self.stage / "staged-releases" / release
            archive = directory / "release.tar.gz"
            installer = directory / "install.py"
            approval = json.loads((directory / "approval.json").read_text())
            if (
                archive.is_symlink()
                or installer.is_symlink()
                or hashlib.sha256(archive.read_bytes()).hexdigest() != release
                or hashlib.sha256(installer.read_bytes()).hexdigest()
                != approval.get("installer_sha256")
            ):
                return {
                    "state": "FAILED",
                    "message": "Staged release integrity check failed. Trading stays stopped.",
                }
            result = self.runner(
                ["/usr/bin/python3", str(installer)], capture_output=True, text=True, timeout=900
            )
            if result.returncode:
                raise RuntimeError("RELEASE_INSTALLATION_FAILED")
            return {
                "state": "COMPLETE",
                "message": "The exact staged release was installed and checked. Trading stays stopped.",
                "evidence": {"release_id": release},
            }
        # Financial commands are submitted to the installed, journaled wallet worker.
        request = self.stage / "operator-data" / (operation["id"] + ".request.json")
        request.write_text(json.dumps(operation))
        os.chmod(request, 0o600)
        self.command("wallet-operation", "--operation-id", operation["id"], timeout=300)
        result = self.stage / "operator-data" / (operation["id"] + ".result.json")
        if not result.is_file():
            return {
                "state": "UNKNOWN",
                "message": "Wallet operation has no final receipt yet. Do not retry; inspect /bot operations.",
            }
        return json.loads(result.read_text())
