from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from memecoin_bot.models import iso


class AlphaState(StrEnum):
    DISCOVERED = "DISCOVERED"
    SCREENING = "SCREENING"
    GENESIS_RADAR = "GENESIS_RADAR"
    STANDARD_RADAR = "STANDARD_RADAR"
    HOT_RADAR = "HOT_RADAR"
    PRIORITY_RADAR = "PRIORITY_RADAR"
    QUALIFIED_SIGNAL = "QUALIFIED_SIGNAL"
    EXPIRED = "EXPIRED"
    REJECTED_UNSAFE = "REJECTED_UNSAFE"
    FAILED_OUTCOME = "FAILED_OUTCOME"


class EntryState(StrEnum):
    VERY_EARLY = "VERY_EARLY"
    EARLY = "EARLY"
    ACCEPTABLE = "ACCEPTABLE"
    EXTENDED = "EXTENDED"
    CHASING = "CHASING"
    LATE = "LATE"
    UNKNOWN = "UNKNOWN"


class SurvivalGrade(StrEnum):
    STRONG = "STRONG"
    ACCEPTABLE = "ACCEPTABLE"
    WEAK = "WEAK"
    HIGH_RISK = "HIGH_RISK"
    UNKNOWN = "UNKNOWN"


class PayoffGrade(StrEnum):
    EXCEPTIONAL = "EXCEPTIONAL"
    CONVEX = "CONVEX"
    BALANCED = "BALANCED"
    POOR = "POOR"
    UNKNOWN = "UNKNOWN"


class CreatorQuality(StrEnum):
    PROVEN = "PROVEN"
    POSITIVE = "POSITIVE"
    NEUTRAL = "NEUTRAL"
    UNKNOWN = "UNKNOWN"
    SUSPICIOUS = "SUSPICIOUS"
    TOXIC = "TOXIC"


@dataclass(slots=True)
class LaunchEvent:
    event_key: str
    source: str
    chain: str
    token_address: str
    source_event_timestamp: str
    source_received_at: str = field(default_factory=iso)
    launchpad: str | None = None
    creator_address: str | None = None
    phase: str = "CREATED"
    slot_or_block: str | None = None
    transaction_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def deterministic(
        cls,
        source: str,
        chain: str,
        token_address: str,
        source_event_timestamp: str,
        **kwargs: Any,
    ) -> LaunchEvent:
        # A chain transaction is the durable identity when the source supplies one.
        # Falling back to the event timestamp keeps synthetic/polling sources stable
        # without letting a reconnect manufacture a second launch from the same tx.
        source_identity = str(kwargs.get("transaction_id") or source_event_timestamp)
        fingerprint = "|".join(
            (
                source,
                chain,
                token_address.lower(),
                source_identity,
                str(kwargs.get("phase", "CREATED")),
            )
        )
        return cls(
            event_key=hashlib.sha256(fingerprint.encode()).hexdigest(),
            source=source,
            chain=chain,
            token_address=token_address,
            source_event_timestamp=source_event_timestamp,
            **kwargs,
        )


@dataclass(slots=True)
class AlphaDecision:
    state: AlphaState
    entry_state: EntryState
    confidence: float
    reasons: list[str]
    unknowns: list[str]
    feature_vector: dict[str, Any]
    stage: str = "T0"
    survival: SurvivalGrade = SurvivalGrade.UNKNOWN
    payoff: PayoffGrade = PayoffGrade.UNKNOWN


@dataclass(slots=True)
class WalletBuy:
    wallet: str
    bought_at: str
    amount_usd: float | None = None
    funder: str | None = None
    deployer_linked: bool = False
    sold_percent: float | None = None


@dataclass(slots=True)
class WalletGraphResult:
    clusters: list[list[str]]
    connected_wallets: int
    connected_percent: float
    coordinated: bool
    deployer_linked_wallets: int
    concentration_percent: float | None
    warnings: list[str]
    evidence: dict[str, Any]


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def percentile(values: Iterable[float], quantile: float) -> float | None:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    rank = (len(ordered) - 1) * quantile
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    fraction = rank - low
    return ordered[low] * (1 - fraction) + ordered[high] * fraction


def latency_summary(values: Iterable[float]) -> dict[str, float | int | None]:
    samples = list(values)
    return {
        "count": len(samples),
        "p50_ms": percentile(samples, 0.50),
        "p95_ms": percentile(samples, 0.95),
        "max_ms": max(samples) if samples else None,
    }


