"""Canonical promoted-creator library for post-certification V12."""
from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SUPPORTED_SCHEMA = "v12-creator-library-v2"


@dataclass(frozen=True, slots=True)
class CreatorRecord:
    creator: str
    status: str
    tier: str
    source: str
    quality_score: float
    selection_authority: str
    auto_buy: bool

    @property
    def promoted(self) -> bool:
        return self.status == "PROMOTED" and self.selection_authority == "RECOGNISED_CREATOR_ONLY"

    @property
    def elite(self) -> bool:
        return self.promoted and self.tier == "ELITE"


class CreatorLibrary:
    def __init__(self, payload: Mapping[str, Any]) -> None:
        schema = str(payload.get("schema_version") or "")
        if schema != SUPPORTED_SCHEMA:
            raise ValueError(f"unsupported creator library schema {schema!r}")
        self.payload = dict(payload)
        self.promoted: dict[str, CreatorRecord] = {}
        self.shortlisted: dict[str, CreatorRecord] = {}
        for bucket_name, target in (
            ("promoted", self.promoted),
            ("shortlisted", self.shortlisted),
        ):
            for raw in payload.get(bucket_name, []) or []:
                creator = str(raw.get("creator") or "")
                if not creator:
                    continue
                target[creator] = CreatorRecord(
                    creator=creator,
                    status=str(raw.get("status") or ""),
                    tier=str(raw.get("tier") or ""),
                    source=str(raw.get("source") or ""),
                    quality_score=float(raw.get("quality_score") or 0.0),
                    selection_authority=str(raw.get("selection_authority") or ""),
                    auto_buy=bool(raw.get("auto_buy")),
                )
        if any(row.auto_buy for row in self.promoted.values()):
            raise ValueError("creator library must never grant auto-buy authority")

    @classmethod
    def from_path(cls, path: Path) -> CreatorLibrary:
        return cls(json.loads(path.read_text(encoding="utf-8")))

    @classmethod
    def from_default_path(cls) -> CreatorLibrary:
        return cls.from_path(
            Path(os.getenv("V12_CREATOR_LIBRARY_PATH", "models/e4/v12-creator-library.json"))
        )

    def record(self, creator: str) -> CreatorRecord | None:
        key = str(creator or "")
        return self.promoted.get(key) or self.shortlisted.get(key)

    def is_promoted(self, creator: str) -> bool:
        row = self.promoted.get(str(creator or ""))
        return bool(row and row.promoted)

    def is_elite(self, creator: str) -> bool:
        row = self.promoted.get(str(creator or ""))
        return bool(row and row.elite)

    def is_shortlisted(self, creator: str) -> bool:
        return str(creator or "") in self.shortlisted

    def status(self) -> dict[str, Any]:
        return {
            "schema_version": SUPPORTED_SCHEMA,
            "promoted": len(self.promoted),
            "elite": sum(row.elite for row in self.promoted.values()),
            "shortlisted": len(self.shortlisted),
            "auto_buy_records": sum(row.auto_buy for row in self.promoted.values()),
        }


def causal_100_passed(state: Mapping[str, Any]) -> bool:
    metrics = state.get("metrics") or {}
    completion = state.get("completion") or {}
    return bool(
        completion.get("reached")
        and int(metrics.get("closed_trades") or 0) >= 100
        and metrics.get("acceptance_gate_passed") is True
    )


def promoted_library_activation_allowed(
    state: Mapping[str, Any],
    *,
    environment: Mapping[str, str] | None = None,
) -> bool:
    env = environment if environment is not None else os.environ
    requested = str(env.get("V12_PROMOTED_LIBRARY_ENABLED", "")).lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    return requested and causal_100_passed(state)
