from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

from memecoin_bot.historical.backfill import BackfillEngine
from memecoin_bot.historical.dune_pilot import DuneAcquisitionConfig, DunePilotRunner
from memecoin_bot.historical.dune_registry import DuneQueryRegistry
from memecoin_bot.historical.providers import (
    OperationalHistoryProvider,
)
from memecoin_bot.historical.store import HistoricalWarehouse

from .providers import ProviderRegistry


class ConvergenceState(StrEnum):
    BLOCKED_EXTERNAL = "BLOCKED_EXTERNAL"
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    RETRYABLE_FAILURE = "RETRYABLE_FAILURE"
    FAILED_RESEARCH = "FAILED_RESEARCH"
    PASSED_ENGINEERING = "PASSED_ENGINEERING"
    PASSED_RESEARCH = "PASSED_RESEARCH"
    AWAITING_MATURITY = "AWAITING_MATURITY"
    SHADOW = "SHADOW"
    APPROVED_FOR_HUMAN_REVIEW = "APPROVED_FOR_HUMAN_REVIEW"


TERMINAL_FOR_INVOCATION = {
    ConvergenceState.BLOCKED_EXTERNAL,
    ConvergenceState.FAILED_RESEARCH,
    ConvergenceState.PASSED_ENGINEERING,
    ConvergenceState.PASSED_RESEARCH,
    ConvergenceState.AWAITING_MATURITY,
    ConvergenceState.SHADOW,
    ConvergenceState.APPROVED_FOR_HUMAN_REVIEW,
}


@dataclass(frozen=True, slots=True)
class PhaseResult:
    state: ConvergenceState
    evidence: dict[str, Any]
    checkpoint: dict[str, Any] | None = None


PhaseHandler = Callable[[], Awaitable[PhaseResult]]


PHASES = (
    "HISTORICAL_ACQUISITION",
    "PROVIDER_ADMISSION",
    "NORMALIZATION",
    "DATA_QUALITY",
    "OUTCOMES",
    "FEATURE_RESEARCH",
    "RUNNER_AUTOPSY",
    "FALSE_POSITIVE_AUTOPSY",
    "HYPOTHESIS_GENERATION",
    "CHALLENGER_FIT",
    "CHRONOLOGICAL_VALIDATION",
    "SHADOW",
    "DRIFT",
    "AUDIT",
    "REPORT",
)