def entry_state(features: dict[str, Any]) -> EntryState:
    age_seconds = features.get("age_seconds")
    price_change = features.get("price_change_from_launch_percent")
    curve = features.get("bonding_curve_progress_percent")
    if age_seconds is None and price_change is None and curve is None:
        return EntryState.UNKNOWN
    age = float(age_seconds or 0)
    move = float(price_change or 0)
    progress = float(curve or 0)
    if move >= 500 or age > 3600 or progress >= 98:
        return EntryState.LATE
    if move >= 300 or age > 1800 or progress >= 90:
        return EntryState.CHASING
    if move >= 150 or age > 900 or progress >= 75:
        return EntryState.EXTENDED
    if age <= 90 and move <= 30 and progress <= 25:
        return EntryState.VERY_EARLY
    if age <= 300 and move <= 80 and progress <= 50:
        return EntryState.EARLY
    return EntryState.ACCEPTABLE


def t0_decision(features: dict[str, Any]) -> AlphaDecision:
    """Cheap, deterministic first decision using only evidence available at T0."""
    unknowns = [
        name
        for name in (
            "liquidity_usd",
            "creator_address",
            "bonding_curve_progress_percent",
            "buyer_count",
        )
        if features.get(name) is None
    ]
    reasons: list[str] = []
    entry = entry_state(features)
    if features.get("invalid_contract") is True:
        return AlphaDecision(
            AlphaState.REJECTED_UNSAFE,
            entry,
            1,
            ["INVALID_CONTRACT"],
            unknowns,
            features,
        )
    if (
        features.get("mint_authority_active") is True
        or features.get("freeze_authority_active") is True
    ):
        return AlphaDecision(
            AlphaState.REJECTED_UNSAFE,
            entry,
            1,
            ["TERMINAL_AUTHORITY_RISK"],
            unknowns,
            features,
        )
    if features.get("launch_event_verified") is not True:
        return AlphaDecision(
            AlphaState.SCREENING,
            entry,
            0.15,
            ["AWAITING_VERIFIED_LAUNCH_EVENT"],
            unknowns,
            features,
        )
    reasons.append("VERIFIED_DIRECT_LAUNCH_EVENT")
    if entry in {EntryState.VERY_EARLY, EntryState.EARLY}:
        reasons.append("ULTRA_EARLY_ENTRY_WINDOW")
    buyer_count = features.get("buyer_count")
    if buyer_count is not None and int(buyer_count) >= 10:
        reasons.append("FIRST_BUYER_COHORT_FORMING")
    coverage_fields = 4 - len(unknowns)
    confidence = min(0.55, 0.20 + coverage_fields * 0.0875)
    return AlphaDecision(
        AlphaState.GENESIS_RADAR,
        entry,
        confidence,
        reasons,
        unknowns,
        features,
    )


class WalletGraphEngine:
    """Build connected-buyer components without treating a relationship as an automatic reject."""

    def analyze(self, buys: Iterable[WalletBuy]) -> WalletGraphResult:
        rows = list(buys)
        parents = {row.wallet: row.wallet for row in rows}

        def root(wallet: str) -> str:
            while parents[wallet] != wallet:
                parents[wallet] = parents[parents[wallet]]
                wallet = parents[wallet]
            return wallet

        def union(left: str, right: str) -> None:
            left_root, right_root = root(left), root(right)
            if left_root != right_root:
                parents[right_root] = left_root

        by_funder: dict[str, list[str]] = {}
        for row in rows:
            if row.funder:
                by_funder.setdefault(row.funder, []).append(row.wallet)
        for wallets in by_funder.values():
            for wallet in wallets[1:]:
                union(wallets[0], wallet)
        groups: dict[str, list[str]] = {}
        for wallet in parents:
            groups.setdefault(root(wallet), []).append(wallet)
        clusters = [sorted(group) for group in groups.values() if len(group) > 1]
        connected = sum(len(group) for group in clusters)
        percent = connected / len(rows) * 100 if rows else 0
        timestamps = sorted(_timestamp(row.bought_at) for row in rows)
        burst_seconds = (
            (timestamps[-1] - timestamps[0]).total_seconds() if len(timestamps) > 1 else None
        )
        coordinated = bool(connected >= 3 and burst_seconds is not None and burst_seconds <= 15)
        total = sum(float(row.amount_usd or 0) for row in rows)
        largest = max((float(row.amount_usd or 0) for row in rows), default=0)
        concentration = largest / total * 100 if total > 0 else None
        deployer_linked = sum(row.deployer_linked for row in rows)
        warnings = []
        if clusters:
            warnings.append("CONNECTED_BUYER_CLUSTER")
        if coordinated:
            warnings.append("COORDINATED_BUY_TIMING")
        if deployer_linked:
            warnings.append("DEPLOYER_LINKED_BUYERS")
        return WalletGraphResult(
            clusters,
            connected,
            percent,
            coordinated,
            deployer_linked,
            concentration,
            warnings,
            {
                "buyer_count": len(rows),
                "shared_funders": {
                    key: value for key, value in by_funder.items() if len(value) > 1
                },
                "burst_seconds": burst_seconds,
            },
        )


