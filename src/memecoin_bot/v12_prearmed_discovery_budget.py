"""Persisted limits for paid discovery fallback; execution routes remain independent."""
from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

STREAM_LEASE_BYTES = 4_194_304
DAILY_STREAM_BYTES = 50_000_000
DAILY_BALANCE_REQUESTS = 1_000


def provider_name(url):
    host = urlsplit(url).hostname or ""
    if host in {"api.mainnet.solana.com", "api.mainnet-beta.solana.com"}:
        return "SOLANA_PUBLIC"
    if host == "helius-rpc.com" or host.endswith(".helius-rpc.com"):
        return "HELIUS"
    if host.endswith(".quiknode.pro"):
        return "QUICKNODE"
    return "OTHER"


def public_first(urls):
    return tuple(sorted(dict.fromkeys(urls), key=lambda u: provider_name(u) != "SOLANA_PUBLIC"))


class DiscoveryBudget:
    """One trading worker owns this ledger under its existing exclusive wallet lock.

    Paid stream capacity is reserved before reading, in chunks at least as large
    as the websocket's maximum message. A restart cannot replenish the budget.
    The limits cover discovery and balance fallback, not total account billing.
    """

    def __init__(self, path, *, clock=time.time_ns):
        self.path, self.clock = Path(path), clock
        if self.path.is_symlink():
            raise ValueError("DISCOVERY_BUDGET_PATH_INVALID")
        self.data = json.loads(self.path.read_text()) if self.path.exists() else {}
        self.stream_provider_index = None
        self.stream_provider = "CONNECTING"
        self.stream_bytes = 0
        self.stream_messages = 0
        self.creates_decoded = 0
        self._day()

    def _day(self):
        day = datetime.fromtimestamp(self.clock() / 1e9, UTC).date().isoformat()
        previous = self.data.get("utc_day")
        if previous and day < previous:
            raise ValueError("DISCOVERY_BUDGET_CLOCK_REVERSED")
        if previous != day:
            self.data = {"utc_day": day, "providers": {}}
            self._save()
        return self.data

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.data, sort_keys=True))
        temporary.replace(self.path)

    def reserve(self, url, field, quantity, limit):
        name = provider_name(url)
        if name == "SOLANA_PUBLIC":
            return True
        if name not in {"HELIUS", "QUICKNODE"}:
            return False  # Do not invent a cost allowance for an unknown plan.
        self._day()
        row = self.data["providers"].setdefault(name, {})
        used = row.get(field, 0)
        if type(used) is not int or used < 0:
            raise ValueError("DISCOVERY_BUDGET_STATE_INVALID")
        if used + quantity > limit:
            return False
        row[field] = used + quantity
        self._save()
        return True

    def reserve_stream(self, url):
        return self.reserve(url, "stream_reserved_bytes", STREAM_LEASE_BYTES, DAILY_STREAM_BYTES)

    def reserve_balance(self, url):
        return self.reserve(url, "balance_requests", 1, DAILY_BALANCE_REQUESTS)

    def snapshot(self):
        self._day()
        value = {
            "mode": "PUBLIC_FULL_PROGRAM_WITH_BOUNDED_PAID_FALLBACK",
            "stream_provider": self.stream_provider,
            "stream_provider_index": self.stream_provider_index,
            "stream_bytes_received": self.stream_bytes,
            "stream_messages_received": self.stream_messages,
            "creates_decoded": self.creates_decoded,
            "utc_day": self.data["utc_day"],
            "paid_stream_daily_limit_bytes": DAILY_STREAM_BYTES,
            "paid_balance_daily_limit_requests": DAILY_BALANCE_REQUESTS,
            "scope": "DISCOVERY_AND_BALANCE_FALLBACK_NOT_TOTAL_ACCOUNT_USAGE",
        }
        for name in ("HELIUS", "QUICKNODE"):
            row = self.data["providers"].get(name, {})
            value[name.lower() + "_stream_reserved_bytes"] = row.get("stream_reserved_bytes", 0)
            value[name.lower() + "_balance_requests"] = row.get("balance_requests", 0)
        return value
