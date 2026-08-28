from __future__ import annotations

import json
import math
import statistics
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import Any

from memecoin_bot.models import iso

FEATURE_VERSION = "realtime-trajectory-v1"
WINDOW_BANDS = (
    (0, 15),
    (15, 30),
    (30, 60),
    (60, 90),
    (90, 120),
    (120, 180),
    (180, 300),
    (300, 600),
    (600, 1_800),
)


def _json(value: Any) -> str:
    return json.dumps(value, default=str, separators=(",", ":"), sort_keys=True)


def _loads(value: str | None) -> dict[str, Any]:
    try:
        return json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp must include timezone: {value}")
    return parsed.astimezone(UTC)


def _ratio(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator else None


def _quantile(values: list[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = (len(ordered) - 1) * probability
    low = int(rank)
    high = min(len(ordered) - 1, low + 1)
    fraction = rank - low
    return ordered[low] * (1 - fraction) + ordered[high] * fraction


@dataclass(frozen=True, slots=True)
class _Trade:
    timestamp: datetime
    actor: str
    side: str
    quote_amount: float
    token_amount: float | None
    transaction: str | None
    slot: str | None
    creator_linked: bool | None
    funder: str | None
    cluster: str | None
    likely_bundled: bool | None


@dataclass(frozen=True, slots=True)
class _Curve:
    timestamp: datetime
    real_sol: float | None
    real_quote_raw: int | None
    real_token_raw: int | None
    virtual_sol: float | None
    progress: float | None


class RealtimeFeatureProjector:
    """Point-in-time trajectory, buyer-arrival, sell, wash, and temperature features."""

    def __init__(self, store: Any):
        self.store = store

    def latest(self, token_id: int, available_at: str) -> dict[str, Any] | None:
        row = self.store.conn.execute(
            "SELECT feature_json,available_timestamp FROM trajectory_feature_snapshots_v15 "
            "WHERE token_id=? AND available_timestamp<=? ORDER BY decision_timestamp DESC LIMIT 1",
            (token_id, available_at),
        ).fetchone()
        if not row:
            return None
        value = _loads(row["feature_json"])
        value["available_timestamp"] = row["available_timestamp"]
        return value

    def compute(self, token_id: int, decision_timestamp: str | None = None) -> dict[str, Any]:
        decision_timestamp = decision_timestamp or iso()
        decision = _timestamp(decision_timestamp)
        state = self.store.conn.execute(
            "SELECT * FROM token_realtime_state WHERE token_id=?", (token_id,)
        ).fetchone()
        if not state:
            raise KeyError(f"token {token_id} has no realtime state")
        launched = _timestamp(str(state["launched_at"]))
        if launched > decision:
            raise ValueError("launch is after feature decision time")
        trades = self._trades(token_id, decision)
        curves = self._curves(token_id, decision)
        creator = str(state["creator_address"] or "")
        components, linkage_coverage = self._wallet_components(token_id, trades)
        wash = self._wash_evidence(trades, creator, components)
        buyer = self._buyer_features(trades, launched, decision, creator, components)
        selling = self._sell_features(trades, curves, launched, decision, creator)
        capital = self._capital_features(trades, curves, launched, decision)
        migration = self._migration_continuity(token_id, trades, curves, decision)
        windows = self._window_features(trades, curves, launched, decision, components, wash)
        coverage = {
            "trade_events": bool(trades),
            "curve_observations": bool(curves),
            "real_sol_reserve": any(row.real_sol is not None for row in curves),
            "buyer_identity": any(bool(row.actor) for row in trades),
            "wallet_linkage": linkage_coverage,
            "creator": bool(creator),
            "funder": any(row.funder for row in trades),
            "bundle": any(row.likely_bundled is not None for row in trades),
            "first_sell": any(row.side == "sell" for row in trades),
            "migration": str(state["migration_state"]) != "PRE_MIGRATION",
            "provider_timestamps": bool(trades or curves),
        }
        evidence_mode = (
            "LIVE_NATIVE"
            if curves and any(row.real_sol is not None for row in curves)
            else "LIVE_EVENT_ONLY"
            if trades
            else "UNAVAILABLE"
        )
        feature = {
            "feature_version": FEATURE_VERSION,
            "decision_timestamp": decision_timestamp,
            "launched_at": launched.isoformat(),
            "token_age_seconds": max(0.0, (decision - launched).total_seconds()),
            "evidence_mode": evidence_mode,
            "windows": windows,
            "buyer_arrival": buyer,
            "first_sell": selling,
            "capital_trajectory": capital,
            "activity_adjustment": wash,
            "migration_state": state["migration_state"],
            "migration_continuity": migration,
            "coverage": coverage,
        }
        temperature = self._temperature(feature)
        feature["monitoring"] = temperature
        now = iso()
        with self.store._lock, self.store.conn:
            self.store.conn.execute(
                "INSERT INTO trajectory_feature_snapshots_v15(token_id,decision_timestamp,"
                "available_timestamp,feature_version,evidence_mode,feature_json,coverage_json) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(token_id,decision_timestamp,feature_version) "
                "DO UPDATE SET available_timestamp=excluded.available_timestamp,feature_json=excluded.feature_json,"
                "coverage_json=excluded.coverage_json",
                (
                    token_id,
                    decision_timestamp,
                    now,
                    FEATURE_VERSION,
                    evidence_mode,
                    _json(feature),
                    _json(coverage),
                ),
            )
            self.store.conn.execute(
                "INSERT INTO activity_evidence_v15(token_id,observed_at,raw_buyers,adjusted_buyers,"
                "raw_volume,adjusted_volume,raw_net_flow,adjusted_net_flow,linked_wallet_share,"
                "bundle_linked_share,wash_probability,evidence_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(token_id,observed_at) DO UPDATE SET evidence_json=excluded.evidence_json",
                (
                    token_id,
                    decision_timestamp,
                    buyer["raw_buyers"],
                    buyer["adjusted_independent_buyers"],
                    wash["raw_volume_sol"],
                    wash["adjusted_volume_sol"],
                    wash["raw_net_flow_sol"],
                    wash["adjusted_net_flow_sol"],
                    wash["linked_wallet_share"],
                    wash["bundle_linked_share"],
                    wash["wash_probability"],
                    _json(wash),
                ),
            )
            next_monitor = (
                (decision + timedelta(seconds=temperature["interval_seconds"])).isoformat()
                if temperature["interval_seconds"] is not None
                else None
            )
            self.store.conn.execute(
                "UPDATE token_realtime_state SET monitoring_temperature=?,updated_at=? WHERE token_id=?",
                (temperature["state"], now, token_id),
            )
            self.store.conn.execute(
                "UPDATE candidates SET monitoring_temperature=?,realtime_priority=?,next_monitor_at=? "
                "WHERE token_id=?",
                (
                    temperature["state"],
                    temperature["priority"],
                    next_monitor,
                    token_id,
                ),
            )
        return feature

    def _migration_continuity(
        self,
        token_id: int,
        trades: list[_Trade],
        curves: list[_Curve],
        decision: datetime,
    ) -> dict[str, Any]:
        row = self.store.conn.execute(
            "SELECT * FROM migration_continuity_v15 WHERE token_id=?", (token_id,)
        ).fetchone()
        if not row or not row["migration_timestamp"]:
            return {
                "state": "UNKNOWN_NO_MIGRATION_EVENT",
                "liquidity_continuity": None,
                "flow_survival": None,
                "buyer_retention": None,
                "sell_shock": None,
            }
        migration = _timestamp(str(row["migration_timestamp"]))
        if migration > decision:
            return {
                "state": "NOT_AVAILABLE_AT_DECISION",
                "migration_timestamp": migration.isoformat(),
                "liquidity_continuity": None,
                "flow_survival": None,
                "buyer_retention": None,
                "sell_shock": None,
            }
        pre_start = migration - timedelta(seconds=60)
        post_end = min(decision, migration + timedelta(seconds=60))
        pre_trades = [row for row in trades if pre_start <= row.timestamp <= migration]
        post_trades = [row for row in trades if migration < row.timestamp <= post_end]
        pre_curves = [row for row in curves if pre_start <= row.timestamp <= migration]
        post_curves = [row for row in curves if migration < row.timestamp <= post_end]
        pre_buyers = {row.actor for row in pre_trades if row.side == "buy"}
        post_buyers = {row.actor for row in post_trades if row.side == "buy"}
        pre_buy = sum(row.quote_amount for row in pre_trades if row.side == "buy")
        post_buy = sum(row.quote_amount for row in post_trades if row.side == "buy")
        post_sell = sum(row.quote_amount for row in post_trades if row.side == "sell")
        pre_reserve = next(
            (row.real_sol for row in reversed(pre_curves) if row.real_sol is not None), None
        )
        post_reserve = next(
            (row.real_sol for row in post_curves if row.real_sol is not None), None
        )
        liquidity = (
            _ratio(float(post_reserve), float(pre_reserve))
            if pre_reserve not in (None, 0) and post_reserve is not None
            else None
        )
        flow = _ratio(post_buy, pre_buy)
        retention = _ratio(len(pre_buyers & post_buyers), len(pre_buyers))
        shock = _ratio(post_sell, post_buy + post_sell)
        measured = sum(value is not None for value in (liquidity, flow, retention, shock))
        result = {
            "state": "MEASURED" if measured >= 2 else "PARTIAL_COVERAGE",
            "migration_timestamp": migration.isoformat(),
            "window_seconds": 60,
            "pre_trade_count": len(pre_trades),
            "post_trade_count": len(post_trades),
            "pre_curve_states": len(pre_curves),
            "post_curve_states": len(post_curves),
            "liquidity_continuity": liquidity,
            "flow_survival": flow,
            "buyer_retention": retention,
            "sell_shock": shock,
            "point_in_time": True,
        }
        with self.store._lock, self.store.conn:
            self.store.conn.execute(
                "UPDATE migration_continuity_v15 SET pre_migration_json=?,post_migration_json=?,"
                "liquidity_continuity=?,flow_survival=?,buyer_retention=?,sell_shock=?,updated_at=? "
                "WHERE token_id=?",
                (
                    _json(
                        {
                            "trade_count": len(pre_trades),
                            "buy_sol": pre_buy,
                            "buyers": len(pre_buyers),
                            "real_sol": pre_reserve,
                        }
                    ),
                    _json(
                        {
                            "trade_count": len(post_trades),
                            "buy_sol": post_buy,
                            "sell_sol": post_sell,
                            "buyers": len(post_buyers),
                            "real_sol": post_reserve,
                        }
                    ),
                    liquidity,
                    flow,
                    retention,
                    shock,
                    iso(),
                    token_id,
                ),
            )
        return result

    def _trades(self, token_id: int, decision: datetime) -> list[_Trade]:
        rows = self.store.conn.execute(
            "SELECT * FROM token_event_timeline_v15 WHERE token_id=? AND event_type='TOKEN_TRADE' "
            "AND available_timestamp<=? ORDER BY event_timestamp,event_id",
            (token_id, decision.isoformat()),
        )
        output = []
        for row in rows:
            timestamp = _timestamp(str(row["event_timestamp"]))
            if timestamp > decision or row["side"] not in ("buy", "sell"):
                continue
            output.append(
                _Trade(
                    timestamp=timestamp,
                    actor=str(row["actor"] or "UNKNOWN"),
                    side=str(row["side"]),
                    quote_amount=max(0.0, float(row["quote_amount"] or 0)),
                    token_amount=(
                        max(0.0, float(row["token_amount"]))
                        if row["token_amount"] is not None
                        else None
                    ),
                    transaction=row["transaction_signature"],
                    slot=row["slot_or_block"],
                    creator_linked=(
                        bool(row["creator_linked"])
                        if row["creator_linked"] is not None
                        else None
                    ),
                    funder=row["funder"],
                    cluster=row["wallet_cluster"],
                    likely_bundled=(
                        bool(row["likely_bundled"])
                        if row["likely_bundled"] is not None
                        else None
                    ),
                )
            )
        return output

    def _curves(self, token_id: int, decision: datetime) -> list[_Curve]:
        rows = self.store.conn.execute(
            "SELECT * FROM curve_observations_v15 WHERE token_id=? AND available_at<=? "
            "ORDER BY observed_at,event_id",
            (token_id, decision.isoformat()),
        )
        output = []
        for row in rows:
            timestamp = _timestamp(str(row["observed_at"]))
            if timestamp > decision:
                continue
            output.append(
                _Curve(
                    timestamp,
                    (
                        float(row["real_sol_reserves"]) / 1_000_000_000
                        if row["real_sol_reserves"] is not None
                        else None
                    ),
                    int(row["real_quote_reserves"])
                    if row["real_quote_reserves"] is not None
                    else None,
                    int(row["real_token_reserves"])
                    if row["real_token_reserves"] is not None
                    else None,
                    (
                        float(row["virtual_sol_reserves"]) / 1_000_000_000
                        if row["virtual_sol_reserves"] is not None
                        else None
                    ),
                    float(row["curve_progress"])
                    if row["curve_progress"] is not None
                    else None,
                )
            )
        return output

    def _wallet_components(
        self, token_id: int, trades: list[_Trade]
    ) -> tuple[dict[str, str], bool]:
        actors = {row.actor for row in trades if row.actor != "UNKNOWN"}
        groups: dict[str, set[str]] = {}
        evidence = False
        for row in trades:
            if row.actor == "UNKNOWN":
                continue
            if row.cluster:
                groups.setdefault(f"cluster:{row.cluster}", set()).add(row.actor)
                evidence = True
            if row.funder:
                groups.setdefault(f"funder:{row.funder}", set()).add(row.actor)
                evidence = True
        token = self.store.conn.execute(
            "SELECT chain FROM tokens WHERE id=?", (token_id,)
        ).fetchone()
        chain = str(token[0]) if token else "solana"
        if actors:
            placeholders = ",".join("?" for _ in actors)
            rows = self.store.conn.execute(
                f"SELECT funded_wallet,funder_wallet FROM wallet_funding_edges_v15 "
                f"WHERE chain=? AND funded_wallet IN ({placeholders})",
                (chain, *sorted(actors)),
            )
            for row in rows:
                groups.setdefault(f"funder:{row['funder_wallet']}", set()).add(
                    str(row["funded_wallet"])
                )
                evidence = True
        parent = {actor: actor for actor in actors}

        def find(value: str) -> str:
            while parent[value] != value:
                parent[value] = parent[parent[value]]
                value = parent[value]
            return value

        def union(left: str, right: str) -> None:
            left_root, right_root = find(left), find(right)
            if left_root != right_root:
                parent[right_root] = left_root

        for members in groups.values():
            ordered = sorted(members)
            for member in ordered[1:]:
                union(ordered[0], member)
        return ({actor: find(actor) for actor in actors} if evidence else {}), evidence

    def _buyer_features(
        self,
        trades: list[_Trade],
        launched: datetime,
        decision: datetime,
        creator: str,
        components: dict[str, str],
    ) -> dict[str, Any]:
        buys = [row for row in trades if row.side == "buy"]
        sells = [row for row in trades if row.side == "sell"]
        buyers = {row.actor for row in buys if row.actor != "UNKNOWN"}
        known_components = {components[row.actor] for row in buys if row.actor in components}
        unknown_linkage = {row.actor for row in buys if row.actor not in components and row.actor != "UNKNOWN"}
        first = {row.actor for row in buys if (row.timestamp - launched).total_seconds() <= 15}
        second = {
            row.actor
            for row in buys
            if 15 < (row.timestamp - launched).total_seconds() <= 30
        }
        repeat = {actor for actor in buyers if sum(row.actor == actor for row in buys) > 1}
        sold = {row.actor for row in sells}
        sizes = [row.quote_amount for row in buys]
        total = sum(sizes)
        largest = max(sizes, default=0.0)
        duration = max(1.0, (decision - launched).total_seconds())
        velocities = []
        for start, end in WINDOW_BANDS:
            count = len(
                {
                    row.actor
                    for row in buys
                    if start < max(0.0, (row.timestamp - launched).total_seconds()) <= end
                }
            )
            velocities.append(count / (end - start))
        acceleration = [
            (right - left) / max(1, WINDOW_BANDS[index + 1][1] - WINDOW_BANDS[index + 1][0])
            for index, (left, right) in enumerate(pairwise(velocities))
        ]
        return {
            "raw_buyers": len(buyers),
            "adjusted_independent_buyers": (
                len(known_components | unknown_linkage) if components else None
            ),
            "linkage_state": "OBSERVED_GRAPH" if components else "UNKNOWN",
            "unknown_linkage_share": _ratio(len(unknown_linkage), len(buyers)),
            "new_buyers_per_second": len(buyers) / duration,
            "independent_new_buyers_per_second": (
                len(known_components | unknown_linkage) / duration if components else None
            ),
            "buyer_velocity_by_band": velocities,
            "buyer_acceleration_by_band": acceleration,
            "buyer_deceleration_observed": any(value < 0 for value in acceleration),
            "buyer_retention": _ratio(len(first & repeat), len(first)),
            "repeat_buyer_share": _ratio(len(repeat), len(buyers)),
            "first_buyer_cohort_size": len(first),
            "second_cohort_growth": len(second) - len(first),
            "buyer_replacement": len(second - first),
            "buyer_churn": _ratio(len(first & sold), len(first)),
            "whale_share": _ratio(largest, total),
            "median_buy_size_sol": statistics.median(sizes) if sizes else None,
            "buy_size_distribution_sol": {
                "p10": _quantile(sizes, 0.1),
                "p50": _quantile(sizes, 0.5),
                "p90": _quantile(sizes, 0.9),
                "max": max(sizes) if sizes else None,
            },
            "new_buyer_to_seller_ratio": _ratio(len(buyers), len(sold)),
            "creator_buyer_share": _ratio(
                sum(row.quote_amount for row in buys if row.actor == creator), total
            ),
        }

    def _sell_features(
        self,
        trades: list[_Trade],
        curves: list[_Curve],
        launched: datetime,
        decision: datetime,
        creator: str,
    ) -> dict[str, Any]:
        sells = [row for row in trades if row.side == "sell"]
        first = sells[0] if sells else None
        prior_buy = 0.0
        meaningful = None
        for row in trades:
            if row.side == "buy":
                prior_buy += row.quote_amount
            elif row.quote_amount >= max(0.05, prior_buy * 0.05):
                meaningful = row
                break
        after = [row for row in trades if meaningful and row.timestamp > meaningful.timestamp]
        after_buys = [row for row in after if row.side == "buy"]
        after_sells = [row for row in after if row.side == "sell"]
        reserve_before = self._nearest_curve(curves, meaningful.timestamp, before=True) if meaningful else None
        reserve_after = self._nearest_curve(curves, decision, before=True) if meaningful else None
        recovery = None
        if reserve_before and reserve_after and reserve_before.real_sol is not None and reserve_after.real_sol is not None:
            recovery = reserve_after.real_sol - reserve_before.real_sol
        sell_velocities = []
        for start, end in WINDOW_BANDS:
            count = sum(
                start < max(0.0, (row.timestamp - launched).total_seconds()) <= end
                for row in sells
            )
            sell_velocities.append(count / (end - start))
        return {
            "time_to_first_sell_seconds": (
                max(0.0, (first.timestamp - launched).total_seconds()) if first else None
            ),
            "time_to_first_meaningful_sell_seconds": (
                max(0.0, (meaningful.timestamp - launched).total_seconds()) if meaningful else None
            ),
            "first_sell_size_sol": first.quote_amount if first else None,
            "first_sell_actor": first.actor if first else None,
            "creator_first_sell": first.actor == creator if first and creator else None,
            "first_meaningful_sell_size_sol": meaningful.quote_amount if meaningful else None,
            "sell_velocity_per_second": len(sells)
            / max(1.0, (decision - launched).total_seconds()),
            "sell_velocity_by_band": sell_velocities,
            "sell_acceleration_by_band": [
                right - left for left, right in pairwise(sell_velocities)
            ],
            "buyers_after_first_meaningful_sell": len({row.actor for row in after_buys}),
            "buy_sol_after_first_meaningful_sell": sum(row.quote_amount for row in after_buys),
            "sell_sol_after_first_meaningful_sell": sum(row.quote_amount for row in after_sells),
            "sell_absorption_rate": _ratio(
                sum(row.quote_amount for row in after_buys),
                sum(row.quote_amount for row in after_sells),
            ),
            "real_sol_recovery_after_first_sell": recovery,
            "first_sell_absorbed": (
                recovery > 0 and len(after_buys) > 0 if recovery is not None else None
            ),
        }

    @staticmethod
    def _nearest_curve(
        curves: list[_Curve], timestamp: datetime, *, before: bool
    ) -> _Curve | None:
        rows = [row for row in curves if row.timestamp <= timestamp] if before else curves
        return rows[-1] if rows else None

    def _capital_features(
        self, trades: list[_Trade], curves: list[_Curve], launched: datetime, decision: datetime
    ) -> dict[str, Any]:
        real = [row for row in curves if row.real_sol is not None]
        velocities: list[float] = []
        for left, right in pairwise(real):
            elapsed = (right.timestamp - left.timestamp).total_seconds()
            if elapsed > 0:
                velocities.append((float(right.real_sol) - float(left.real_sol)) / elapsed)
        accelerations: list[float] = []
        if len(velocities) >= 2:
            for index in range(1, len(velocities)):
                elapsed = (real[index + 1].timestamp - real[index].timestamp).total_seconds()
                if elapsed > 0:
                    accelerations.append((velocities[index] - velocities[index - 1]) / elapsed)
        jerks: list[float] = []
        if len(accelerations) >= 2:
            for index in range(1, len(accelerations)):
                elapsed = (real[index + 2].timestamp - real[index + 1].timestamp).total_seconds()
                if elapsed > 0:
                    jerks.append((accelerations[index] - accelerations[index - 1]) / elapsed)
        progress = [row for row in curves if row.progress is not None]
        progress_velocities = []
        for left, right in pairwise(progress):
            elapsed = (right.timestamp - left.timestamp).total_seconds()
            if elapsed > 0:
                progress_velocities.append(
                    (float(right.progress) - float(left.progress)) / elapsed
                )
        progress_accelerations = []
        for index in range(1, len(progress_velocities)):
            elapsed = (progress[index + 1].timestamp - progress[index].timestamp).total_seconds()
            if elapsed > 0:
                progress_accelerations.append(
                    (progress_velocities[index] - progress_velocities[index - 1]) / elapsed
                )
        milestones = {}
        for threshold in (5, 10, 20, 30):
            hit = next((row for row in real if float(row.real_sol) >= threshold), None)
            prior_trades = [row for row in trades if hit and row.timestamp <= hit.timestamp]
            milestones[str(threshold)] = {
                "seconds": max(0.0, (hit.timestamp - launched).total_seconds()) if hit else None,
                "trades": len(prior_trades) if hit else None,
            }
        values = [float(row.real_sol) for row in real]
        current = values[-1] if values else None
        peak = max(values) if values else None
        return {
            "real_sol_reserve": current,
            "real_sol_delta": values[-1] - values[0] if len(values) >= 2 else None,
            "real_sol_velocity": velocities[-1] if velocities else None,
            "real_sol_acceleration": accelerations[-1] if accelerations else None,
            "real_sol_jerk": jerks[-1] if jerks else None,
            "derivative_support": {
                "states": len(real),
                "velocity_intervals": len(velocities),
                "acceleration_intervals": len(accelerations),
                "jerk_intervals": len(jerks),
            },
            "curve_progress": progress[-1].progress if progress else None,
            "curve_progress_velocity": progress_velocities[-1] if progress_velocities else None,
            "curve_progress_acceleration": (
                progress_accelerations[-1] if progress_accelerations else None
            ),
            "sol_per_trade": _ratio(current or 0, len(trades)) if current is not None else None,
            "sol_per_new_buyer": _ratio(
                current or 0,
                len({row.actor for row in trades if row.side == "buy"}),
            )
            if current is not None
            else None,
            "milestones": milestones,
            **{
                f"seconds_to_{threshold}_sol": milestones[str(threshold)]["seconds"]
                for threshold in (5, 10, 20, 30)
            },
            **{
                f"trades_to_{threshold}_sol": milestones[str(threshold)]["trades"]
                for threshold in (5, 10, 20, 30)
            },
            "capital_persistence": _ratio(sum(value > 0 for value in velocities), len(velocities)),
            "capital_reversal": any(value < 0 for value in velocities),
            "capital_drawdown_sol": peak - current if peak is not None and current is not None else None,
            "peak_real_sol": peak,
            "time_to_peak_seconds": (
                max(0.0, (real[values.index(peak)].timestamp - launched).total_seconds())
                if peak is not None
                else None
            ),
            "observation_age_seconds": max(0.0, (decision - launched).total_seconds()),
        }

    def _wash_evidence(
        self, trades: list[_Trade], creator: str, components: dict[str, str]
    ) -> dict[str, Any]:
        by_actor: dict[str, list[_Trade]] = {}
        for row in trades:
            by_actor.setdefault(row.actor, []).append(row)
        recycled: set[tuple[str | None, str]] = set()
        for actor, rows in by_actor.items():
            for left, right in pairwise(rows):
                if left.side != right.side and (right.timestamp - left.timestamp).total_seconds() <= 15:
                    recycled.add((right.transaction, actor))
        size_counts: dict[float, int] = {}
        for row in trades:
            rounded = round(row.quote_amount, 6)
            size_counts[rounded] = size_counts.get(rounded, 0) + 1
        repeated_sizes = {size for size, count in size_counts.items() if count >= 3 and size > 0}
        flagged = [
            row
            for row in trades
            if (row.transaction, row.actor) in recycled
            or round(row.quote_amount, 6) in repeated_sizes
            or (creator and row.actor == creator)
        ]
        raw_volume = sum(row.quote_amount for row in trades)
        adjusted = max(0.0, raw_volume - sum(row.quote_amount for row in flagged))
        raw_net = sum(
            row.quote_amount if row.side == "buy" else -row.quote_amount for row in trades
        )
        adjusted_net = sum(
            row.quote_amount if row.side == "buy" else -row.quote_amount
            for row in trades
            if row not in flagged
        )
        actors = {row.actor for row in trades}
        linked = len(actors) - len({components.get(actor, actor) for actor in actors})
        bundled = [row for row in trades if row.likely_bundled]
        probability = min(
            1.0,
            0.45 * (_ratio(len(flagged), len(trades)) or 0)
            + 0.25 * (_ratio(linked, len(actors)) or 0)
            + 0.30 * (_ratio(len(bundled), len(trades)) or 0),
        )
        return {
            "raw_volume_sol": raw_volume,
            "adjusted_volume_sol": adjusted,
            "raw_net_flow_sol": raw_net,
            "adjusted_net_flow_sol": adjusted_net,
            "raw_trade_count": len(trades),
            "adjusted_trade_count": len(trades) - len(flagged),
            "rapid_recycle_events": len(recycled),
            "repeated_size_values": sorted(repeated_sizes),
            "creator_linked_events": sum(row.actor == creator for row in trades) if creator else None,
            "linked_wallet_share": _ratio(linked, len(actors)) if components else None,
            "bundle_linked_share": _ratio(len(bundled), len(trades)),
            "wash_probability": probability,
            "wash_state": "HIGH" if probability >= 0.6 else "MEDIUM" if probability >= 0.3 else "LOW",
            "method": "probabilistic_event_heuristics_not_identity_proof",
        }

    def _window_features(
        self,
        trades: list[_Trade],
        curves: list[_Curve],
        launched: datetime,
        decision: datetime,
        components: dict[str, str],
        wash: dict[str, Any],
    ) -> dict[str, Any]:
        output: dict[str, Any] = {}
        for start, end in WINDOW_BANDS:
            available_end = min(decision, launched + timedelta(seconds=end))
            if available_end <= launched + timedelta(seconds=start):
                continue
            rows = [
                row
                for row in trades
                if start < max(0.0, (row.timestamp - launched).total_seconds()) <= end
            ]
            curve_rows = [
                row
                for row in curves
                if start < max(0.0, (row.timestamp - launched).total_seconds()) <= end
            ]
            buys = [row for row in rows if row.side == "buy"]
            sells = [row for row in rows if row.side == "sell"]
            buy_sol, sell_sol = (
                sum(row.quote_amount for row in buys),
                sum(row.quote_amount for row in sells),
            )
            buyers = {row.actor for row in buys}
            independent = {components.get(actor, actor) for actor in buyers} if components else None
            first_reserve = next((row.real_sol for row in curve_rows if row.real_sol is not None), None)
            last_reserve = next(
                (row.real_sol for row in reversed(curve_rows) if row.real_sol is not None), None
            )
            duration = end - start
            output[f"{start}-{end}"] = {
                "native_observation": bool(rows or curve_rows),
                "trade_count": len(rows),
                "buyer_count": len(buyers),
                "independent_buyer_count": len(independent) if independent is not None else None,
                "buyer_velocity": len(buyers) / duration,
                "buy_sol": buy_sol,
                "sell_sol": sell_sol,
                "net_flow_sol": buy_sol - sell_sol,
                "net_flow_velocity": (buy_sol - sell_sol) / duration,
                "sell_pressure": _ratio(sell_sol, buy_sol + sell_sol),
                "repeat_buyer_share": _ratio(
                    len({actor for actor in buyers if sum(row.actor == actor for row in buys) > 1}),
                    len(buyers),
                ),
                "bundle_linked_share": _ratio(
                    sum(bool(row.likely_bundled) for row in rows), len(rows)
                ),
                "real_sol_start": first_reserve,
                "real_sol_end": last_reserve,
                "real_sol_change": (
                    last_reserve - first_reserve
                    if first_reserve is not None and last_reserve is not None
                    else None
                ),
                "wash_probability_global_to_decision": wash["wash_probability"],
            }
        return output

    @staticmethod
    def _temperature(feature: dict[str, Any]) -> dict[str, Any]:
        age = float(feature["token_age_seconds"])
        buyer = feature["buyer_arrival"]
        capital = feature["capital_trajectory"]
        velocity = capital.get("real_sol_velocity")
        buyer_velocity = buyer.get("new_buyers_per_second") or 0.0
        if age <= 120:
            state, interval, priority = "GENESIS", 1.0, 100.0
        elif (velocity is not None and velocity >= 0.02) or buyer_velocity >= 0.1:
            state, interval, priority = "HOT", 2.0, 90.0
        elif age <= 1_800 and (feature["windows"] or feature["capital_trajectory"]["real_sol_reserve"] is not None):
            state, interval, priority = "WARM", 10.0, 60.0
        elif age > 10_800:
            state, interval, priority = "DEAD", None, 0.0
        else:
            state, interval, priority = "COLD", 60.0, 20.0
        return {
            "state": state,
            "interval_seconds": interval,
            "priority": priority,
            "reason": {
                "age_seconds": age,
                "real_sol_velocity": velocity,
                "new_buyers_per_second": buyer_velocity,
            },
        }


def latency_distribution(values: list[float]) -> dict[str, float | int | None]:
    finite = [float(value) for value in values if math.isfinite(float(value)) and value >= 0]
    return {
        "count": len(finite),
        "p50_ms": _quantile(finite, 0.50),
        "p90_ms": _quantile(finite, 0.90),
        "p95_ms": _quantile(finite, 0.95),
        "p99_ms": _quantile(finite, 0.99),
        "max_ms": max(finite) if finite else None,
    }
