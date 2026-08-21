from __future__ import annotations

from memecoin_bot.models import MarketSnapshot


def _ratio(a: float | None, b: float | None) -> float | None:
    if a is None or b is None or b <= 0:
        return None
    return a / b


class MomentumEngine:
    def assess_history(
        self, current: MarketSnapshot, previous: list[dict], minimum: int = 3
    ) -> dict:
        if len(previous) + 1 < minimum:
            result = self.assess(current, previous[-1] if previous else None)
            result["score"] = None
            result["reason"] = "INSUFFICIENT_ROLLING_HISTORY"
            result["snapshots_required"] = minimum
            return result
        latest = self.assess(current, previous[-1])
        if latest.get("score") is None or len(previous) < 2:
            return latest
        prior = (
            self.assess(previous[-1]["_snapshot"], previous[-2])
            if "_snapshot" in previous[-1]
            else None
        )
        latest["acceleration"] = (
            None
            if not prior or prior.get("score") is None
            else round(latest["score"] - prior["score"], 2)
        )
        return latest

    def assess(self, current: MarketSnapshot, previous: dict | None) -> dict:
        buy_sell = _ratio(
            float(current.buys_5m) if current.buys_5m is not None else None,
            float(current.sells_5m) if current.sells_5m is not None else None,
        )
        volume_mc = _ratio(current.volume_5m_usd, current.market_cap_usd)
        if not previous:
            return {
                "score": None,
                "buy_sell_ratio": buy_sell,
                "volume_to_market_cap": volume_mc,
                "reason": "ROLLING_HISTORY_NOT_YET_AVAILABLE",
            }
        previous_mc = previous.get("market_cap_usd")
        previous_volume = previous.get("volume_5m_usd")
        mc_velocity = _ratio(current.market_cap_usd, previous_mc)
        volume_velocity = _ratio(current.volume_5m_usd, previous_volume)
        if mc_velocity is None or volume_velocity is None or buy_sell is None:
            return {
                "score": None,
                "market_cap_velocity": mc_velocity,
                "volume_velocity": volume_velocity,
                "buy_sell_ratio": buy_sell,
                "reason": "MOMENTUM_INPUTS_INCOMPLETE",
            }
        score = 4.0
        score += min(4.0, max(0.0, (mc_velocity - 1) * 10))
        score += min(4.0, max(0.0, (volume_velocity - 1) * 4))
        score += min(3.0, max(0.0, (buy_sell - 1) * 2))
        if current.price_change_5m is not None and current.price_change_5m > 100:
            score -= min(5.0, (current.price_change_5m - 100) / 50)
        return {
            "score": round(max(0.0, min(15.0, score)), 2),
            "market_cap_velocity": mc_velocity,
            "volume_velocity": volume_velocity,
            "buy_sell_ratio": buy_sell,
            "volume_to_market_cap": volume_mc,
        }
