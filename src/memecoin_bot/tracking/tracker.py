from __future__ import annotations

import json
from datetime import datetime

from memecoin_bot.config import Settings
from memecoin_bot.database import Store
from memecoin_bot.models import iso
from memecoin_bot.providers.dexscreener import DexScreenerProvider


class SignalTracker:
    def __init__(self, store: Store, market: DexScreenerProvider, settings: Settings):
        self.store = store
        self.market = market
        self.settings = settings

    async def monitor_once(self) -> dict[str, int]:
        stats = {"monitored": 0, "milestones": 0, "failed": 0}
        for signal in self.store.active_signals():
            snapshot = await self.market.market_snapshot(signal["token_address"])
            if not snapshot or snapshot.market_cap_usd is None or snapshot.market_cap_usd <= 0:
                continue
            stats["monitored"] += 1
            self.store.save_snapshot(int(signal["token_id"]), snapshot)
            signal_mc = float(signal["signal_market_cap_usd"])
            current_mc = snapshot.market_cap_usd
            multiple = current_mc / signal_mc
            ath = max(float(signal["ath_market_cap_usd"] or signal_mc), current_mc)
            atl = min(float(signal["atl_market_cap_usd"] or signal_mc), current_mc)
            max_multiple = max(float(signal["max_multiple"]), ath / signal_mc)
            max_drawdown = min(float(signal["max_drawdown"]), (atl - signal_mc) / signal_mc)
            now = snapshot.captured_at
            self.store.update_tracking(int(signal["id"]), current_mc, now, max_multiple, max_drawdown, ath, atl)
            started = datetime.fromisoformat(signal["signal_timestamp"])
            seconds = (datetime.fromisoformat(now) - started).total_seconds()
            candidates = [
                (target, current_mc, seconds)
                for target in self.settings.milestones if multiple >= target
            ]
            payload = {
                "token_address": signal["token_address"], "symbol": signal["symbol"],
                "signal_market_cap_usd": signal_mc, "max_multiple": max_multiple,
            }
            hit = self.store.record_milestones(int(signal["id"]), candidates, payload)
            stats["milestones"] += len(hit)
            reached_2x = self.store.conn.execute(
                "SELECT 1 FROM milestones WHERE signal_id=? AND multiple>=2 LIMIT 1",
                (signal["id"],),
            ).fetchone() is not None
            if multiple <= self.settings.failure_multiple and not reached_2x:
                if self.store.fail_signal(int(signal["id"]), dict(
                    payload, current_multiple=multiple, max_drawdown=max_drawdown,
                )):
                    stats["failed"] += 1
        return stats
