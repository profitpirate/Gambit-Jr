from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from itertools import pairwise
from typing import Any

from memecoin_bot.models import iso
from memecoin_bot.realtime.events import CanonicalEvent, CanonicalEventType

STATE_VERSION = "incremental-token-state-v2"
WINDOW_BANDS = (
    (0, 15),
    (15, 30),
    (30, 60),
    (60, 90),
    (90, 120),
    (120, 180),
    (180, 300),
    (300, 600),
)
CAPITAL_MILESTONES = (5, 10, 20, 30)


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp must include timezone: {value}")
    return parsed.astimezone(UTC)


def _ratio(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator else None


def _band(age: float) -> str | None:
    for start, end in WINDOW_BANDS:
        if start <= age <= end:
            return f"{start}-{end}"
    return None


def _new_state(launched_at: str) -> dict[str, Any]:
    return {
        "launched_at": launched_at,
        "event_count": 0,
        "trade_count": 0,
        "buy_count": 0,
        "sell_count": 0,
        "buy_sol": 0.0,
        "sell_sol": 0.0,
        "unique_buyer_count": 0,
        "repeat_buyer_count": 0,
        "unique_seller_count": 0,
        "post_sell_unique_buyer_count": 0,
        "largest_buyer_sol": 0.0,
        "sequence": {
            f"{start}-{end}": {
                "trade_count": 0,
                "buy_count": 0,
                "sell_count": 0,
                "buy_sol": 0.0,
                "sell_sol": 0.0,
                "new_buyer_count": 0,
            }
            for start, end in WINDOW_BANDS
        },
        "first_meaningful_sell": None,
        "second_meaningful_sell": None,
        "meaningful_sell_count": 0,
        "post_sell_buy_sol": 0.0,
        "post_sell_sell_sol": 0.0,
        "post_second_sell_buy_sol": 0.0,
        "post_sell_responses": {
            str(value): {"buy_sol": 0.0, "sell_sol": 0.0, "buyer_count": 0}
            for value in (5, 10, 20, 30)
        },
        "capital_net_sol": 0.0,
        "capital_peak_sol": 0.0,
        "capital_previous_sol": None,
        "capital_velocity": 0.0,
        "capital_acceleration": 0.0,
        "capital_milestones": {},
        "last_trade_at": None,
        "latest_real_sol_reserve": None,
        "previous_real_sol_reserve": None,
        "latest_curve_at": None,
        "curve_velocity": None,
        "curve_acceleration": None,
        "migration_state": "PRE_MIGRATION",
        "sources": {},
    }


class IncrementalTokenProjector:
    """O(1) event updates for the live hot path, persisted for restart recovery."""

    def __init__(self, store: Any):
        self.store = store
        self._states: dict[int, dict[str, Any]] = {}

    def apply(self, token_id: int, event: CanonicalEvent) -> dict[str, Any]:
        # Actor counters, rolling windows and the compact token snapshot commit
        # together. This keeps one SQLite transaction per canonical event.
        with self.store._lock, self.store.conn:
            state = self._load(token_id)
            if state.get("last_event_id") == event.event_id:
                return state
            state["event_count"] = int(state["event_count"]) + 1
            state["last_event_id"] = event.event_id
            state["last_event_timestamp"] = event.source_timestamp
            state["last_available_timestamp"] = event.available_timestamp
            state["sources"][event.source] = {
                "source_timestamp": event.source_timestamp,
                "received_timestamp": event.received_timestamp,
                "available_timestamp": event.available_timestamp,
            }
            if event.event_type == CanonicalEventType.TOKEN_TRADE:
                self._trade(token_id, state, event)
            elif event.event_type in {
                CanonicalEventType.BONDING_CURVE_STATE,
                CanonicalEventType.BONDING_CURVE_PROGRESS,
            }:
                self._curve(state, event)
            elif event.event_type == CanonicalEventType.MIGRATION_STARTED:
                state["migration_state"] = "MIGRATING"
            elif event.event_type in {
                CanonicalEventType.MIGRATION_COMPLETED,
                CanonicalEventType.POOL_CREATED,
            }:
                state["migration_state"] = "MIGRATED"
            self._persist(token_id, state)
            return state

    def snapshot(self, token_id: int, decision_timestamp: str) -> dict[str, Any] | None:
        state = self._load(token_id, required=False)
        if not state or str(state.get("last_available_timestamp")) > decision_timestamp:
            return None
        launched = _timestamp(str(state["launched_at"]))
        decision = _timestamp(decision_timestamp)
        sequence = self._sequence_features(state)
        first_sell = dict(state.get("first_meaningful_sell") or {})
        buyer_count = int(state["unique_buyer_count"])
        seller_count = int(state["unique_seller_count"])
        post_buyer_count = int(state["post_sell_unique_buyer_count"])
        total_buy = float(state["buy_sol"])
        net = float(state["capital_net_sol"])
        feature = {
            "feature_version": STATE_VERSION,
            "decision_timestamp": decision_timestamp,
            "launched_at": launched.isoformat(),
            "token_age_seconds": max(0.0, (decision - launched).total_seconds()),
            "evidence_mode": "LIVE_INCREMENTAL",
            "windows": sequence,
            "sequence_intelligence": self._sequence_transitions(sequence),
            "buyer_arrival": {
                "raw_buyers": buyer_count,
                "adjusted_independent_buyers": None,
                "linkage_state": "UNKNOWN",
                "new_buyers_per_second": buyer_count
                / max(1.0, (decision - launched).total_seconds()),
                "repeat_buyer_share": _ratio(int(state["repeat_buyer_count"]), buyer_count),
                "buyer_replacement": max(0, post_buyer_count - seller_count),
                "median_buy_size_sol": None,
            },
            "first_sell": {
                **first_sell,
                "observed": bool(first_sell),
                "fresh_buyers_after_sell": post_buyer_count,
                "post_sell_buy_sol": float(state["post_sell_buy_sol"]),
                "post_sell_sell_sol": float(state["post_sell_sell_sol"]),
                "capital_replacement_ratio": _ratio(
                    float(state["post_sell_buy_sol"]),
                    float(first_sell.get("sell_sol") or 0),
                ),
                "responses": state["post_sell_responses"],
            },
            "sell_absorption_v2": self._sell_absorption_v2(state, decision),
            "capital_trajectory": {
                "real_sol_reserve": state.get("latest_real_sol_reserve"),
                "net_flow_sol": net,
                "capital_peak_sol": float(state["capital_peak_sol"]),
                "capital_velocity_sol_per_second": float(state["capital_velocity"]),
                "capital_acceleration_sol_per_second2": float(state["capital_acceleration"]),
                "sol_gained_per_trade": _ratio(net, int(state["trade_count"])),
                "sol_gained_per_unique_buyer": _ratio(net, buyer_count),
                "sol_gained_per_independent_buyer": None,
                "capital_milestones": state["capital_milestones"],
                "capital_recovery_after_pullback": _ratio(net, float(state["capital_peak_sol"])),
            },
            "activity_adjustment": {
                "raw_volume_sol": total_buy + float(state["sell_sol"]),
                "adjusted_volume_sol": None,
                "raw_net_flow_sol": net,
                "adjusted_net_flow_sol": None,
                "linked_wallet_share": None,
                "bundle_linked_share": None,
                "wash_probability": None,
            },
            "migration_state": state["migration_state"],
            "migration_continuity": {"state": "INCREMENTAL_PENDING_RECONCILIATION"},
            "capital_efficiency": {
                "sol_gained_per_trade": _ratio(net, int(state["trade_count"])),
                "sol_gained_per_unique_buyer": _ratio(net, buyer_count),
                "trades_to_milestones": {
                    key: value.get("trade_count")
                    for key, value in state["capital_milestones"].items()
                },
                "buyers_to_milestones": {
                    key: value.get("buyer_count")
                    for key, value in state["capital_milestones"].items()
                },
                "time_to_milestones_seconds": {
                    key: value.get("seconds") for key, value in state["capital_milestones"].items()
                },
                "buyer_capital_concentration": _ratio(float(state["largest_buyer_sol"]), total_buy),
            },
            "coverage": {
                "trade_events": int(state["trade_count"]) > 0,
                "curve_observations": state.get("latest_curve_at") is not None,
                "real_sol_reserve": state.get("latest_real_sol_reserve") is not None,
                "buyer_identity": buyer_count > 0,
                "wallet_linkage": False,
                "creator": False,
                "funder": False,
                "bundle": False,
                "first_sell": bool(first_sell),
                "migration": state["migration_state"] != "PRE_MIGRATION",
                "provider_timestamps": bool(state["sources"]),
            },
            "provenance": [
                {"field_name": source, "source": source, **timestamps}
                for source, timestamps in sorted(state["sources"].items())
            ],
            "incremental_state": {
                "event_count": state["event_count"],
                "trade_count": state["trade_count"],
                "buy_count": state["buy_count"],
                "sell_count": state["sell_count"],
                "last_event_id": state["last_event_id"],
            },
        }
        feature["monitoring"] = self._monitoring(feature)
        return feature

    def _load(self, token_id: int, *, required: bool = True) -> dict[str, Any] | None:
        if token_id in self._states:
            return self._states[token_id]
        row = self.store.conn.execute(
            "SELECT state_json FROM incremental_feature_state_v15 WHERE token_id=?",
            (token_id,),
        ).fetchone()
        if row:
            state = json.loads(row[0])
            defaults = _new_state(str(state["launched_at"]))
            for key, value in defaults.items():
                state.setdefault(key, value)
            self._states[token_id] = state
            return state
        token = self.store.conn.execute(
            "SELECT launched_at FROM token_realtime_state WHERE token_id=?",
            (token_id,),
        ).fetchone()
        if not token:
            if required:
                raise KeyError(f"token {token_id} has no realtime state")
            return None
        state = _new_state(str(token[0]))
        self._states[token_id] = state
        return state

    def _trade(self, token_id: int, state: dict[str, Any], event: CanonicalEvent) -> None:
        payload = event.payload
        side = str(payload.get("side") or "").lower()
        if side not in {"buy", "sell"}:
            return
        actor = str(payload.get("actor") or payload.get("user") or "UNKNOWN")
        amount = max(
            0.0,
            float(payload.get("sol_amount") or payload.get("quote_amount") or 0),
        )
        timestamp = _timestamp(event.source_timestamp)
        launched = _timestamp(str(state["launched_at"]))
        age = max(0.0, (timestamp - launched).total_seconds())
        state["trade_count"] += 1
        state[f"{side}_count"] += 1
        state[f"{side}_sol"] += amount
        first_sell_before_trade = state.get("first_meaningful_sell")
        actor_state = self._update_actor(
            token_id,
            actor,
            side,
            amount,
            timestamp.isoformat(),
            post_sell=bool(first_sell_before_trade and side == "buy"),
        )
        if side == "buy" and actor_state["buy_count"] == 1:
            state["unique_buyer_count"] += 1
        if side == "buy" and actor_state["buy_count"] == 2:
            state["repeat_buyer_count"] += 1
        if side == "sell" and actor_state["sell_count"] == 1:
            state["unique_seller_count"] += 1
        if side == "buy" and actor_state["post_sell_buy_count"] == 1:
            state["post_sell_unique_buyer_count"] += 1
        state["largest_buyer_sol"] = max(
            float(state["largest_buyer_sol"]), float(actor_state["buy_sol"])
        )
        band = _band(age)
        if band:
            cell = state["sequence"][band]
            cell["trade_count"] += 1
            cell[f"{side}_count"] += 1
            cell[f"{side}_sol"] += amount
            if side == "buy" and self._mark_window_buyer(
                token_id, actor, f"launch:{band}", timestamp.isoformat()
            ):
                cell["new_buyer_count"] += 1
        signed = amount if side == "buy" else -amount
        previous_net = float(state["capital_net_sol"])
        previous_time = _timestamp(state["last_trade_at"]) if state["last_trade_at"] else None
        delta_seconds = (
            max(1.0, (timestamp - previous_time).total_seconds()) if previous_time else 1.0
        )
        prior_velocity = float(state["capital_velocity"])
        velocity = signed / delta_seconds
        state["capital_velocity"] = velocity
        state["capital_acceleration"] = (velocity - prior_velocity) / delta_seconds
        state["capital_previous_sol"] = previous_net
        state["capital_net_sol"] = previous_net + signed
        state["capital_peak_sol"] = max(
            float(state["capital_peak_sol"]), float(state["capital_net_sol"])
        )
        state["last_trade_at"] = timestamp.isoformat()
        for milestone in CAPITAL_MILESTONES:
            key = str(milestone)
            if key not in state["capital_milestones"] and state["capital_net_sol"] >= milestone:
                state["capital_milestones"][key] = {
                    "seconds": age,
                    "trade_count": state["trade_count"],
                    "buyer_count": state["unique_buyer_count"],
                }
        first_sell = state.get("first_meaningful_sell")
        meaningful_sell = side == "sell" and amount >= max(
            0.05,
            (float(state["buy_sol"]) * 0.05),
        )
        if meaningful_sell:
            state["meaningful_sell_count"] += 1
        if meaningful_sell and first_sell is None:
            state["first_meaningful_sell"] = {
                "timestamp": timestamp.isoformat(),
                "seconds_since_launch": age,
                "seller": actor,
                "sell_sol": amount,
                "trade_count_at_sell": state["trade_count"],
                "buyer_count_at_sell": state["unique_buyer_count"],
                "real_sol_at_sell": state.get("latest_real_sol_reserve"),
                "seller_size_relative_to_prior_buy": _ratio(
                    amount,
                    float(actor_state["buy_sol"]),
                ),
            }
            first_sell = state["first_meaningful_sell"]
        elif meaningful_sell and state.get("second_meaningful_sell") is None:
            state["second_meaningful_sell"] = {
                "timestamp": timestamp.isoformat(),
                "seconds_since_launch": age,
                "seconds_after_first_sell": max(
                    0.0,
                    (timestamp - _timestamp(first_sell["timestamp"])).total_seconds(),
                ),
                "seller": actor,
                "sell_sol": amount,
                "trade_count_at_sell": state["trade_count"],
                "buyer_count_at_sell": state["unique_buyer_count"],
            }
        if first_sell:
            after = max(0.0, (timestamp - _timestamp(first_sell["timestamp"])).total_seconds())
            if side == "buy":
                state["post_sell_buy_sol"] += amount
            elif after > 0:
                state["post_sell_sell_sol"] += amount
            for horizon in (5, 10, 20, 30):
                if 0 < after <= horizon:
                    response = state["post_sell_responses"][str(horizon)]
                    response[f"{side}_sol"] += amount
                    if side == "buy" and self._mark_window_buyer(
                        token_id,
                        actor,
                        f"post_sell:{horizon}",
                        timestamp.isoformat(),
                    ):
                        response["buyer_count"] += 1
        second_sell = state.get("second_meaningful_sell")
        if second_sell and timestamp > _timestamp(second_sell["timestamp"]) and side == "buy":
            state["post_second_sell_buy_sol"] += amount

    def _update_actor(
        self,
        token_id: int,
        actor: str,
        side: str,
        amount: float,
        observed_at: str,
        *,
        post_sell: bool,
    ) -> dict[str, Any]:
        self.store.conn.execute(
            "INSERT INTO incremental_actor_state_v15(token_id,actor,buy_count,sell_count,"
            "buy_sol,sell_sol,post_sell_buy_count,first_seen_at,last_seen_at) "
            "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(token_id,actor) DO UPDATE SET "
            "buy_count=buy_count+excluded.buy_count,sell_count=sell_count+excluded.sell_count,"
            "buy_sol=buy_sol+excluded.buy_sol,sell_sol=sell_sol+excluded.sell_sol,"
            "post_sell_buy_count=post_sell_buy_count+excluded.post_sell_buy_count,"
            "last_seen_at=excluded.last_seen_at",
            (
                token_id,
                actor,
                int(side == "buy"),
                int(side == "sell"),
                amount if side == "buy" else 0.0,
                amount if side == "sell" else 0.0,
                int(post_sell and side == "buy"),
                observed_at,
                observed_at,
            ),
        )
        row = self.store.conn.execute(
            "SELECT buy_count,sell_count,buy_sol,sell_sol,post_sell_buy_count "
            "FROM incremental_actor_state_v15 WHERE token_id=? AND actor=?",
            (token_id, actor),
        ).fetchone()
        return dict(row)

    def _mark_window_buyer(
        self,
        token_id: int,
        actor: str,
        window_key: str,
        observed_at: str,
    ) -> bool:
        cursor = self.store.conn.execute(
            "INSERT OR IGNORE INTO incremental_actor_window_v15 VALUES(?,?,?,?)",
            (token_id, actor, window_key, observed_at),
        )
        return cursor.rowcount == 1

    @staticmethod
    def _curve(state: dict[str, Any], event: CanonicalEvent) -> None:
        payload = event.payload
        value = payload.get("real_sol_reserves") or payload.get("real_quote_reserves")
        if value is not None:
            number = float(value)
            if number > 1_000_000:
                number /= 1_000_000_000
            previous_at = state.get("latest_curve_at")
            previous_value = state.get("latest_real_sol_reserve")
            if previous_at and previous_value is not None:
                seconds = max(
                    1e-6,
                    (
                        _timestamp(event.source_timestamp) - _timestamp(str(previous_at))
                    ).total_seconds(),
                )
                prior_velocity = state.get("curve_velocity")
                velocity = (number - float(previous_value)) / seconds
                state["curve_velocity"] = velocity
                if prior_velocity is not None:
                    state["curve_acceleration"] = (velocity - float(prior_velocity)) / seconds
            state["previous_real_sol_reserve"] = state.get("latest_real_sol_reserve")
            state["latest_real_sol_reserve"] = number
        state["latest_curve_at"] = event.source_timestamp

    def _persist(self, token_id: int, state: dict[str, Any]) -> None:
        self.store.conn.execute(
            "INSERT INTO incremental_feature_state_v15 VALUES(?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(token_id) DO UPDATE SET last_event_id=excluded.last_event_id,"
            "last_event_timestamp=excluded.last_event_timestamp,last_available_timestamp="
            "excluded.last_available_timestamp,state_version=excluded.state_version,"
            "event_count=excluded.event_count,trade_count=excluded.trade_count,"
            "buy_count=excluded.buy_count,sell_count=excluded.sell_count,"
            "state_json=excluded.state_json,updated_at=excluded.updated_at",
            (
                token_id,
                state["last_event_id"],
                state["last_event_timestamp"],
                state["last_available_timestamp"],
                STATE_VERSION,
                state["event_count"],
                state["trade_count"],
                state["buy_count"],
                state["sell_count"],
                json.dumps(state, separators=(",", ":"), sort_keys=True),
                iso(),
            ),
        )

    @staticmethod
    def _sequence_features(state: dict[str, Any]) -> dict[str, Any]:
        output = {}
        for start, end in WINDOW_BANDS:
            key = f"{start}-{end}"
            cell = state["sequence"][key]
            duration = end - start
            output[key] = {
                "start_seconds": start,
                "end_seconds": end,
                "trade_count": cell["trade_count"],
                "buy_count": cell["buy_count"],
                "sell_count": cell["sell_count"],
                "new_buyer_count": cell["new_buyer_count"],
                "net_sol": cell["buy_sol"] - cell["sell_sol"],
                "trade_velocity": cell["trade_count"] / duration,
                "buyer_velocity": cell["new_buyer_count"] / duration,
                "sell_pressure": _ratio(cell["sell_sol"], cell["buy_sol"] + cell["sell_sol"]),
            }
        return output

    @staticmethod
    def _sequence_transitions(sequence: dict[str, Any]) -> dict[str, Any]:
        cells = list(sequence.values())
        net = [float(row["net_sol"]) for row in cells]
        velocity = [float(row["trade_velocity"]) for row in cells]
        net_deltas = [right - left for left, right in pairwise(net)]
        velocity_deltas = [right - left for left, right in pairwise(velocity)]
        return {
            "net_flow_direction_by_band": [
                0 if value == 0 else math.copysign(1, value) for value in net
            ],
            "net_flow_acceleration_by_transition": net_deltas,
            "trade_velocity_acceleration_by_transition": velocity_deltas,
            "positive_persistence_bands": sum(value > 0 for value in net),
            "reversal_count": sum(left * right < 0 for left, right in pairwise(net)),
            "recovery_after_negative_band": any(left < 0 < right for left, right in pairwise(net)),
        }

    @staticmethod
    def _monitoring(feature: dict[str, Any]) -> dict[str, Any]:
        buyers = int(feature["buyer_arrival"]["raw_buyers"])
        velocity = abs(float(feature["capital_trajectory"]["capital_velocity_sol_per_second"]))
        if velocity >= 0.25 or buyers >= 20:
            return {"state": "HOT", "priority": 1.0, "interval_seconds": 1}
        if velocity >= 0.05 or buyers >= 5:
            return {"state": "WARM", "priority": 0.65, "interval_seconds": 5}
        return {"state": "COLD", "priority": 0.2, "interval_seconds": 30}

    @staticmethod
    def _sell_absorption_v2(state: dict[str, Any], decision: datetime) -> dict[str, Any]:
        first = dict(state.get("first_meaningful_sell") or {})
        second = dict(state.get("second_meaningful_sell") or {})
        post_buy = float(state["post_sell_buy_sol"])
        post_sell = float(state["post_sell_sell_sol"])
        elapsed = (
            max(1.0, (decision - _timestamp(first["timestamp"])).total_seconds()) if first else None
        )
        return {
            "model_name": "SELL_ABSORPTION_V2",
            "authority": "RESEARCH_ONLY",
            "first_meaningful_sell": first or None,
            "responses_5_10_20_30_seconds": state["post_sell_responses"],
            "fresh_buyer_count": int(state["post_sell_unique_buyer_count"]),
            "fresh_independent_buyer_count": None,
            "repeat_buyer_count": int(state["repeat_buyer_count"]),
            "buyer_replacement": max(
                0,
                int(state["post_sell_unique_buyer_count"]) - int(state["unique_seller_count"]),
            ),
            "seller_identity": first.get("seller"),
            "seller_historical_behavior": None,
            "seller_size_relative_to_prior_buy_sol": first.get("seller_size_relative_to_prior_buy"),
            "real_sol_recovery": (
                float(state["latest_real_sol_reserve"]) - float(first["real_sol_at_sell"])
                if first.get("real_sol_at_sell") is not None
                and state.get("latest_real_sol_reserve") is not None
                else None
            ),
            "real_sol_reacceleration": state.get("curve_acceleration"),
            "second_meaningful_sell": second or None,
            "second_sell_absorption_ratio": _ratio(
                float(state["post_second_sell_buy_sol"]),
                float(second.get("sell_sol") or 0),
            ),
            "post_sell_wallet_arrivals": int(state["post_sell_unique_buyer_count"]),
            "post_sell_net_flow_sol": post_buy - post_sell,
            "post_sell_curve_velocity_sol_per_second": state.get("curve_velocity"),
            "post_sell_trade_intensity_per_second": (
                _ratio(int(state["trade_count"]) - int(first["trade_count_at_sell"]), elapsed)
                if first
                else None
            ),
            "post_sell_market_cap_expansion": None,
            "migration_proximity": None,
            "migration_continuation": state["migration_state"],
            "capital_lost_per_sell": _ratio(float(state["sell_sol"]), int(state["sell_count"])),
            "capital_replacement_ratio": _ratio(post_buy, post_sell),
            "coverage": {
                "independent_wallet_linkage": False,
                "seller_history": False,
                "market_cap_series": False,
                "migration_proximity": False,
            },
        }
