from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from memecoin_bot.models import iso

TARGETS = (2, 5, 10, 20, 50)
TERMINAL_FAILURE_MULTIPLE = 0.30


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp must include timezone: {value}")
    return parsed.astimezone(UTC)


class DecisionOutcomeLedger:
    """Outcome labels anchored to each immutable decision, never first discovery."""

    def __init__(self, store: Any):
        self.store = store

    def refresh_token(self, token_id: int, *, mature_after_seconds: float = 86_400) -> int:
        decisions = list(
            self.store.conn.execute(
                "SELECT decision_id,decision_at,decision_price,decision_market_cap "
                "FROM decision_outcomes_v15 WHERE token_id=? AND outcome_state!='SEALED'",
                (token_id,),
            )
        )
        updated = 0
        for decision in decisions:
            observed = list(
                self.store.conn.execute(
                    "SELECT captured_at,price_usd,market_cap_usd FROM token_snapshots "
                    "WHERE token_id=? AND captured_at>=? ORDER BY captured_at,id",
                    (token_id, decision["decision_at"]),
                )
            )
            if not observed:
                continue
            decision_price = decision["decision_price"]
            decision_market_cap = decision["decision_market_cap"]
            peak_price = max(
                (float(row["price_usd"]) for row in observed if row["price_usd"]),
                default=None,
            )
            peak_market_cap = max(
                (float(row["market_cap_usd"]) for row in observed if row["market_cap_usd"]),
                default=None,
            )
            multiples = []
            if decision_price and peak_price:
                multiples.append(peak_price / float(decision_price))
            if decision_market_cap and peak_market_cap:
                multiples.append(peak_market_cap / float(decision_market_cap))
            peak_multiple = max(multiples, default=None)
            latest_multiples = []
            if decision_price and observed[-1]["price_usd"]:
                latest_multiples.append(float(observed[-1]["price_usd"]) / float(decision_price))
            if decision_market_cap and observed[-1]["market_cap_usd"]:
                latest_multiples.append(
                    float(observed[-1]["market_cap_usd"]) / float(decision_market_cap)
                )
            times: dict[int, float | None] = {}
            decision_at = _timestamp(str(decision["decision_at"]))
            adverse_values: list[float] = []
            for target in TARGETS:
                hit = None
                for row in observed:
                    point_multiples = []
                    if decision_price and row["price_usd"]:
                        point_multiples.append(float(row["price_usd"]) / float(decision_price))
                    if decision_market_cap and row["market_cap_usd"]:
                        point_multiples.append(
                            float(row["market_cap_usd"]) / float(decision_market_cap)
                        )
                    if point_multiples:
                        adverse_values.append(min(point_multiples) - 1)
                    if point_multiples and max(point_multiples) >= target:
                        hit = max(
                            0.0,
                            (_timestamp(str(row["captured_at"])) - decision_at).total_seconds(),
                        )
                        break
                times[target] = hit
            last_observed = str(observed[-1]["captured_at"])
            mature = (
                _timestamp(last_observed) - decision_at
            ).total_seconds() >= mature_after_seconds
            with self.store._lock, self.store.conn:
                self.store.conn.execute(
                    "UPDATE decision_outcomes_v15 SET future_peak_price=?,future_peak_market_cap=?,"
                    "peak_multiple_from_decision=?,time_to_2x_from_decision=?,"
                    "time_to_5x_from_decision=?,time_to_10x_from_decision=?,"
                    "time_to_20x_from_decision=?,time_to_50x_from_decision=?,"
                    "maximum_adverse_excursion=?,maximum_favorable_excursion=?,"
                    "terminal_failure=?,outcome_mature_at=?,last_observed_at=?,outcome_state=?,"
                    "updated_at=? WHERE decision_id=?",
                    (
                        peak_price,
                        peak_market_cap,
                        peak_multiple,
                        times[2],
                        times[5],
                        times[10],
                        times[20],
                        times[50],
                        min(adverse_values, default=None),
                        peak_multiple - 1 if peak_multiple is not None else None,
                        int(max(latest_multiples) <= TERMINAL_FAILURE_MULTIPLE)
                        if mature and latest_multiples
                        else None,
                        last_observed if mature else None,
                        last_observed,
                        "MATURE" if mature else "OPEN",
                        iso(),
                        decision["decision_id"],
                    ),
                )
            updated += 1
        return updated
