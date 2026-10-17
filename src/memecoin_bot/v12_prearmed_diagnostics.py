"""Bounded, credential-free diagnostics. Never serialize exception messages."""

from __future__ import annotations

import re
import math
import traceback
from collections import Counter, deque
from .v12_prearmed_timing import BUILDER_TIMINGS, Timings


class DiagnosticError(ValueError):
    def __init__(self, reason, *, http_status=None):
        super().__init__(reason)
        self.reason = reason
        self.http_status = http_status


SAFE_REASONS = frozenset(
    {
        "RPC_HTTP_REJECTED",
        "RPC_SERVER_REJECTED",
        "ENTRY_EXPIRED",
        "METADATA_URI_UNSUPPORTED",
        "METADATA_ADDRESS_BLOCKED",
        "METADATA_HTTP_REJECTED",
        "METADATA_TOO_LARGE",
        "METADATA_JSON_INVALID",
        "METADATA_NOT_OBJECT",
        "METADATA_TIMEOUT",
        "METADATA_NETWORK_FAILED",
        "METADATA_IPFS_CID_UNSUPPORTED",
        "METADATA_IPFS_LAYOUT_UNSUPPORTED",
        "METADATA_IPFS_INTEGRITY_FAILED",
        "METADATA_GATEWAY_COOLDOWN",
        "SOCIAL_EXTENSIONS_INVALID",
        "SOCIAL_STATUS_URL_REQUIRED",
        "SOCIAL_STATUS_PATH_INVALID",
        "CURVE_MIGRATED",
        "ENTRY_CHASE_GUARD_REJECTED",
        "ENTRY_OUTPUT_GUARD_REJECTED",
        "BUILD_REJECTED",
        "BUILDER_UNAVAILABLE",
        "BUILDER_RESPONSE_MISMATCH",
        "QUOTE_MINT_UNAVAILABLE",
        "QUOTE_RPC_COOLDOWN",
        "QUOTE_STATE_UNAVAILABLE",
        "QUOTE_TOKEN_UNSUPPORTED",
        "QUOTE_DECIMALS_UNSUPPORTED",
        "QUOTE_CURRENCY_UNSUPPORTED",
        "QUOTE_MAYHEM",
        "QUOTE_FEES_UNAVAILABLE",
        "QUOTE_CREATOR_CHANGED",
        "QUOTE_CREATOR_REQUIRED",
        "QUOTE_CREATOR_PROOF_UNAVAILABLE",
        "QUOTE_CREATOR_PROOF_INVALID",
        "QUOTE_BUDGET_INSUFFICIENT",
        "QUOTE_CALCULATION_FAILED",
        "SIMULATION_FAILED",
        "SIMULATION_BUDGET_EXCEEDED",
        "FEED_SUBSCRIPTION_REJECTED",
        "FEED_CLOSED",
        "FEED_HTTP_REJECTED",
    }
)


def failure(stage, error):
    # Only explicit codes from our own contract may leave the process.
    candidate = (
        error.reason if isinstance(error, DiagnosticError) else str(error).removeprefix("BUILDER:")
    )
    reason = (
        candidate
        if candidate in SAFE_REASONS
        else (stage.upper() + ("_TIMEOUT" if isinstance(error, TimeoutError) else "_FAILED"))
    )
    result = {"reason": reason, "error_type": type(error).__name__[:64]}
    status = getattr(error, "http_status", None)
    if type(status) is int and 100 <= status <= 599:
        result["http_status"] = status
    attempt = getattr(error, "metadata_attempt", None)
    if type(attempt) is int and 1 <= attempt <= 4:
        result["metadata_attempt"] = attempt
    location = getattr(error, "source_location", None)
    if isinstance(location, str) and re.fullmatch(r"[A-Za-z0-9_.-]+:\d+(?::\d+)?", location):
        result["source_location"] = location
    elif error.__traceback__:
        frames = [
            frame
            for frame in traceback.extract_tb(error.__traceback__)
            if "v12_prearmed" in frame.filename
        ]
        if frames:
            frame = frames[-1]
            result["source_location"] = (
                frame.filename.replace("\\", "/").rsplit("/", 1)[-1] + ":" + str(frame.lineno)
            )
    remote_type = getattr(error, "remote_error_type", None)
    if isinstance(remote_type, str) and re.fullmatch(r"[A-Za-z]{1,64}", remote_type):
        result["remote_error_type"] = remote_type
    timings = getattr(error, "builder_timings", {})
    for key, value in (timings if isinstance(timings, dict) else {}).items():
        if key in BUILDER_TIMINGS and type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 120000:
            result["builder_" + key] = value
    result.update(quote_facts(getattr(error, 'quote_facts', {})))
    provider = getattr(error, 'provider_index', None)
    if type(provider) is int and 0 <= provider < 16:
        result['provider_index'] = provider
    return result


def quote_facts(values):
    """Allow only finite price facts and an integer bank slot from the builder."""
    if not isinstance(values, dict):
        return {}
    result = {}
    for key in ('launch_price', 'curve_price', 'price_multiple', 'maximum_price_multiple'):
        value = values.get(key)
        if type(value) in (int, float) and math.isfinite(value) and 0 < value < 1e30:
            result[key] = value
    slot = values.get('state_slot')
    if type(slot) is int and 0 < slot < 2**53:
        result['state_slot'] = slot
    return result


class Diagnostics:
    def __init__(self, emit, clock, run_id):
        self.emit, self.clock, self.run_id = emit, clock, run_id
        self.counts = Counter()
        self.reasons = Counter()
        self.recent_faults = deque(maxlen=100)
        self.last_progress_ns = 0
        self.last_fault_ns = 0
        self.timings = Timings()

    def record(
        self, event, *, stage, reason, category="progress", mint=None, started_ns=None, **details
    ):
        now = self.clock()
        self.counts[event] += 1
        if category in ("fault", "policy", "blocked"):
            self.reasons[reason] += 1
        if category == "fault":
            self.last_fault_ns = now
            self.recent_faults.append(now)
        else:
            self.last_progress_ns = now
        payload = dict(
            stage=stage,
            reason=reason,
            category=category,
            run_id=self.run_id,
            observed_ns=now,
            **details,
        )
        if mint:
            payload["mint"] = mint
            payload["candidate_id"] = self.run_id + ":" + mint
        if started_ns is not None:
            payload["elapsed_ms"] = max(0, (now - started_ns) / 1e6)
        for key, value in details.items():
            name = key
            if key.startswith('builder_'):
                name = ('quote_' if stage == 'quote' else 'build_') + key[8:]
            self.timings.observe(name, value, now)
        self.emit(event, payload)

    def snapshot(self):
        now = self.clock()
        recent = sum(now - value <= 900_000_000_000 for value in self.recent_faults)
        return {
            "schema": 2,
            "run_id": self.run_id,
            "counters": dict(self.counts),
            "reasons": dict(self.reasons),
            "recent_fault_count": recent,
            "last_progress_ns": self.last_progress_ns,
            "last_fault_ns": self.last_fault_ns,
            "pipeline_health": "DEGRADED" if recent else "NO_RECENT_REPORTED_FAULTS",
            "execution_verification": "NOT_ATTESTED",
            "timing": self.timings.snapshot(now),
        }
