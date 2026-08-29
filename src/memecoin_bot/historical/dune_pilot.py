from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiohttp

from .backfill import BackfillEngine
from .dune_registry import DuneQueryRegistry
from .providers import DuneMonthHistoricalProvider

DEFAULT_PILOT_QUERIES = (
    "monthly_universe",
    "pumpfun_launches",
    "pumpfun_trades",
    "outcome_reconstruction",
)
_SOLANA_ADDRESS = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
_SOLANA_SIGNATURE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,100}$")


@dataclass(frozen=True, slots=True)
class DuneAcquisitionConfig:
    start_month: str | None = None
    end_month: str | None = None
    maximum_executions: int = 0
    dry_run: bool = True
    query_names: tuple[str, ...] = DEFAULT_PILOT_QUERIES
    parquet_root: Path = Path("data/historical/parquet")
    pilot_sample_rows: int = 10_000

    @classmethod
    def from_environment(cls, environment: dict[str, str] | None = None) -> DuneAcquisitionConfig:
        values = environment if environment is not None else os.environ
        names = tuple(
            dict.fromkeys(
                value.strip()
                for value in values.get("DUNE_QUERY_NAMES", ",".join(DEFAULT_PILOT_QUERIES)).split(",")
                if value.strip()
            )
        )
        config = cls(
            start_month=values.get("DUNE_START_MONTH") or None,
            end_month=values.get("DUNE_END_MONTH") or None,
            maximum_executions=int(values.get("DUNE_MAX_EXECUTIONS", "0")),
            dry_run=values.get("DUNE_DRY_RUN", "true").strip().lower()
            in {"1", "true", "yes", "on"},
            query_names=names,
            parquet_root=Path(values.get("DUNE_PARQUET_ROOT", "data/historical/parquet")),
            pilot_sample_rows=int(values.get("DUNE_PILOT_SAMPLE_ROWS", "10000")),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if bool(self.start_month) != bool(self.end_month):
            raise ValueError("DUNE_START_MONTH and DUNE_END_MONTH must be configured together")
        if self.start_month and self.end_month:
            start = _month(self.start_month)
            end = _month(self.end_month)
            if start > end:
                raise ValueError("DUNE_START_MONTH cannot follow DUNE_END_MONTH")
            current = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            if end >= current:
                raise ValueError("Dune acquisition range must contain complete months only")
        if self.maximum_executions < 0:
            raise ValueError("DUNE_MAX_EXECUTIONS cannot be negative")
        if self.pilot_sample_rows < 0:
            raise ValueError("DUNE_PILOT_SAMPLE_ROWS cannot be negative")
        if not self.query_names:
            raise ValueError("DUNE_QUERY_NAMES cannot be empty")


@dataclass(frozen=True, slots=True)
class DunePlanPartition:
    month: str
    query_name: str
    schema_version: str
    state: str
    reason: str | None = None
    execution_id: str | None = None


def build_dune_plan(
    warehouse: Any,
    config: DuneAcquisitionConfig,
    *,
    registry: DuneQueryRegistry | None = None,
    force: bool = False,
) -> dict[str, Any]:
    registry = registry or DuneQueryRegistry()
    for name in config.query_names:
        registry.spec(name)
    months = _months(config.start_month, config.end_month)
    partitions = []
    minimum_skips = []
    completed = []
    for month in months:
        for query_name in config.query_names:
            spec = registry.spec(query_name)
            if month < spec.minimum_date[:7]:
                item = DunePlanPartition(
                    month,
                    query_name,
                    spec.schema_version,
                    "MINIMUM_DATE_SKIP",
                    f"query minimum date is {spec.minimum_date}",
                )
                minimum_skips.append(asdict(item))
            else:
                existing = warehouse.conn.execute(
                    "SELECT state,execution_id FROM dune_partition_state_v15 WHERE query_name=? "
                    "AND schema_version=? AND partition_year=? AND partition_month=?",
                    (query_name, spec.schema_version, int(month[:4]), int(month[5:7])),
                ).fetchone()
                completed_for_requested_mode = existing and (
                    existing["state"] == "COMPLETE"
                    or (
                        existing["state"] == "PILOT_SAMPLE_COMPLETE"
                        and config.pilot_sample_rows > 0
                    )
                )
                if completed_for_requested_mode and not force:
                    item = DunePlanPartition(
                        month,
                        query_name,
                        spec.schema_version,
                        "COMPLETED_SKIP",
                        (
                            "bounded pilot sample already completed"
                            if existing["state"] == "PILOT_SAMPLE_COMPLETE"
                            else "immutable partition already completed"
                        ),
                        existing["execution_id"],
                    )
                    completed.append(asdict(item))
                else:
                    item = DunePlanPartition(month, query_name, spec.schema_version, "PLANNED")
            partitions.append(asdict(item))
    planned = [item for item in partitions if item["state"] == "PLANNED"]
    return {
        "dry_run": True,
        "explicit_range_configured": bool(months),
        "months": months,
        "query_names": list(config.query_names),
        "estimated_partitions": len(months) * len(config.query_names),
        "planned_executions": len(planned),
        "maximum_executions": config.maximum_executions,
        "pilot_sample_rows": config.pilot_sample_rows,
        "minimum_date_skips": minimum_skips,
        "existing_completed_partitions": completed,
        "remaining_partitions": planned,
        "execution_performed": False,
        "reason": None if months else "EXPLICIT_DUNE_MONTH_RANGE_REQUIRED",
    }


class DunePilotRunner:
    def __init__(
        self,
        warehouse: Any,
        api_key: str | None,
        config: DuneAcquisitionConfig,
        *,
        registry: DuneQueryRegistry | None = None,
        provider_factory: Any = DuneMonthHistoricalProvider,
    ):
        self.warehouse = warehouse
        self.api_key = api_key
        self.config = config
        self.registry = registry or DuneQueryRegistry()
        self.provider_factory = provider_factory

    def plan(self, *, force: bool = False) -> dict[str, Any]:
        return build_dune_plan(self.warehouse, self.config, registry=self.registry, force=force)

    async def run(self, *, execute: bool = False, force: bool = False) -> dict[str, Any]:
        plan = self.plan(force=force)
        if not execute or self.config.dry_run:
            return plan
        if not self.api_key:
            raise ValueError("DUNE_API_KEY is required to execute a pilot")
        if not plan["explicit_range_configured"]:
            raise ValueError("an explicit complete-month Dune range is required")
        if self.config.maximum_executions <= 0:
            raise ValueError("DUNE_MAX_EXECUTIONS must be positive for execution")
        selected = plan["remaining_partitions"][: self.config.maximum_executions]
        pilot_id = str(uuid.uuid4())
        started = _now()
        with self.warehouse._lock, self.warehouse.conn:
            self.warehouse.conn.execute(
                "INSERT INTO dune_pilot_runs_v15(pilot_id,started_at,start_month,end_month,"
                "query_names_json,maximum_executions,state) VALUES(?,?,?,?,?,?,'RUNNING')",
                (
                    pilot_id,
                    started,
                    self.config.start_month,
                    self.config.end_month,
                    json.dumps(self.config.query_names, separators=(",", ":")),
                    self.config.maximum_executions,
                ),
            )
        results = []
        for item in selected:
            result = await self._run_partition(pilot_id, item)
            results.append(result)
        failures = [item for item in results if item["state"] != "COMPLETE"]
        capped = max(0, len(plan["remaining_partitions"]) - len(selected))
        state = "COMPLETE" if not failures and not capped else "PARTIAL" if not failures else "FAILED"
        summary = {
            "pilot_id": pilot_id,
            "state": state,
            "start_month": self.config.start_month,
            "end_month": self.config.end_month,
            "query_names": list(self.config.query_names),
            "executions_started": sum(
                int(item.get("execution_started", False)) for item in results
            ),
            "partitions_attempted": len(selected),
            "execution_cap": self.config.maximum_executions,
            "partitions_remaining_after_cap": capped,
            "partitions": results,
            "total_rows": sum(item["row_count"] for item in results),
            "total_source_rows": sum(item["source_total_rows"] for item in results),
            "total_output_bytes": sum(item["output_bytes"] for item in results),
            "total_source_result_bytes": sum(
                item["source_result_bytes"] for item in results
            ),
            "sampled_partitions": sum(
                int(item["materialization_mode"] == "BOUNDED_SERVER_SAMPLE")
                for item in results
            ),
            "credits_used": (
                sum(item["credits_used"] for item in results if item["credits_used"] is not None)
                if any(item["credits_used"] is not None for item in results)
                else None
            ),
            "schema_validation": "PASS"
            if results and all(item["schema_state"] == "PASS" for item in results)
            else "FAIL",
            "semantic_validation": "PASS"
            if results and all(item["semantic_state"] == "PASS" for item in results)
            else "FAIL",
            "full_backfill_started": False,
        }
        with self.warehouse._lock, self.warehouse.conn:
            self.warehouse.conn.execute(
                "UPDATE dune_pilot_runs_v15 SET completed_at=?,executions_started=?,state=?,"
                "summary_json=? WHERE pilot_id=?",
                (_now(), summary["executions_started"], state, _json(summary), pilot_id),
            )
        return summary

    async def _run_partition(self, pilot_id: str, item: dict[str, Any]) -> dict[str, Any]:
        query_name = item["query_name"]
        month = item["month"]
        spec = self.registry.spec(query_name)
        provider = self.provider_factory(
            None,
            month,
            self.api_key,
            query_name=query_name,
            registry=self.registry,
            parquet_root=self.config.parquet_root,
            materialize_raw_records=False,
            page_size=32_000,
            maximum_result_rows=self.config.pilot_sample_rows or None,
        )
        _register_dataset(self.warehouse, provider, month, query_name)
        job_id = (
            f"dune:{query_name}:{spec.schema_version}:{spec.sql_sha256[:12]}:{month}"
        )
        prior_job = self.warehouse.backfill_status(job_id) or {}
        resumed_execution = bool(prior_job.get("cursor_json"))
        try:
            status = await BackfillEngine(self.warehouse, max_retries=2).run(
                provider, job_id=job_id
            )
            semantic = validate_dune_partition(
                self.warehouse,
                query_name,
                month,
                provider.dataset_id,
                self.config.parquet_root,
            )
            partition = self.warehouse.conn.execute(
                "SELECT execution_id,row_count FROM dune_partition_state_v15 WHERE query_name=? "
                "AND schema_version=? AND partition_year=? AND partition_month=?",
                (query_name, spec.schema_version, int(month[:4]), int(month[5:7])),
            ).fetchone()
            execution_id = partition["execution_id"] if partition else None
            row_count = int(partition["row_count"] if partition else status["records_ingested"])
            output_bytes = int(semantic["output_bytes"])
            credits = _execution_credits(getattr(provider, "last_execution_metadata", {}))
            result_metadata = getattr(provider, "last_result_metadata", {})
            source_total_rows = int(result_metadata.get("source_total_rows") or row_count)
            source_result_bytes = int(result_metadata.get("source_result_bytes") or 0)
            materialization_mode = str(
                result_metadata.get("materialization_mode") or "FULL_RESULT"
            )
            state = "COMPLETE" if semantic["state"] == "PASS" else "SEMANTIC_FAILED"
            partition_state = (
                "PILOT_SAMPLE_COMPLETE"
                if materialization_mode == "BOUNDED_SERVER_SAMPLE" and state == "COMPLETE"
                else state
            )
            result = {
                "query_name": query_name,
                "month": month,
                "execution_id": execution_id,
                "row_count": row_count,
                "source_total_rows": source_total_rows,
                "source_result_bytes": source_result_bytes,
                "materialization_mode": materialization_mode,
                "output_bytes": output_bytes,
                "credits_used": credits,
                "execution_started": not resumed_execution,
                "schema_state": "PASS",
                "semantic_state": semantic["state"],
                "semantic": semantic,
                "state": state,
            }
            self._persist_partition(pilot_id, spec.schema_version, result)
            with self.warehouse._lock, self.warehouse.conn:
                self.warehouse.conn.execute(
                    "UPDATE dune_partition_state_v15 SET output_bytes=?,credits_used=?,"
                    "semantic_state=?,semantic_json=?,state=?,updated_at=? WHERE query_name=? "
                    "AND schema_version=? AND partition_year=? AND partition_month=?",
                    (
                        output_bytes,
                        credits,
                        semantic["state"],
                        _json(semantic),
                        partition_state,
                        _now(),
                        query_name,
                        spec.schema_version,
                        int(month[:4]),
                        int(month[5:7]),
                    ),
                )
            return result
        except (
            TimeoutError,
            OSError,
            RuntimeError,
            ValueError,
            TypeError,
            KeyError,
            sqlite3.Error,
            aiohttp.ClientError,
        ) as error:
            recovery = getattr(provider, "recovery_cursor", None)
            execution_id = recovery.get("execution_id") if isinstance(recovery, dict) else None
            result = {
                "query_name": query_name,
                "month": month,
                "execution_id": execution_id,
                "row_count": 0,
                "source_total_rows": 0,
                "source_result_bytes": 0,
                "materialization_mode": "NOT_MATERIALIZED",
                "output_bytes": 0,
                "credits_used": None,
                "execution_started": not resumed_execution,
                "schema_state": "FAIL",
                "semantic_state": "NOT_RUN",
                "semantic": {},
                "state": "FAILED",
                "error": f"{type(error).__name__}: {error}"[:500],
            }
            self._persist_partition(pilot_id, spec.schema_version, result)
            self.warehouse.record_dune_partition(
                {
                    "query_name": query_name,
                    "schema_version": spec.schema_version,
                    "month": month,
                    "execution_id": execution_id,
                    "offset": 0,
                    "total_rows": 0,
                    "state": "FAILED",
                    "error": result["error"],
                }
            )
            return result

    def _persist_partition(
        self, pilot_id: str, schema_version: str, result: dict[str, Any]
    ) -> None:
        with self.warehouse._lock, self.warehouse.conn:
            self.warehouse.conn.execute(
                "INSERT OR REPLACE INTO dune_pilot_partitions_v15("
                "pilot_id,query_name,schema_version,month,execution_id,row_count,"
                "source_total_rows,source_result_bytes,materialization_mode,output_bytes,"
                "credits_used,schema_state,semantic_state,semantic_json,state,last_error,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    pilot_id,
                    result["query_name"],
                    schema_version,
                    result["month"],
                    result.get("execution_id"),
                    result["row_count"],
                    result["source_total_rows"],
                    result["source_result_bytes"],
                    result["materialization_mode"],
                    result["output_bytes"],
                    result["credits_used"],
                    result["schema_state"],
                    result["semantic_state"],
                    _json(result.get("semantic") or {}),
                    result["state"],
                    result.get("error"),
                    _now(),
                ),
            )


