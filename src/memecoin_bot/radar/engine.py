from __future__ import annotations

from datetime import datetime, timezone

from memecoin_bot.config import Settings
from memecoin_bot.models import MarketSnapshot, RadarResult


def _ratio(current: float | int | None, previous: float | int | None) -> float | None:
    if current is None or previous is None or float(previous) <= 0:
        return None
    return float(current) / float(previous)


class RadarEngine:
    """Early-behaviour evaluator kept deliberately separate from signal scoring."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def evaluate(
        self,
        current: MarketSnapshot,
        previous: list[dict],
        first_discovered_at: str,
        basic_safety_passed: bool,
    ) -> RadarResult:
        if not basic_safety_passed or len(previous) + 1 < self.settings.radar_min_snapshots:
            return RadarResult(False, 0, [], ["BASIC_SAFETY_OR_HISTORY_INCOMPLETE"])
        now = datetime.fromisoformat(current.captured_at.replace("Z", "+00:00"))
        origin_text = current.pair_created_at or first_discovered_at
        origin = datetime.fromisoformat(origin_text.replace("Z", "+00:00"))
        age_minutes = max(0, (now - origin).total_seconds() / 60)
        prior = previous[-1]
        mc_velocity = _ratio(current.market_cap_usd, prior.get("market_cap_usd"))
        volume_velocity = _ratio(current.volume_5m_usd, prior.get("volume_5m_usd"))
        liquidity_velocity = _ratio(current.liquidity_usd, prior.get("liquidity_usd"))
        buy_sell = _ratio(current.buys_5m, current.sells_5m)
        prior_buy_sell = _ratio(prior.get("buys_5m"), prior.get("sells_5m"))
        volume_mc = _ratio(current.volume_5m_usd, current.market_cap_usd)

        conditions: list[tuple[str, float]] = []
        if mc_velocity is not None and mc_velocity >= 1.12:
            conditions.append(("MARKET_CAP_ACCELERATING", min(18, (mc_velocity - 1) * 50)))
        if volume_velocity is not None and volume_velocity >= 1.35:
            conditions.append(("VOLUME_ACCELERATING", min(18, (volume_velocity - 1) * 20)))
        if liquidity_velocity is not None and liquidity_velocity >= 1.05:
            conditions.append(("LIQUIDITY_GROWING", min(12, (liquidity_velocity - 1) * 30)))
        if buy_sell is not None and buy_sell >= 1.8:
            conditions.append(("BUY_PRESSURE_HIGH", min(15, (buy_sell - 1) * 8)))
        if buy_sell is not None and prior_buy_sell is not None and buy_sell >= prior_buy_sell * 1.2:
            conditions.append(("BUY_PRESSURE_ACCELERATING", 10))
        if volume_mc is not None and volume_mc >= 0.15:
            conditions.append(("RELATIVE_ACTIVITY_HIGH", min(12, volume_mc * 30)))

        score = 10.0  # basic chain safety passed
        if age_minutes <= self.settings.radar_max_age_minutes:
            score += max(4, 15 * (1 - age_minutes / self.settings.radar_max_age_minutes))
        if (
            current.liquidity_usd is not None
            and current.liquidity_usd >= self.settings.radar_min_liquidity_usd
        ):
            score += 10
        score += sum(points for _, points in conditions)
        penalties: list[str] = []
        if age_minutes > self.settings.radar_max_age_minutes:
            penalties.append("OUTSIDE_EARLY_WINDOW")
            score -= 30
        if (
            current.market_cap_usd is None
            or current.market_cap_usd > self.settings.radar_max_market_cap_usd
        ):
            penalties.append("ABOVE_RADAR_MARKET_CAP_RANGE")
            score -= 30
        if (
            current.liquidity_usd is None
            or current.liquidity_usd < self.settings.radar_min_liquidity_usd
        ):
            penalties.append("RADAR_LIQUIDITY_TOO_LOW")
            score -= 30
        if (
            current.price_change_5m is not None
            and current.price_change_5m >= self.settings.radar_late_pump_price_change_percent
        ):
            penalties.append("LATE_VERTICAL_PRICE_MOVE")
            score -= 45
        if liquidity_velocity is not None and liquidity_velocity < 0.85:
            penalties.append("LIQUIDITY_COLLAPSING")
            score -= 35
        if buy_sell is not None and buy_sell < 0.8:
            penalties.append("SELL_PRESSURE_DOMINANT")
            score -= 25

        final = round(max(0, min(100, score)), 2)
        triggered = (
            len(conditions) >= self.settings.radar_min_conditions
            and final >= self.settings.radar_score_threshold
            and not {
                "OUTSIDE_EARLY_WINDOW",
                "ABOVE_RADAR_MARKET_CAP_RANGE",
                "RADAR_LIQUIDITY_TOO_LOW",
                "LATE_VERTICAL_PRICE_MOVE",
                "LIQUIDITY_COLLAPSING",
                "SELL_PRESSURE_DOMINANT",
            }
            & set(penalties)
        )
        return RadarResult(triggered, final, [name for name, _ in conditions], penalties)
