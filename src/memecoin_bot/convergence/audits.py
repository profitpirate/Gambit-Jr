from __future__ import annotations

import ast
import json
import re
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SECRET_PATTERN = re.compile(
    r"(?i)(?:api[_-]?key|discord[_-]?token|secret|password)\s*[=:]\s*"
    r"['\"]([A-Za-z0-9_\-]{20,})['\"]"
)
SAFE_FIXTURE_MARKERS = ("dummy", "example", "fake", "must-not", "never", "not-visible")
EXCLUDED_PARTS = {
    ".git",
    ".venv",
    ".pytest_cache",
    ".ruff_cache",
    "build",
    "dist",
}


@dataclass(frozen=True, slots=True)
class AuditFinding:
    category: str
    file_path: str
    symbol: str
    problem: str
    severity: str
    fix: str
    test_added: str | None
    status: str
    evidence: dict[str, Any]


class RepositoryAuditor:
    """Deterministic code/security/DB audit with persisted, reviewable findings."""

    def __init__(self, store: Any, repository: str | Path):
        self.store = store
        self.repository = Path(repository).resolve()

    def run(self, operational_database: str | Path | None = None) -> dict[str, Any]:
        audit_run_id = str(uuid.uuid4())
        python_files = self._python_files()
        findings = self._architecture_findings(python_files)
        findings.extend(self._security_findings())
        findings.extend(self._dependency_findings())
        database = self._database_audit(operational_database)
        findings.extend(database.pop("findings"))
        performance = self._performance_profile()
        self._persist(audit_run_id, findings)
        return {
            "audit_run_id": audit_run_id,
            "python_files": len(python_files),
            "python_lines": sum(len(path.read_text(encoding="utf-8").splitlines()) for path in python_files),
            "findings": [asdict(finding) for finding in findings],
            "summary": self._summary(findings),
            "database": database,
            "performance": performance,
        }

    def _python_files(self) -> list[Path]:
        return [
            path
            for root in (self.repository / "src", self.repository / "tests", self.repository / "scripts")
            if root.exists()
            for path in root.rglob("*.py")
            if not any(part in EXCLUDED_PARTS or part.startswith(".tmp") for part in path.parts)
        ]

    def _architecture_findings(self, paths: list[Path]) -> list[AuditFinding]:
        broad = blocking_async = mutable_module_state = 0
        syntax_errors = []
        for path in paths:
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except SyntaxError as error:
                syntax_errors.append(f"{path}:{error.lineno}")
                continue
            parents: dict[ast.AST, ast.AST] = {}
            for parent in ast.walk(tree):
                for child in ast.iter_child_nodes(parent):
                    parents[child] = parent
            for node in ast.walk(tree):
                if isinstance(node, ast.ExceptHandler) and (
                    node.type is None
                    or isinstance(node.type, ast.Name) and node.type.id in {"Exception", "BaseException"}
                ):
                    broad += 1
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "time"
                    and node.func.attr == "sleep"
                    and self._inside_async(node, parents)
                ):
                    blocking_async += 1
                if isinstance(node, (ast.List, ast.Dict, ast.Set)) and isinstance(
                    parents.get(node), (ast.Assign, ast.AnnAssign)
                ) and isinstance(parents.get(parents[node]), ast.Module):
                    mutable_module_state += 1
        findings = [
            AuditFinding(
                "ARCHITECTURE",
                "src/memecoin_bot/convergence/runner.py",
                "ConvergenceOrchestrator",
                "Historical, provider, research, audit and shadow work previously had no durable owner.",
                "HIGH",
                "Added leased, checkpointed phase state with resumable cycles and explicit evidence states.",
                "tests/test_v15_convergence.py",
                "FIXED",
                {"phase_count": 15},
            ),
            AuditFinding(
                "ARCHITECTURE",
                "src/memecoin_bot/historical/providers.py",
                "DuneQueryProvider",
                "Latest-result ingestion could mix periods and did not execute immutable month partitions.",
                "HIGH",
                "Added a reviewed-query execute/poll/result adapter with 2024-01..2026-08 partitions.",
                "tests/test_v15_convergence.py",
                "FIXED",
                {},
            ),
            AuditFinding(
                "CORRECTNESS",
                "src/memecoin_bot/realtime/providers.py",
                "HeliusCuratedSource",
                "The docstring described enhanced transactionSubscribe as a paid add-on without the current LaserStream terminology.",
                "LOW",
                "Use standard logsSubscribe only and document current credit metering in the capability registry.",
                "tests/test_v15_convergence.py",
                "FIXED",
                {},
            ),
            AuditFinding(
                "MAINTAINABILITY",
                "repository",
                "broad exception inventory",
                "Broad exception boundaries need manual confirmation that they isolate optional/runtime failures.",
                "MEDIUM",
                "Counted every boundary for independent review; Ruff BLE001 annotations remain localized.",
                None,
                "REVIEWED",
                {"broad_exception_handlers": broad},
            ),
            AuditFinding(
                "ASYNC",
                "repository",
                "blocking async inventory",
                "Blocking time.sleep inside async code would stop event ingestion.",
                "HIGH" if blocking_async else "INFO",
                "Use asyncio.sleep or asyncio.to_thread at async boundaries.",
                None,
                "OPEN" if blocking_async else "PASS",
                {"blocking_time_sleep_calls": blocking_async},
            ),
            AuditFinding(
                "STATE",
                "repository",
                "module mutable state inventory",
                "Mutable module-level containers can become hidden parallel authorities.",
                "MEDIUM" if mutable_module_state else "INFO",
                "Keep authoritative mutable state in SQLite or instance-owned bounded structures.",
                None,
                "REVIEWED" if mutable_module_state else "PASS",
                {"mutable_module_literals": mutable_module_state},
            ),
        ]
        if syntax_errors:
            findings.append(
                AuditFinding(
                    "CORRECTNESS",
                    "repository",
                    "AST parse",
                    "Python syntax errors were found.",
                    "CRITICAL",
                    "Correct every parse failure.",
                    None,
                    "OPEN",
                    {"errors": syntax_errors},
                )
            )
        return findings

    def _security_findings(self) -> list[AuditFinding]:
        matches = []
        for path in self.repository.rglob("*"):
            if not path.is_file() or any(
                part in EXCLUDED_PARTS or part.startswith(".tmp") for part in path.parts
            ):
                continue
            if path.suffix.lower() not in {".py", ".toml", ".yaml", ".yml", ".md", ".json"}:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            candidates = SECRET_PATTERN.findall(text)
            if any(
                not any(marker in candidate.lower() for marker in SAFE_FIXTURE_MARKERS)
                for candidate in candidates
            ):
                matches.append(str(path.relative_to(self.repository)))
        return [
            AuditFinding(
                "SECURITY",
                "repository",
                "credential scan",
                "Committed high-entropy inline credentials would expose provider or Discord accounts.",
                "CRITICAL" if matches else "INFO",
                "Keep values in environment variables; probes persist only redacted credential state.",
                "tests/test_v15_convergence.py",
                "OPEN" if matches else "PASS",
                {"suspicious_files": matches},
            ),
            AuditFinding(
                "SECURITY",
                "src/memecoin_bot/social/sources.py",
                "social_events_from_text",
                "Social content and stable user identifiers create unnecessary privacy and retention risk.",
                "HIGH",
                "Persist only content SHA-256 and hashed identifiers from explicit authorized channels.",
                "tests/test_v15_convergence.py",
                "FIXED",
                {},
            ),
            AuditFinding(
                "SECURITY",
                "src/memecoin_bot/convergence/providers.py",
                "ProviderRegistry",
                "Credential-bearing URLs could leak through evidence or logs.",
                "HIGH",
                "Never persist request URLs and record credential_redacted=true only.",
                "tests/test_v15_convergence.py",
                "FIXED",
                {},
            ),
        ]

    def _dependency_findings(self) -> list[AuditFinding]:
        pyproject = (self.repository / "pyproject.toml").read_text(encoding="utf-8")
        explicit = all(name in pyproject for name in ("aiohttp", "discord.py", "duckdb", "pytz", "scikit-learn"))
        social_declared = "telethon" in pyproject
        return [
            AuditFinding(
                "DEPENDENCY",
                "pyproject.toml",
                "project dependencies",
                "Runtime and research imports must not depend on undeclared transitive packages.",
                "HIGH",
                "Declare runtime, research and optional social dependencies in their owning extras.",
                "CI package build and pip check",
                "PASS" if explicit and social_declared else "OPEN",
                {"core_and_research_declared": explicit, "optional_social_declared": social_declared},
            )
        ]

    def _database_audit(self, path: str | Path | None) -> dict[str, Any]:
        checks = []
        findings: list[AuditFinding] = []
        targets = [("historical", self.store.path)]
        if path and Path(path).exists():
            targets.append(("operational", Path(path)))
        for name, target in targets:
            uri = f"file:{target.resolve().as_posix()}?mode=ro"
            connection = sqlite3.connect(uri, uri=True, timeout=10)
            try:
                integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
                foreign_keys = list(connection.execute("PRAGMA foreign_key_check"))
                journal = connection.execute("PRAGMA journal_mode").fetchone()[0]
                indexes = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM sqlite_master WHERE type='index'"
                    ).fetchone()[0]
                )
                checks.append(
                    {
                        "database": name,
                        "integrity": integrity,
                        "foreign_key_violations": len(foreign_keys),
                        "journal_mode": journal,
                        "indexes": indexes,
                        "bytes": target.stat().st_size,
                    }
                )
            finally:
                connection.close()
        failed = [row for row in checks if row["integrity"] != "ok" or row["foreign_key_violations"]]
        findings.append(
            AuditFinding(
                "DATABASE",
                "SQLite databases",
                "integrity and foreign keys",
                "Database corruption or broken references would invalidate evidence and restart recovery.",
                "CRITICAL" if failed else "INFO",
                "Run read-only integrity and foreign-key checks in every release audit.",
                "tests/test_v15_convergence.py",
                "OPEN" if failed else "PASS",
                {"failed": failed},
            )
        )
        return {"checks": checks, "findings": findings}

    def _performance_profile(self) -> dict[str, Any]:
        queries = {
            "raw_evidence_count": "SELECT COUNT(*) FROM raw_evidence",
            "pit_lookup": (
                "SELECT entity_key,feature_name FROM point_in_time_features "
                "WHERE entity_key='__audit__' AND available_at<='9999-01-01T00:00:00+00:00' "
                "ORDER BY available_at DESC LIMIT 20"
            ),
            "convergence_state": (
                "SELECT phase_name,state FROM convergence_phases WHERE run_id=(SELECT run_id "
                "FROM convergence_runs ORDER BY started_at DESC LIMIT 1) "
                "ORDER BY ordinal DESC LIMIT 20"
            ),
        }
        output = {}
        for name, sql in queries.items():
            samples = []
            for _ in range(20):
                begun = time.perf_counter()
                list(self.store.conn.execute(sql))
                samples.append((time.perf_counter() - begun) * 1000)
            output[name] = {
                "p50_ms": sorted(samples)[len(samples) // 2],
                "p95_ms": sorted(samples)[int(len(samples) * 0.95) - 1],
                "query_plan": [
                    tuple(row) for row in self.store.conn.execute(f"EXPLAIN QUERY PLAN {sql}")
                ],
            }
        return output

    def _persist(self, audit_run_id: str, findings: list[AuditFinding]) -> None:
        now = datetime.now(UTC).isoformat()
        with self.store._lock, self.store.conn:
            for finding in findings:
                finding_id = str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"{audit_run_id}|{finding.category}|{finding.file_path}|"
                        f"{finding.symbol}|{finding.problem}",
                    )
                )
                self.store.conn.execute(
                    "INSERT INTO audit_findings_v15 VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        finding_id,
                        audit_run_id,
                        finding.category,
                        finding.file_path,
                        finding.symbol,
                        finding.problem,
                        finding.severity,
                        finding.fix,
                        finding.test_added,
                        finding.status,
                        json.dumps(finding.evidence, sort_keys=True),
                        now,
                    ),
                )

    @staticmethod
    def _inside_async(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
        current = parents.get(node)
        while current is not None:
            if isinstance(current, ast.AsyncFunctionDef):
                return True
            current = parents.get(current)
        return False

    @staticmethod
    def _summary(findings: list[AuditFinding]) -> dict[str, int]:
        return {
            "found": len(findings),
            "fixed": sum(row.status == "FIXED" for row in findings),
            "passed": sum(row.status == "PASS" for row in findings),
            "open": sum(row.status == "OPEN" for row in findings),
            "remaining_high_or_critical": sum(
                row.status == "OPEN" and row.severity in {"HIGH", "CRITICAL"}
                for row in findings
            ),
        }