def creator_quality(launches: Iterable[dict[str, Any]]) -> dict[str, Any]:
    rows = list(launches)
    if not rows:
        return {"quality": CreatorQuality.UNKNOWN, "sample": 0, "reason": "NO_HISTORY"}
    rugs = sum(str(row.get("outcome", "")).upper() in {"RUG", "FAILED"} for row in rows)
    runners = sum(float(row.get("peak_multiple") or 0) >= 5 for row in rows)
    survived = sum(
        str(row.get("outcome", "")).upper() in {"ACTIVE", "SURVIVED", "RUNNER"} for row in rows
    )
    if rugs >= 2 and rugs / len(rows) >= 0.5:
        quality = CreatorQuality.TOXIC
    elif rugs:
        quality = CreatorQuality.SUSPICIOUS
    elif len(rows) >= 3 and runners >= 2:
        quality = CreatorQuality.PROVEN
    elif survived:
        quality = CreatorQuality.POSITIVE
    else:
        quality = CreatorQuality.NEUTRAL
    return {
        "quality": quality,
        "sample": len(rows),
        "rugs": rugs,
        "runners": runners,
        "survived": survived,
    }


def survival_engine(features: dict[str, Any]) -> dict[str, Any]:
    known = 0
    risk = 0
    reasons: list[str] = []
    if features.get("terminal_safety_failure") is True:
        return {
            "grade": SurvivalGrade.HIGH_RISK,
            "score": 0,
            "reasons": ["TERMINAL_SAFETY_FAILURE"],
        }
    liquidity = features.get("liquidity_usd")
    if liquidity is not None:
        known += 1
        if float(liquidity) < 5_000:
            risk += 2
            reasons.append("THIN_LIQUIDITY")
    connected = features.get("connected_wallet_percent")
    if connected is not None:
        known += 1
        if float(connected) >= 60:
            risk += 2
            reasons.append("CONNECTED_WALLET_DOMINANCE")
    creator = features.get("creator_quality")
    if creator not in (None, "UNKNOWN"):
        known += 1
        if creator in {CreatorQuality.TOXIC, CreatorQuality.SUSPICIOUS, "TOXIC", "SUSPICIOUS"}:
            risk += 2
            reasons.append("CREATOR_HISTORY_RISK")
    sell_pressure = features.get("sell_pressure")
    if sell_pressure is not None:
        known += 1
        if float(sell_pressure) >= 0.7:
            risk += 1
            reasons.append("SELL_PRESSURE")
    if not known:
        grade = SurvivalGrade.UNKNOWN
    elif risk >= 4:
        grade = SurvivalGrade.HIGH_RISK
    elif risk >= 2:
        grade = SurvivalGrade.WEAK
    elif risk == 1 or known < 3:
        grade = SurvivalGrade.ACCEPTABLE
    else:
        grade = SurvivalGrade.STRONG
    return {
        "grade": grade,
        "score": max(0, 100 - risk * 22),
        "reasons": reasons,
        "known_inputs": known,
    }