def validate_dune_partition(
    warehouse: Any,
    query_name: str,
    month: str,
    dataset_id: str,
    parquet_root: Path,
    *,
    sample_limit: int = 5_000,
) -> dict[str, Any]:
    start = _month(month)
    end = _next_month(start)
    del dataset_id  # Pilot rows are Parquet-first; SQLite retains partition/checkpoint metadata.
    files = sorted(
        (parquet_root / query_name / f"year={month[:4]}" / f"month={month[5:7]}").glob(
            "*.parquet"
        )
    )
    if not files:
        return {
            "state": "FAIL",
            "row_count": 0,
            "sampled_rows": 0,
            "minimum_timestamp": None,
            "maximum_timestamp": None,
            "date_boundaries_valid": True,
            "sample_duplicate_rate": None,
            "invalid_counts": {},
            "missing_counts": {},
            "parquet_files": 0,
            "output_bytes": 0,
            "pumpfun_or_pumpswap_semantics": False,
            "sample_limited": False,
        }
    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover - research dependency is declared
        raise RuntimeError("install the research extra to validate Dune Parquet") from exc
    connection = duckdb.connect()
    try:
        file_names = [str(path.resolve()) for path in files]
        total, minimum, maximum = connection.execute(
            "SELECT COUNT(*),MIN(observed_at),MAX(observed_at) "
            "FROM read_parquet(?, union_by_name=true)",
            [file_names],
        ).fetchone()
        cursor = connection.execute(
            "SELECT * FROM read_parquet(?, union_by_name=true) ORDER BY observed_at LIMIT ?",
            [file_names, sample_limit],
        )
        columns = [item[0] for item in cursor.description]
        rows = [dict(zip(columns, values, strict=True)) for values in cursor.fetchall()]
    finally:
        connection.close()
    total = int(total)
    invalid = {
        "token_address": 0,
        "timestamp": 0,
        "transaction_id": 0,
        "source": 0,
        "side": 0,
        "usd_amount": 0,
        "wallet": 0,
        "outcome": 0,
    }
    missing = {"usd_amount": 0, "outcome_price": 0, "wallet": 0}
    seen = set()
    duplicates = 0
    for payload in rows:
        token = str(payload.get("token_address") or "")
        if not _SOLANA_ADDRESS.fullmatch(token):
            invalid["token_address"] += 1
        try:
            observed = _timestamp(payload.get("observed_at"))
            if not start <= observed < end:
                invalid["timestamp"] += 1
        except (TypeError, ValueError):
            invalid["timestamp"] += 1
        transaction = str(payload.get("tx_id") or "")
        if not _SOLANA_SIGNATURE.fullmatch(transaction):
            invalid["transaction_id"] += 1
        source = str(payload.get("source") or "").lower()
        if not source or not any(term in source for term in ("pump", "dex_solana", "solana")):
            invalid["source"] += 1
        if query_name == "pumpfun_trades":
            if str(payload.get("side") or "").lower() not in {"buy", "sell"}:
                invalid["side"] += 1
            if payload.get("amount_usd") is None:
                missing["usd_amount"] += 1
            elif not _nonnegative(payload.get("amount_usd")):
                invalid["usd_amount"] += 1
            if payload.get("trader") is None:
                missing["wallet"] += 1
            elif not _SOLANA_ADDRESS.fullmatch(str(payload.get("trader") or "")):
                invalid["wallet"] += 1
        if query_name == "outcome_reconstruction":
            if payload.get("amount_usd") is None:
                missing["usd_amount"] += 1
            elif not _nonnegative(payload.get("amount_usd")):
                invalid["outcome"] += 1
            if payload.get("price_usd") is None:
                missing["outcome_price"] += 1
            elif not _nonnegative(payload.get("price_usd")):
                invalid["outcome"] += 1
        key = (
            token,
            transaction,
            str(payload.get("side") or ""),
            str(payload.get("observed_at") or ""),
        )
        duplicates += int(key in seen)
        seen.add(key)
    output_bytes = sum(path.stat().st_size for path in files)
    invalid_total = sum(invalid.values())
    state = "PASS" if total > 0 and invalid_total == 0 and duplicates == 0 and files else "FAIL"
    return {
        "state": state,
        "row_count": total,
        "sampled_rows": len(rows),
        "minimum_timestamp": str(minimum) if minimum is not None else None,
        "maximum_timestamp": str(maximum) if maximum is not None else None,
        "date_boundaries_valid": invalid["timestamp"] == 0,
        "sample_duplicate_rate": duplicates / len(rows) if rows else None,
        "invalid_counts": invalid,
        "missing_counts": missing,
        "parquet_files": len(files),
        "output_bytes": output_bytes,
        "pumpfun_or_pumpswap_semantics": invalid_total == 0 and total > 0,
        "sample_limited": total > sample_limit,
    }