class ConvergenceOrchestrator:
    """Persistent, shadow-only research scheduler that keeps independent work moving."""

    version = "v15-convergence-v1"

    def __init__(
        self,
        warehouse: HistoricalWarehouse,
        *,
        operational_database: str | Path | None = None,
        code_version: str = "working-tree",
        environment: dict[str, str] | None = None,
        lease_seconds: int = 900,
    ):
        self.warehouse = warehouse
        self.operational_database = Path(operational_database) if operational_database else None
        self.code_version = code_version
        self.environment = environment if environment is not None else dict(os.environ)
        self.lease_seconds = lease_seconds
        self.worker_id = str(uuid.uuid4())
        self.providers = ProviderRegistry(warehouse, self.environment)

    async def run(
        self,
        *,
        run_id: str | None = None,
        phases: set[str] | None = None,
        live_probes: bool = True,
    ) -> dict[str, Any]:
        run_id = self._resume_or_create(run_id)
        self._recover_expired_leases(run_id)
        handlers = self._handlers(live_probes)
        for phase_name in PHASES:
            if phases and phase_name not in phases:
                continue
            if not self._claim(run_id, phase_name):
                continue
            try:
                result = await handlers[phase_name]()
            except (TimeoutError, sqlite3.Error, OSError, ValueError, RuntimeError) as error:
                self._fail_phase(run_id, phase_name, error)
                continue
            self._finish_phase(run_id, phase_name, result)
        return self._finish_run(run_id)

    def _handlers(self, live_probes: bool) -> dict[str, PhaseHandler]:
        return {
            "HISTORICAL_ACQUISITION": self._historical_acquisition,
            "PROVIDER_ADMISSION": lambda: self._provider_admission(live_probes),
            "NORMALIZATION": self._normalization,
            "DATA_QUALITY": self._data_quality,
            "OUTCOMES": self._outcomes,
            "FEATURE_RESEARCH": self._feature_research,
            "RUNNER_AUTOPSY": self._runner_autopsy,
            "FALSE_POSITIVE_AUTOPSY": self._false_positive_autopsy,
            "HYPOTHESIS_GENERATION": self._hypothesis_generation,
            "CHALLENGER_FIT": self._challenger_fit,
            "CHRONOLOGICAL_VALIDATION": self._chronological_validation,
            "SHADOW": self._shadow,
            "DRIFT": self._drift,
            "AUDIT": self._audit,
            "REPORT": self._report,
        }

    def _resume_or_create(self, requested: str | None) -> str:
        if requested:
            row = self.warehouse.conn.execute(
                "SELECT run_id FROM convergence_runs WHERE run_id=?", (requested,)
            ).fetchone()
            if row:
                return str(row[0])
        row = self.warehouse.conn.execute(
            "SELECT run_id FROM convergence_runs WHERE completed_at IS NULL "
            "ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        if row and not requested:
            return str(row[0])
        run_id = requested or str(uuid.uuid4())
        now = _now()
        config = {
            "operational_database_configured": bool(self.operational_database),
            "dune_configured": bool(self.environment.get("DUNE_API_KEY")),
            "public_route": False,
        }
        with self.warehouse._lock, self.warehouse.conn:
            self.warehouse.conn.execute(
                "INSERT INTO convergence_runs(run_id,orchestration_version,code_version,state,"
                "public_route,started_at,configuration_json) VALUES(?,?,?,'RUNNING',0,?,?)",
                (run_id, self.version, self.code_version, now, _json(config)),
            )
            for ordinal, phase_name in enumerate(PHASES):
                self.warehouse.conn.execute(
                    "INSERT INTO convergence_phases(run_id,phase_name,ordinal,state,dependency_json) "
                    "VALUES(?,?,?,'PENDING',?)",
                    (run_id, phase_name, ordinal, _json(self._dependencies(phase_name))),
                )
        return run_id

    @staticmethod
    def _dependencies(phase_name: str) -> list[str]:
        dependencies = {
            "NORMALIZATION": ["HISTORICAL_ACQUISITION"],
            "DATA_QUALITY": ["NORMALIZATION"],
            "OUTCOMES": ["DATA_QUALITY"],
            "FEATURE_RESEARCH": ["DATA_QUALITY"],
            "RUNNER_AUTOPSY": ["OUTCOMES"],
            "FALSE_POSITIVE_AUTOPSY": ["OUTCOMES"],
            "HYPOTHESIS_GENERATION": ["RUNNER_AUTOPSY", "FALSE_POSITIVE_AUTOPSY"],
            "CHALLENGER_FIT": ["HYPOTHESIS_GENERATION"],
            "CHRONOLOGICAL_VALIDATION": ["CHALLENGER_FIT"],
            "SHADOW": ["CHRONOLOGICAL_VALIDATION"],
            "DRIFT": ["SHADOW"],
        }
        return dependencies.get(phase_name, [])

    def _recover_expired_leases(self, run_id: str) -> None:
        now = _now()
        with self.warehouse._lock, self.warehouse.conn:
            self.warehouse.conn.execute(
                "UPDATE convergence_phases SET state='RETRYABLE_FAILURE',lease_owner=NULL,"
                "lease_expires_at=NULL,last_error='expired worker lease recovered',next_retry_at=? "
                "WHERE run_id=? AND state='RUNNING' AND lease_expires_at<?",
                (now, run_id, now),
            )

    def _claim(self, run_id: str, phase_name: str) -> bool:
        now = datetime.now(UTC)
        retryable = {
            ConvergenceState.PENDING,
            ConvergenceState.RETRYABLE_FAILURE,
            ConvergenceState.BLOCKED_EXTERNAL,
            ConvergenceState.AWAITING_MATURITY,
            ConvergenceState.SHADOW,
        }
        with self.warehouse._lock, self.warehouse.conn:
            row = self.warehouse.conn.execute(
                "SELECT state,attempt,maximum_attempts,next_retry_at FROM convergence_phases "
                "WHERE run_id=? AND phase_name=?",
                (run_id, phase_name),
            ).fetchone()
            if not row or ConvergenceState(str(row["state"])) not in retryable:
                return False
            if row["next_retry_at"] and _time(row["next_retry_at"]) > now:
                return False
            if int(row["attempt"]) >= int(row["maximum_attempts"]):
                return False
            cursor = self.warehouse.conn.execute(
                "UPDATE convergence_phases SET state='RUNNING',attempt=attempt+1,lease_owner=?,"
                "lease_expires_at=?,started_at=COALESCE(started_at,?),last_error=NULL WHERE run_id=? "
                "AND phase_name=? AND state=?",
                (
                    self.worker_id,
                    (now + timedelta(seconds=self.lease_seconds)).isoformat(),
                    now.isoformat(),
                    run_id,
                    phase_name,
                    row["state"],
                ),
            )
        return cursor.rowcount == 1

    def _finish_phase(self, run_id: str, phase_name: str, result: PhaseResult) -> None:
        with self.warehouse._lock, self.warehouse.conn:
            self.warehouse.conn.execute(
                "UPDATE convergence_phases SET state=?,checkpoint_json=?,evidence_json=?,"
                "lease_owner=NULL,lease_expires_at=NULL,next_retry_at=NULL,completed_at=? "
                "WHERE run_id=? AND phase_name=? AND lease_owner=?",
                (
                    str(result.state),
                    _json(result.checkpoint or {}),
                    _json(result.evidence),
                    _now(),
                    run_id,
                    phase_name,
                    self.worker_id,
                ),
            )

    def _fail_phase(self, run_id: str, phase_name: str, error: Exception) -> None:
        row = self.warehouse.conn.execute(
            "SELECT attempt,maximum_attempts FROM convergence_phases WHERE run_id=? AND phase_name=?",
            (run_id, phase_name),
        ).fetchone()
        exhausted = bool(row and int(row["attempt"]) >= int(row["maximum_attempts"]))
        state = (
            ConvergenceState.BLOCKED_EXTERNAL if exhausted else ConvergenceState.RETRYABLE_FAILURE
        )
        retry = None if exhausted else (datetime.now(UTC) + timedelta(seconds=30)).isoformat()
        with self.warehouse._lock, self.warehouse.conn:
            self.warehouse.conn.execute(
                "UPDATE convergence_phases SET state=?,lease_owner=NULL,lease_expires_at=NULL,"
                "next_retry_at=?,completed_at=?,last_error=? WHERE run_id=? AND phase_name=?",
                (
                    str(state),
                    retry,
                    _now(),
                    f"{type(error).__name__}: {error}"[:500],
                    run_id,
                    phase_name,
                ),
            )

    def _finish_run(self, run_id: str) -> dict[str, Any]:
        phases = [
            dict(row)
            for row in self.warehouse.conn.execute(
                "SELECT phase_name,state,attempt,evidence_json,last_error,completed_at "
                "FROM convergence_phases WHERE run_id=? ORDER BY ordinal",
                (run_id,),
            )
        ]
        states = {ConvergenceState(row["state"]) for row in phases}
        if ConvergenceState.RUNNING in states or ConvergenceState.PENDING in states:
            state = ConvergenceState.RUNNING
        elif ConvergenceState.RETRYABLE_FAILURE in states:
            state = ConvergenceState.RETRYABLE_FAILURE
        elif ConvergenceState.BLOCKED_EXTERNAL in states:
            state = ConvergenceState.BLOCKED_EXTERNAL
        elif ConvergenceState.FAILED_RESEARCH in states:
            state = ConvergenceState.FAILED_RESEARCH
        elif ConvergenceState.AWAITING_MATURITY in states or ConvergenceState.SHADOW in states:
            state = ConvergenceState.AWAITING_MATURITY
        else:
            state = ConvergenceState.PASSED_ENGINEERING
        summary = {
            "phase_states": {row["phase_name"]: row["state"] for row in phases},
            "engineering_production_ready": self._engineering_ready(phases),
            "intelligence_production_ready": False,
            "public_production_ready": False,
        }
        # A convergence cycle is immutable once every phase reached an evidence state.
        # The next scheduled invocation creates a fresh cycle and can re-evaluate
        # BLOCKED_EXTERNAL, FAILED_RESEARCH, and AWAITING_MATURITY after inputs change.
        complete = state not in {
            ConvergenceState.RUNNING,
            ConvergenceState.RETRYABLE_FAILURE,
        }
        with self.warehouse._lock, self.warehouse.conn:
            self.warehouse.conn.execute(
                "UPDATE convergence_runs SET state=?,summary_json=?,completed_at=? WHERE run_id=?",
                (str(state), _json(summary), _now() if complete else None, run_id),
            )
        return {"run_id": run_id, "state": str(state), **summary, "phases": phases}

    @staticmethod
    def _engineering_ready(phases: list[dict[str, Any]]) -> bool:
        required = {"PROVIDER_ADMISSION", "DATA_QUALITY", "AUDIT"}
        passing = {
            row["phase_name"]
            for row in phases
            if row["state"] in {"PASSED_ENGINEERING", "PASSED_RESEARCH", "SHADOW"}
        }
        return required <= passing

    async def _historical_acquisition(self) -> PhaseResult:
        acquired: dict[str, Any] = {}
        blockers = []
        target_months: list[str] = []
        existing = self.warehouse.coverage_manifest()
        if existing:
            acquired["existing_local_datasets"] = {
                "datasets": len(existing),
                "point_in_time_safe": sum(bool(row["point_in_time_safe"]) for row in existing),
                "normalized_events": self._count("normalized_events"),
                "outcomes": self._count("outcomes"),
            }
        if self.operational_database and self.operational_database.exists():
            provider = OperationalHistoryProvider(self.operational_database)
            self._register_operational_dataset(provider.dataset_id)
            acquired["operational"] = await BackfillEngine(self.warehouse).run(provider)
        else:
            blockers.append("read-only production DATABASE_PATH copy is absent")
        key = self.environment.get("DUNE_API_KEY")
        if key:
            registry = DuneQueryRegistry()
            registry.register(self.warehouse)
            config = DuneAcquisitionConfig.from_environment(self.environment)
            controlled = DunePilotRunner(
                self.warehouse,
                key,
                config,
                registry=registry,
            )
            acquired["dune_controlled"] = await controlled.run(execute=not config.dry_run)
            target_months = controlled.plan()["months"]
            if not config.start_month:
                blockers.append("explicit DUNE_START_MONTH and DUNE_END_MONTH are required")
            elif config.dry_run:
                blockers.append("DUNE_DRY_RUN is enabled; no historical query was executed")
        else:
            blockers.append("DUNE_API_KEY is not configured")
        raw = self._count("raw_evidence")
        state = (
            ConvergenceState.PASSED_ENGINEERING
            if raw or existing
            else ConvergenceState.BLOCKED_EXTERNAL
        )
        return PhaseResult(
            state,
            {
                "acquired": acquired,
                "blockers": blockers,
                "raw_evidence_rows": raw,
                "target_months": target_months,
                "raw_corpora_committed_to_git": False,
            },
        )

    async def _provider_admission(self, live_probes: bool) -> PhaseResult:
        preflight = self.providers.refresh()
        probes = await self.providers.probe() if live_probes else []
        live = [row for row in probes if row["state"] not in {"BLOCKED_EXTERNAL", "REJECTED"}]
        state = (
            ConvergenceState.PASSED_ENGINEERING
            if live or not live_probes
            else ConvergenceState.BLOCKED_EXTERNAL
        )
        return PhaseResult(
            state,
            {
                "credential_preflight": preflight,
                "live_probes": probes,
                "zero_events_are_not_pass": True,
                "optional_provider_absence_did_not_stop_keyless_work": True,
            },
        )

    async def _normalization(self) -> PhaseResult:
        raw = self._count("raw_evidence")
        normalized = self._count("normalized_events")
        state = (
            ConvergenceState.PASSED_ENGINEERING if normalized else ConvergenceState.BLOCKED_EXTERNAL
        )
        return PhaseResult(
            state,
            {
                "raw_evidence": raw,
                "normalized_events": normalized,
                "reason": None
                if normalized
                else "no schema-reviewed raw evidence is available to normalize",
            },
        )

    async def _data_quality(self) -> PhaseResult:
        integrity = self.warehouse.conn.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = list(self.warehouse.conn.execute("PRAGMA foreign_key_check"))
        datasets = self.warehouse.coverage_manifest()
        pit_unsafe = [row["dataset_id"] for row in datasets if not row["point_in_time_safe"]]
        pit_safe = [row["dataset_id"] for row in datasets if row["point_in_time_safe"]]
        state = (
            ConvergenceState.PASSED_ENGINEERING
            if integrity == "ok" and not foreign_keys and (not datasets or pit_safe)
            else ConvergenceState.RETRYABLE_FAILURE
        )
        return PhaseResult(
            state,
            {
                "integrity": integrity,
                "foreign_key_violations": len(foreign_keys),
                "datasets": len(datasets),
                "pit_safe_datasets": pit_safe,
                "pit_unsafe_datasets": pit_unsafe,
                "pit_unsafe_policy": "context-only; excluded from unbiased training and evaluation",
                "wal_mode": self.warehouse.conn.execute("PRAGMA journal_mode").fetchone()[0],
            },
        )

    async def _outcomes(self) -> PhaseResult:
        outcomes = self._count("outcomes")
        actionable = int(
            self.warehouse.conn.execute(
                "SELECT COUNT(*) FROM outcomes WHERE class_name LIKE 'ACTIONABLE%'"
            ).fetchone()[0]
        )
        state = (
            ConvergenceState.PASSED_RESEARCH
            if outcomes >= 250 and actionable
            else ConvergenceState.AWAITING_MATURITY
        )
        return PhaseResult(
            state,
            {
                "outcomes": outcomes,
                "actionable_outcomes": actionable,
                "approval_threshold_is_not_relaxed": True,
            },
        )

    async def _feature_research(self) -> PhaseResult:
        sources = self.warehouse.research_source_report()
        decisions = self._count("research_decisions")
        approved = int(
            self.warehouse.conn.execute(
                "SELECT COUNT(*) FROM research_decisions WHERE approval_state='APPROVED'"
            ).fetchone()[0]
        )
        state = (
            ConvergenceState.PASSED_RESEARCH
            if sources["gate_passed"]
            else ConvergenceState.FAILED_RESEARCH
        )
        return PhaseResult(
            state,
            {
                "source_gate": sources,
                "decisions": decisions,
                "approved_features": approved,
                "automatic_approval": False,
            },
        )

    async def _runner_autopsy(self) -> PhaseResult:
        runners = int(
            self.warehouse.conn.execute(
                "SELECT COUNT(*) FROM outcomes WHERE peak_multiple>=5"
            ).fetchone()[0]
        )
        return PhaseResult(
            ConvergenceState.PASSED_RESEARCH
            if runners >= 100
            else ConvergenceState.FAILED_RESEARCH,
            {"5x_runner_cohort": runners, "minimum_research_sample": 100},
        )

    async def _false_positive_autopsy(self) -> PhaseResult:
        failures = int(
            self.warehouse.conn.execute(
                "SELECT COUNT(*) FROM outcomes WHERE rugged=1 OR peak_multiple<1"
            ).fetchone()[0]
        )
        return PhaseResult(
            ConvergenceState.PASSED_RESEARCH
            if failures >= 100
            else ConvergenceState.FAILED_RESEARCH,
            {"failure_cohort": failures, "minimum_research_sample": 100},
        )

    async def _hypothesis_generation(self) -> PhaseResult:
        findings = self._count("research_findings")
        return PhaseResult(
            ConvergenceState.PASSED_RESEARCH if findings else ConvergenceState.FAILED_RESEARCH,
            {"machine_answerable_findings": findings, "test_set_optimization_allowed": False},
        )

    async def _challenger_fit(self) -> PhaseResult:
        runs = self._count("research_runs")
        return PhaseResult(
            ConvergenceState.PASSED_RESEARCH if runs else ConvergenceState.FAILED_RESEARCH,
            {"research_runs": runs, "public_route": False},
        )

    async def _chronological_validation(self) -> PhaseResult:
        retired = self._count("retired_holdouts_v15")
        approved = int(
            self.warehouse.conn.execute(
                "SELECT COUNT(*) FROM research_decisions WHERE approval_state='APPROVED'"
            ).fetchone()[0]
        )
        return PhaseResult(
            ConvergenceState.APPROVED_FOR_HUMAN_REVIEW
            if approved
            else ConvergenceState.FAILED_RESEARCH,
            {
                "approved_challenger_decisions": approved,
                "retired_holdouts": retired,
                "outer_test_reuse_forbidden": True,
            },
        )

    async def _shadow(self) -> PhaseResult:
        decisions = self._count("shadow_decisions")
        return PhaseResult(
            ConvergenceState.SHADOW if decisions else ConvergenceState.AWAITING_MATURITY,
            {"immutable_shadow_decisions": decisions, "public_route": False},
        )

    async def _drift(self) -> PhaseResult:
        observations = self._count("drift_observations")
        return PhaseResult(
            ConvergenceState.PASSED_RESEARCH
            if observations
            else ConvergenceState.AWAITING_MATURITY,
            {"drift_observations": observations},
        )

    async def _audit(self) -> PhaseResult:
        integrity = self.warehouse.conn.execute("PRAGMA integrity_check").fetchone()[0]
        findings = self._count("audit_findings_v15")
        unresolved_high = int(
            self.warehouse.conn.execute(
                "SELECT COUNT(*) FROM audit_findings_v15 WHERE severity IN ('CRITICAL','HIGH') "
                "AND status NOT IN ('FIXED','PASS','BLOCKED_EXTERNAL','ACCEPTED_LIMITATION') "
                "AND audit_run_id=(SELECT audit_run_id FROM audit_findings_v15 "
                "ORDER BY recorded_at DESC LIMIT 1)"
            ).fetchone()[0]
        )
        return PhaseResult(
            ConvergenceState.PASSED_ENGINEERING
            if integrity == "ok" and not unresolved_high
            else ConvergenceState.RETRYABLE_FAILURE,
            {
                "db_integrity": integrity,
                "recorded_findings": findings,
                "unresolved_high_or_critical": unresolved_high,
                "full_validation_is_a_release_gate": True,
            },
        )

    async def _report(self) -> PhaseResult:
        report = self.daily_report()
        return PhaseResult(ConvergenceState.PASSED_ENGINEERING, report)

    def daily_report(self) -> dict[str, Any]:
        today = datetime.now(UTC).date().isoformat()
        current = self.status()
        report = {
            "date": today,
            "data_acquired": self._count("raw_evidence"),
            "normalized_events": self._count("normalized_events"),
            "shadow_calls": self._count("shadow_decisions"),
            "matured_outcomes": self._count("outcomes"),
            "research_findings": self._count("research_findings"),
            "drift_observations": self._count("drift_observations"),
            "provider_health": self.providers.status(),
            "convergence": current,
            "public_route": False,
        }
        with self.warehouse._lock, self.warehouse.conn:
            self.warehouse.conn.execute(
                "INSERT INTO daily_convergence_reports_v15(report_date,run_id,generated_at,"
                "report_json,public_route) VALUES(?,?,?,?,0) ON CONFLICT(report_date) DO UPDATE SET "
                "run_id=excluded.run_id,generated_at=excluded.generated_at,report_json=excluded.report_json",
                (today, current.get("run_id"), _now(), _json(report)),
            )
        return report

    def status(self) -> dict[str, Any]:
        run = self.warehouse.conn.execute(
            "SELECT * FROM convergence_runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        if not run:
            return {"state": "NOT_STARTED", "public_production_ready": False}
        phases = [
            dict(row)
            for row in self.warehouse.conn.execute(
                "SELECT phase_name,state,attempt,maximum_attempts,last_error,completed_at "
                "FROM convergence_phases WHERE run_id=? ORDER BY ordinal",
                (run["run_id"],),
            )
        ]
        return {
            "run_id": run["run_id"],
            "state": run["state"],
            "code_version": run["code_version"],
            "phases": phases,
            "public_production_ready": False,
        }

    def historical_status(self) -> dict[str, Any]:
        datasets = self.warehouse.coverage_manifest()
        by_month = {
            month: {"state": "MISSING", "tokens": 0, "events": 0}
            for month in historical_months("2024-01", "2026-08")
        }
        for row in datasets:
            earliest = row.get("earliest_timestamp")
            latest = row.get("latest_timestamp")
            if not earliest or not latest:
                continue
            for month, item in by_month.items():
                if earliest[:7] <= month <= latest[:7]:
                    item["state"] = "PARTIAL_OR_FULL_REVIEW_REQUIRED"
                    item["tokens"] += int(row.get("entity_count") or 0)
                    item["events"] += int(row.get("observation_count") or 0)
        return {"months": by_month, "datasets": datasets, "required_months": 24}

    def champion_status(self) -> dict[str, Any]:
        row = self.warehouse.conn.execute(
            "SELECT research_run_id,result_json,metrics_json,created_at FROM research_runs "
            "ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        return {
            "champion": "CONTROL_V15",
            "latest_research": dict(row) if row else None,
            "approved_challenger": False,
            "public_route": False,
        }

    def _register_operational_dataset(self, dataset_id: str) -> None:
        self.warehouse.register_dataset(
            {
                "dataset_id": dataset_id,
                "dataset_version": "v1.5-convergence-operational-v1",
                "provider": "gambit_jr_operational_store",
                "chain": "multi",
                "acquisition_method": "read_only_allowlisted_sqlite_transfer",
                "refresh_method": "checkpointed_table_rowid",
                "timestamp_precision": "original operational timestamp",
                "reliability": "FIRST_PARTY_OBSERVED",
                "history_kind": "TRUE_HISTORICAL",
                "point_in_time_safe": True,
                "estimated_completeness": None,
                "missing_ranges_json": ["before first operational observation"],
                "cost_json": {"monthly_usd": 0},
            }
        )

    def _register_dune_dataset(
        self,
        dataset_id: str,
        month: str,
        query_name: str,
        schema_version: str,
        query_id: int | None,
    ) -> None:
        self.warehouse.register_dataset(
            {
                "dataset_id": dataset_id,
                "dataset_version": f"dune-{query_name}-{schema_version}-{month}",
                "provider": "dune_month_partition",
                "chain": "solana",
                "acquisition_method": (
                    "repository_owned_direct_sql"
                    if query_id is None
                    else "repository_owned_direct_sql_with_saved_query_fallback"
                ),
                "refresh_method": "immutable_month_partition_execution",
                "timestamp_precision": "query-defined chain block time",
                "reliability": "INDEXED_ONCHAIN_QUERY",
                "history_kind": "TRUE_HISTORICAL",
                "point_in_time_safe": True,
                "estimated_completeness": None,
                "missing_ranges_json": [],
                "rate_limit_json": {"free_execute_rpm": 15, "free_result_rpm": 40},
                "cost_json": {"free_credit_metered": True},
            }
        )

    def _count(self, table: str) -> int:
        allowed = {
            "raw_evidence",
            "normalized_events",
            "outcomes",
            "research_decisions",
            "research_findings",
            "research_runs",
            "shadow_decisions",
            "drift_observations",
            "retired_holdouts_v15",
            "audit_findings_v15",
        }
        if table not in allowed:
            raise ValueError(f"unsupported count table: {table}")
        return int(self.warehouse.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def historical_months(start: str, end: str) -> list[str]:
    current = datetime.strptime(start, "%Y-%m").replace(tzinfo=UTC)
    finish = datetime.strptime(end, "%Y-%m").replace(tzinfo=UTC)
    output = []
    while current <= finish:
        output.append(current.strftime("%Y-%m"))
        current = (
            current.replace(year=current.year + 1, month=1)
            if current.month == 12
            else current.replace(month=current.month + 1)
        )
    return output


def artifact_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("convergence timestamp must include timezone")
    return parsed.astimezone(UTC)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, default=str, separators=(",", ":"), sort_keys=True)
