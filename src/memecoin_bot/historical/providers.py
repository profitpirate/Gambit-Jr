from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .backfill import BackfillPage
from .store import RawEvidence


class JsonlHistoricalProvider:
    """Adapter for legitimate provider exports using the RawEvidence field contract."""

    def __init__(self, path: str | Path, dataset_id: str, name: str, page_size: int = 500):
        self.path = Path(path)
        self.dataset_id = dataset_id
        self.name = name
        self.page_size = page_size

    async def fetch_page(self, cursor: Any) -> BackfillPage:
        offset = int((cursor or {}).get("offset", 0))
        records = []
        next_offset = offset
        with self.path.open(encoding="utf-8") as source:
            for index, line in enumerate(source):
                if index < offset:
                    continue
                if len(records) >= self.page_size:
                    break
                raw = json.loads(line)
                if raw.get("dataset_id") not in {None, self.dataset_id}:
                    raise ValueError("JSONL record dataset does not match the configured dataset")
                records.append(
                    RawEvidence(
                        dataset_id=self.dataset_id,
                        provider=self.name,
                        chain=raw["chain"],
                        entity_type=raw["entity_type"],
                        entity_id=raw["entity_id"],
                        source_timestamp=raw["source_timestamp"],
                        availability_timestamp=raw["availability_timestamp"],
                        endpoint_type=raw["endpoint_type"],
                        payload=raw["payload"],
                        schema_version=raw["schema_version"],
                        acquisition_version=raw["acquisition_version"],
                        quality_state=raw.get("quality_state", "KNOWN"),
                        provenance=raw.get("provenance") or {},
                    )
                )
                next_offset = index + 1
        has_more = len(records) == self.page_size
        return BackfillPage(
            records,
            {"offset": next_offset} if has_more else None,
            None,
        )


class OperationalSnapshotProvider:
    """Moves Jr's actually observed live snapshots into the offline raw archive."""

    name = "gambit_jr_operational_store"

    def __init__(
        self,
        database_path: str | Path,
        dataset_id: str = "gambit-jr-observed-market",
        page_size: int = 500,
    ):
        self.database_path = Path(database_path).resolve()
        self.dataset_id = dataset_id
        self.page_size = page_size

    async def fetch_page(self, cursor: Any) -> BackfillPage:
        last_id = int((cursor or {}).get("last_id", 0))
        connection = sqlite3.connect(f"file:{self.database_path.as_posix()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                "SELECT s.*,t.chain,t.token_address,t.symbol,t.name,t.first_discovered_at "
                "FROM token_snapshots s JOIN tokens t ON t.id=s.token_id WHERE s.id>? "
                "ORDER BY s.id LIMIT ?",
                (last_id, self.page_size),
            ).fetchall()
        finally:
            connection.close()
        records = [
            RawEvidence(
                dataset_id=self.dataset_id,
                provider=self.name,
                chain=row["chain"],
                entity_type="token",
                entity_id=row["token_address"],
                source_timestamp=row["captured_at"],
                availability_timestamp=row["captured_at"],
                endpoint_type="operational_market_snapshot",
                payload=dict(row),
                schema_version="operational-snapshot-v1",
                acquisition_version="v1.5-history-transfer-v1",
                provenance={
                    "source_database": self.database_path.name,
                    "originally_observed_live": True,
                },
            )
            for row in rows
        ]
        has_more = len(rows) == self.page_size
        return BackfillPage(
            records,
            {"last_id": int(rows[-1]["id"])} if rows and has_more else None,
            None,
        )
