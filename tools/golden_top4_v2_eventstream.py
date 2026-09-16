"""Forward-live V2 execution-state correction for event-driven Solana reserves.

The frozen FULL_3S_V2 selection policy, costs, sizing and 3-second exit remain
unchanged. This adapter fixes one integration error: a coin's reserve snapshot
was treated as stale merely because that coin had no TradeEvent for 500 ms.
On an event stream, no intervening coin event means the last reserve state is
still the latest observed state. Global feed silence remains an uncertainty.
The original frozen V2 files are not modified.
"""
from __future__ import annotations
import copy
import golden_full3s_v2 as frozen

PATCH_VERSION = "FULL_3S_V2_EVENT_STREAM_STATE_1"

class EventStreamSingleEngine(frozen.SingleEngine):
    def tick(self, now, states, last_feed_ns):
        if not isinstance(now, int) or now < self.last_now:
            self.accepting = False
            self.errors = list(dict.fromkeys(self.errors + ["CLOCK_REGRESSION"]))
            return
        self.last_now = now
        effective = {}
        feed_healthy = 0 <= now-last_feed_ns <= self.c.feed_stale_ms*1_000_000
        for mint, bundle in list(self.bundles.items()):
            previous = self._view.get(mint)
            incoming = states.get(mint)
            candidate = incoming if incoming is not None else previous
            if candidate is not None:
                if not isinstance(candidate.get("ns"), int) or candidate["ns"] > now:
                    self.input_rejections += 1; candidate = previous
                elif previous and (candidate["ns"] < previous["ns"] or candidate.get("slot",0) < previous.get("slot",0)):
                    self.input_rejections += 1; candidate = previous
                elif not candidate.get("complete") and not frozen.base.valid(candidate):
                    self.input_rejections += 1; candidate = previous
            if candidate is not None:
                self._view[mint] = dict(candidate)
            p = bundle["positions"][frozen.ARM]
            alive = p["status"] in ("OPEN", "PENDING")
            # A quiet coin is not a stale coin. The global stream is the liveness
            # signal; reserve state changes only when a coin event is observed.
            if alive and not feed_healthy and not (candidate and candidate.get("complete")):
                self.uncertain_mints.add(mint)
                effective[mint] = None
                if p["status"] == "PENDING" and now-bundle["decision"]["create_ns"] > self.c.max_entry_age_ms*1_000_000:
                    p["status"] = "NO_ENTRY"; p["abort_reason"] = "GLOBAL_FEED_UNHEALTHY_BEFORE_ENTRY_EXPIRY"
                    a = self.accounts[frozen.ARM]
                    a["failed_entry_fees"] += sum(p["fees"].values())
                    a["aborted_entries"].append(copy.deepcopy(p))
                    self.history.append({"mint":mint,"signal_id":p["signal_id"],"decision":bundle["decision"],"market_path":bundle["market_path"],"all_arms_closed":False,"creation_proof":bundle.get("creation_proof")})
                    del self.bundles[mint]; self._view.pop(mint,None)
                continue
            effective[mint] = candidate
        # Call the frozen accounting engine directly, bypassing the faulty
        # per-coin wall-clock staleness wrapper only.
        frozen.base.Engine.tick(self, now, effective, last_feed_ns)
        for mint in list(self._view):
            if mint not in self.bundles: self._view.pop(mint,None)
        if self.errors: self.accepting = False

# Runtime resolves SingleEngine dynamically in __init__/restore. Patching this
# module symbol changes only the integration wrapper, not frozen source bytes.
frozen.SingleEngine = EventStreamSingleEngine
Runtime = frozen.Runtime
Config = frozen.Config
ARM = frozen.ARM
MODEL = frozen.MODEL