def execution_config(config: DuneAcquisitionConfig) -> DuneAcquisitionConfig:
    return replace(config, dry_run=False)


def _register_dataset(warehouse: Any, provider: Any, month: str, query_name: str) -> None:
    warehouse.register_dataset(
        {
            "dataset_id": provider.dataset_id,
            "dataset_version": f"dune-{query_name}-{provider.query_spec.schema_version}-{month}",
            "provider": provider.name,
            "chain": "solana",
            "acquisition_method": "repository_owned_direct_sql",
            "refresh_method": "immutable_month_partition_execution",
            "timestamp_precision": "query-defined chain block time",
            "reliability": "INDEXED_ONCHAIN_QUERY",
            "history_kind": "TRUE_HISTORICAL",
            "point_in_time_safe": True,
            "estimated_completeness": None,
            "missing_ranges_json": [],
            "rate_limit_json": {"bounded_retries": 2},
            "cost_json": {"credit_metered": True},
        }
    )


def _execution_credits(metadata: dict[str, Any]) -> float | None:
    for key in (
        "execution_cost_credits",
        "credits_used",
        "execution_cost",
        "credits",
        "cost",
    ):
        value = metadata.get(key)
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, dict):
            for nested in ("credits", "credits_used", "total"):
                if isinstance(value.get(nested), (int, float)):
                    return float(value[nested])
    return None


def _months(start: str | None, end: str | None) -> list[str]:
    if not start or not end:
        return []
    current = _month(start)
    finish = _month(end)
    values = []
    while current <= finish:
        values.append(current.strftime("%Y-%m"))
        current = _next_month(current)
    return values


def _month(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m").replace(tzinfo=UTC)


def _next_month(value: datetime) -> datetime:
    return (
        value.replace(year=value.year + 1, month=1)
        if value.month == 12
        else value.replace(month=value.month + 1)
    )


def _timestamp(value: Any) -> datetime:
    text = str(value).strip()
    if text.endswith(" UTC"):
        text = f"{text[:-4]}+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _nonnegative(value: Any) -> bool:
    try:
        return float(value) >= 0
    except (TypeError, ValueError):
        return False


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, default=str, separators=(",", ":"), sort_keys=True)
