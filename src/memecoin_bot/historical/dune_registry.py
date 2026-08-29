from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from memecoin_bot.models import iso

_PARAMETER = re.compile(r"\{\{([a-z][a-z0-9_]*)\}\}")


@dataclass(frozen=True, slots=True)
class DuneQuerySpec:
    query_name: str
    schema_version: str
    sql_sha256: str
    expected_columns: tuple[str, ...]
    parameters: tuple[str, ...]
    source_tables: tuple[str, ...]
    minimum_date: str
    compatibility_status: str
    docs_checked_at: str
    template_path: Path


def _safe_timestamp(value: Any, name: str) -> str:
    text = str(value).strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    parsed = parsed.astimezone(UTC)
    if parsed.year < 2024 or parsed.year > datetime.now(UTC).year + 1:
        raise ValueError(f"{name} is outside the supported Pump.fun history range")
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


class DuneQueryRegistry:
    """Versioned repository SQL with strict parameters and executable contracts."""

    def __init__(self, root: str | Path | None = None):
        self.root = Path(root) if root else Path(__file__).with_name("sql") / "dune"
        metadata = json.loads((self.root / "registry.json").read_text(encoding="utf-8"))
        self._specs: dict[str, DuneQuerySpec] = {}
        for row in metadata["queries"]:
            path = self.root / str(row["template"])
            sql = path.read_text(encoding="utf-8")
            name = str(row["query_name"])
            placeholders = tuple(dict.fromkeys(_PARAMETER.findall(sql)))
            declared = tuple(str(value) for value in row["parameters"])
            if set(placeholders) != set(declared):
                raise ValueError(f"Dune query {name} parameter contract does not match SQL")
            self._specs[name] = DuneQuerySpec(
                query_name=name,
                schema_version=str(row["schema_version"]),
                sql_sha256=hashlib.sha256(sql.encode()).hexdigest(),
                expected_columns=tuple(str(value) for value in row["expected_columns"]),
                parameters=declared,
                source_tables=tuple(str(value) for value in row["source_tables"]),
                minimum_date=str(row["minimum_date"]),
                compatibility_status=str(row["compatibility_status"]),
                docs_checked_at=str(metadata["docs_checked_at"]),
                template_path=path,
            )

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._specs))

    def spec(self, query_name: str) -> DuneQuerySpec:
        try:
            return self._specs[query_name]
        except KeyError as exc:
            raise KeyError(f"unknown repository Dune query: {query_name}") from exc

    def render(self, query_name: str, parameters: Mapping[str, Any]) -> str:
        spec = self.spec(query_name)
        if set(parameters) != set(spec.parameters):
            raise ValueError(
                f"{query_name} requires exactly these parameters: {', '.join(spec.parameters)}"
            )
        values = {name: _safe_timestamp(parameters[name], name) for name in spec.parameters}
        sql = spec.template_path.read_text(encoding="utf-8")
        return _PARAMETER.sub(lambda match: values[match.group(1)], sql)

    def validate_columns(self, query_name: str, rows: list[Mapping[str, Any]]) -> None:
        if not rows:
            return
        expected = set(self.spec(query_name).expected_columns)
        missing = expected - set(rows[0])
        if missing:
            raise ValueError(
                f"Dune {query_name} result is missing contract columns: {sorted(missing)}"
            )

    def register(self, warehouse: Any) -> None:
        with warehouse._lock, warehouse.conn:
            for spec in self._specs.values():
                warehouse.conn.execute(
                    "INSERT INTO dune_query_registry_v15 VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(query_name,schema_version) DO UPDATE SET "
                    "sql_sha256=excluded.sql_sha256,compatibility_status="
                    "excluded.compatibility_status,registered_at=excluded.registered_at",
                    (
                        spec.query_name,
                        spec.schema_version,
                        spec.sql_sha256,
                        spec.template_path.name,
                        json.dumps(spec.expected_columns),
                        json.dumps(spec.parameters),
                        json.dumps(spec.source_tables),
                        spec.minimum_date,
                        spec.compatibility_status,
                        spec.docs_checked_at,
                        iso(),
                    ),
                )