def payoff_engine(features: dict[str, Any], survival: SurvivalGrade | str) -> dict[str, Any]:
    entry = entry_state(features)
    if entry == EntryState.UNKNOWN or survival == SurvivalGrade.UNKNOWN:
        return {"grade": PayoffGrade.UNKNOWN, "score": None, "reasons": ["INSUFFICIENT_EVIDENCE"]}
    score = 50
    reasons: list[str] = []
    if entry == EntryState.VERY_EARLY:
        score += 30
        reasons.append("VERY_EARLY_ENTRY")
    elif entry == EntryState.EARLY:
        score += 20
        reasons.append("EARLY_ENTRY")
    elif entry in {EntryState.EXTENDED, EntryState.CHASING, EntryState.LATE}:
        score -= {EntryState.EXTENDED: 20, EntryState.CHASING: 35, EntryState.LATE: 50}[entry]
        reasons.append(f"{entry}_PENALTY")
    market_cap = features.get("market_cap_usd")
    if market_cap is not None and float(market_cap) <= 75_000:
        score += 10
        reasons.append("LOW_BASE_MARKET_CAP")
    if survival in {SurvivalGrade.STRONG, "STRONG"}:
        score += 10
    elif survival in {SurvivalGrade.HIGH_RISK, "HIGH_RISK"}:
        score -= 35
    grade = (
        PayoffGrade.EXCEPTIONAL
        if score >= 90
        else PayoffGrade.CONVEX
        if score >= 75
        else PayoffGrade.BALANCED
        if score >= 50
        else PayoffGrade.POOR
    )
    if grade == PayoffGrade.EXCEPTIONAL and survival not in {SurvivalGrade.STRONG, "STRONG"}:
        grade = PayoffGrade.CONVEX
    return {"grade": grade, "score": max(0, min(score, 100)), "reasons": reasons}


def promotion_decision(
    current: AlphaState | str,
    *,
    score: float,
    confidence: float,
    entry: EntryState | str,
    survival: SurvivalGrade | str,
    payoff: PayoffGrade | str,
    independent_pillars: int,
) -> AlphaState:
    """Monotonic state promotion; confidence can never hide a late/chasing entry."""
    current = AlphaState(current)
    entry = EntryState(entry)
    if current in {AlphaState.REJECTED_UNSAFE, AlphaState.EXPIRED, AlphaState.FAILED_OUTCOME}:
        return current
    if entry in {EntryState.CHASING, EntryState.LATE}:
        return max(current, AlphaState.STANDARD_RADAR, key=list(AlphaState).index)
    target = current
    if (
        score >= 85
        and confidence >= 0.75
        and independent_pillars >= 4
        and survival != SurvivalGrade.HIGH_RISK
        and payoff in {PayoffGrade.CONVEX, PayoffGrade.EXCEPTIONAL, "CONVEX", "EXCEPTIONAL"}
    ):
        target = AlphaState.QUALIFIED_SIGNAL
    elif score >= 75 and confidence >= 0.55 and independent_pillars >= 3:
        target = AlphaState.PRIORITY_RADAR
    elif score >= 65 and independent_pillars >= 2:
        target = AlphaState.HOT_RADAR
    elif score >= 55:
        target = AlphaState.STANDARD_RADAR
    return max(current, target, key=list(AlphaState).index)


def narrative_election(members: Iterable[dict[str, Any]]) -> dict[str, Any]:
    rows = sorted(members, key=lambda row: (_timestamp(row["detected_at"]), row["token_address"]))
    if not rows:
        return {"leader": None, "members": [], "saturation": "UNKNOWN"}
    leader = max(
        rows,
        key=lambda row: (
            float(row.get("traction") or 0) - float(row.get("clone_similarity") or 0) * 25,
            -_timestamp(row["detected_at"]).timestamp(),
        ),
    )
    enriched = []
    for index, row in enumerate(rows):
        role = "LEADER" if row is leader else "CHALLENGER" if index < 3 else "COPYCAT"
        enriched.append(
            {
                **row,
                "role": role,
                "clone_penalty": round(float(row.get("clone_similarity") or 0) * 30, 2),
            }
        )
    saturation = "SATURATED" if len(rows) >= 8 else "BUILDING" if len(rows) >= 3 else "FRESH"
    return {"leader": leader["token_address"], "members": enriched, "saturation": saturation}


