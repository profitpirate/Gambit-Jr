from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from .store import HistoricalWarehouse, RawEvidence


@dataclass(frozen=True, slots=True)
class BackfillPage:
    records: list[RawEvidence]
    next_cursor: Any = None
    queue_remaining: int | None = None
    retry_after_seconds: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class HistoricalProvider(Protocol):
    name: str
    dataset_id: str

    async def fetch_page(self, cursor: Any) -> BackfillPage: ...


class BackfillEngine:
    """Restart-safe, idempotent and provider-neutral historical ingestion."""

    def __init__(
        self,
        warehouse: HistoricalWarehouse,
        *,
        max_retries: int = 4,
        maximum_rate_limit_sleep_seconds: float = 30,
    ):
        self.warehouse = warehouse
        self.max_retries = max_retries
        self.maximum_rate_limit_sleep_seconds = maximum_rate_limit_sleep_seconds

    async def run(
        self,
        provider: HistoricalProvider,
        *,
        job_id: str | None = None,
        maximum_pages: int | None = None,
    ) -> dict[str, Any]:
        job_id = self.warehouse.begin_backfill(provider.dataset_id, provider.name, job_id)
        prior = self.warehouse.backfill_status(job_id) or {}
        cursor = json.loads(prior["cursor_json"]) if prior.get("cursor_json") else None
        pages = 0
        while maximum_pages is None or pages < maximum_pages:
            page = await self._fetch_with_retry(provider, cursor, job_id)
            earliest = min(
                (record.source_timestamp for record in page.records), default=None
            )
            latest = max((record.source_timestamp for record in page.records), default=None)
            ingested = 0
            for record in page.records:
                _evidence_id, inserted = self.warehouse.ingest_raw(record)
                ingested += int(inserted)
            pages += 1
            state = "COMPLETE" if page.next_cursor is None else "RUNNING"
            self.warehouse.checkpoint_backfill(
                job_id,
                cursor=page.next_cursor,
                ingested=ingested,
                queue_remaining=page.queue_remaining,
                earliest=earliest,
                latest=latest,
                state=state,
            )
            if page.retry_after_seconds:
                await asyncio.sleep(
                    min(page.retry_after_seconds, self.maximum_rate_limit_sleep_seconds)
                )
            if page.next_cursor is None:
                break
            cursor = page.next_cursor
        status = self.warehouse.backfill_status(job_id)
        assert status is not None
        return status

    async def _fetch_with_retry(
        self, provider: HistoricalProvider, cursor: Any, job_id: str
    ) -> BackfillPage:
        for attempt in range(self.max_retries + 1):
            try:
                return await provider.fetch_page(cursor)
            except Exception as error:
                if attempt >= self.max_retries:
                    self.warehouse.checkpoint_backfill(
                        job_id,
                        cursor=cursor,
                        ingested=0,
                        queue_remaining=None,
                        earliest=None,
                        latest=None,
                        state="FAILED",
                        error=f"{type(error).__name__}: {error}"[:500],
                    )
                    raise
                await asyncio.sleep(min(2**attempt, self.maximum_rate_limit_sleep_seconds))
        raise RuntimeError("unreachable retry state")