def capital_rotation(previous: dict[str, float], current: dict[str, float]) -> list[dict[str, Any]]:
    changes = {
        key: current.get(key, 0) - previous.get(key, 0) for key in set(previous) | set(current)
    }
    donors = sorted(
        ((key, -value) for key, value in changes.items() if value < 0), key=lambda item: -item[1]
    )
    receivers = sorted(
        ((key, value) for key, value in changes.items() if value > 0), key=lambda item: -item[1]
    )
    rotations = []
    for (source, outflow), (target, inflow) in zip(donors, receivers):
        rotations.append({"from": source, "to": target, "strength": min(outflow, inflow)})
    return rotations


def right_tail_metrics(universe: Iterable[dict[str, Any]], min_sample: int = 30) -> dict[str, Any]:
    rows = list(universe)
    runners = {
        target: [row for row in rows if float(row.get("peak_multiple") or 0) >= target]
        for target in (2, 5, 10, 20)
    }
    selected = [
        row for row in rows if row.get("highest_tier") not in (None, "DISCOVERED", "SCREENING")
    ]
    qualified = [row for row in rows if row.get("highest_tier") == AlphaState.QUALIFIED_SIGNAL]
    qualified_2x = sum(float(row.get("peak_multiple") or 0) >= 2 for row in qualified)
    result: dict[str, Any] = {
        "universe_n": len(rows),
        "selected_n": len(selected),
        "qualified_n": len(qualified),
        "small_sample": len(qualified) < min_sample,
        "qualified_2x_precision": qualified_2x / len(qualified) * 100 if qualified else None,
    }
    for target, target_rows in runners.items():
        caught = sum(
            row.get("highest_tier") not in (None, "DISCOVERED", "SCREENING") for row in target_rows
        )
        result[f"runners_{target}x"] = len(target_rows)
        result[f"recall_{target}x"] = caught / len(target_rows) * 100 if target_rows else None
    result["missed_runners"] = [
        row.get("token_address")
        for row in runners[5]
        if row.get("highest_tier") in (None, "DISCOVERED", "SCREENING")
    ]
    return result


def evaluation_stage(age_seconds: float) -> str:
    if age_seconds < 30:
        return "T0"
    if age_seconds < 120:
        return "T+30S"
    if age_seconds < 300:
        return "T+2M"
    if age_seconds < 900:
        return "T+5M"
    return "LATER"


def enrichment_level(evidence: dict[str, Any]) -> int:
    """Return the highest completed staged-enrichment level (0 through 3)."""
    level = 0
    if evidence.get("market") is not None and evidence.get("safety") is not None:
        level = 1
    if (
        level >= 1
        and evidence.get("wallet_graph") is not None
        and evidence.get("creator") is not None
    ):
        level = 2
    if level >= 2 and evidence.get("narrative") is not None and evidence.get("social") is not None:
        level = 3
    return level


def provider_consensus(values: dict[str, Any]) -> dict[str, Any]:
    """Preserve disagreement instead of silently choosing a provider."""
    known = {provider: value for provider, value in values.items() if value is not None}
    if not known:
        return {"state": "UNKNOWN", "value": None, "providers": values}
    normalized = {json.dumps(value, sort_keys=True, default=str) for value in known.values()}
    if len(normalized) > 1:
        return {"state": "CONFLICTED", "value": None, "providers": known}
    return {"state": "OBSERVED", "value": next(iter(known.values())), "providers": known}


def buyer_cohort_metrics(buys: Iterable[WalletBuy]) -> list[dict[str, Any]]:
    rows = sorted(buys, key=lambda row: _timestamp(row.bought_at))
    metrics = []
    for size in (10, 25, 50, 100):
        cohort = rows[:size]
        if len(cohort) < size:
            metrics.append({"cohort_size": size, "state": "UNKNOWN", "observed": len(cohort)})
            continue
        sold = sum(float(row.sold_percent or 0) >= 90 for row in cohort)
        retained = sum(float(row.sold_percent or 0) < 50 for row in cohort)
        connected = sum(row.funder is not None for row in cohort)
        metrics.append(
            {
                "cohort_size": size,
                "state": "OBSERVED",
                "retained_count": retained,
                "sold_count": sold,
                "connected_count": connected,
            }
        )
    return metrics


def maximum_adverse_excursion(
    observations: Iterable[dict[str, Any]], call_price: float, before_multiple: float
) -> dict[str, Any]:
    ordered = sorted(observations, key=lambda row: _timestamp(str(row["observed_at"])))
    before = []
    target = call_price * before_multiple
    for row in ordered:
        price = float(row["price"])
        before.append(price)
        if price >= target:
            break
    minimum = min(before) if before else call_price
    return {
        "before_multiple": before_multiple,
        "maximum_adverse_excursion": max(0.0, 1 - minimum / call_price),
        "target_reached": bool(before and before[-1] >= target),
        "sample": len(before),
    }


def liquidity_quality(liquidity_usd: float | None, trade_size_usd: float = 1_000) -> dict[str, Any]:
    if liquidity_usd is None or liquidity_usd <= 0:
        return {"state": "UNKNOWN", "estimated_slippage_percent": None}
    reserve = liquidity_usd / 2
    slippage = trade_size_usd / (reserve + trade_size_usd) * 100
    state = "GOOD" if slippage <= 5 else "THIN" if slippage <= 15 else "POOR_EXITABILITY"
    return {
        "state": state,
        "estimated_slippage_percent": slippage,
        "trade_size_usd": trade_size_usd,
        "guaranteed_fill": False,
    }


def miss_analysis(row: dict[str, Any]) -> dict[str, Any]:
    if not row.get("discovered"):
        category = "DISCOVERY_MISS"
    elif row.get("provider_outage"):
        category = "PROVIDER_EVIDENCE_MISS"
    elif row.get("terminal_safety_failure"):
        category = "INTENTIONAL_SAFETY_REJECT"
    elif row.get("entry_state") in {"CHASING", "LATE"}:
        category = "LATE_DISCOVERY"
    elif float(row.get("score") or 0) < float(row.get("threshold") or 0):
        category = "THRESHOLD_FALSE_NEGATIVE"
    else:
        category = "UNEXPLAINED_FALSE_NEGATIVE"
    return {"category": category, "evidence": row, "auditable": True}


def filter_as_of(observations: Iterable[dict[str, Any]], as_of: str) -> list[dict[str, Any]]:
    """Replay boundary used by explicit anti-lookahead tests."""
    boundary = _timestamp(as_of)
    return [row for row in observations if _timestamp(str(row["observed_at"])) <= boundary]


async def parallel_enrichment(
    providers: dict[str, Callable[[], Awaitable[Any]]], timeout_seconds: float = 10
) -> dict[str, dict[str, Any]]:
    async def invoke(name: str, call: Callable[[], Awaitable[Any]]) -> tuple[str, dict[str, Any]]:
        started = datetime.now(UTC)
        try:
            value = await asyncio.wait_for(call(), timeout_seconds)
            return name, {
                "state": "HEALTHY",
                "value": value,
                "latency_ms": (datetime.now(UTC) - started).total_seconds() * 1000,
            }
        except TimeoutError:
            return name, {"state": "DOWN", "value": None, "error": "TIMEOUT"}
        except Exception as exc:  # noqa: BLE001 - optional providers are isolated by design
            return name, {"state": "DEGRADED", "value": None, "error": str(exc)}

    return dict(await asyncio.gather(*(invoke(name, call) for name, call in providers.items())))


class BoundedLaunchQueue:
    """Bounded, deduplicating event queue with fresh-first backpressure behavior."""

    def __init__(self, maxsize: int = 2_048):
        if maxsize <= 0:
            raise ValueError("maxsize must be positive")
        self.queue: asyncio.Queue[LaunchEvent] = asyncio.Queue(maxsize=maxsize)
        self._seen: set[str] = set()
        self.accepted = 0
        self.duplicates = 0
        self.dropped = 0

    def offer(self, event: LaunchEvent) -> str:
        if event.event_key in self._seen:
            self.duplicates += 1
            return "DUPLICATE"
        self._seen.add(event.event_key)
        if self.queue.full():
            self.dropped += 1
            self._seen.remove(event.event_key)
            return "BACKPRESSURE"
        self.queue.put_nowait(event)
        self.accepted += 1
        return "QUEUED"

    def task_done(self, event: LaunchEvent) -> None:
        self.queue.task_done()
        self._seen.discard(event.event_key)

    def stats(self) -> dict[str, int]:
        return {
            "size": self.queue.qsize(),
            "maxsize": self.queue.maxsize,
            "accepted": self.accepted,
            "duplicates": self.duplicates,
            "dropped": self.dropped,
        }


def decision_payload(decision: AlphaDecision) -> dict[str, Any]:
    value = asdict(decision)
    return json.loads(json.dumps(value, default=str))
