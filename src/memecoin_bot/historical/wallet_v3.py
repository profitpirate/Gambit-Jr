from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum


class WalletStrategy(StrEnum):
    BONDING_CURVE_SNIPER = "BONDING_CURVE_SNIPER"
    MIGRATION_BUYER = "MIGRATION_BUYER"
    POST_MIGRATION_PULLBACK = "POST_MIGRATION_PULLBACK"
    MOMENTUM_SCALPER = "MOMENTUM_SCALPER"
    EARLY_SWING = "EARLY_SWING"
    RIGHT_TAIL_HOLDER = "RIGHT_TAIL_HOLDER"
    REVIVAL_TRADER = "REVIVAL_TRADER"
    SCALE_IN_SCALE_OUT = "SCALE_IN_SCALE_OUT"
    HIGH_FREQUENCY_BOT = "HIGH_FREQUENCY_BOT"
    CREATOR_OR_INSIDER = "CREATOR_OR_INSIDER"
    ARBITRAGE = "ARBITRAGE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class WalletHistory:
    wallet: str
    active_days: int
    closed_positions: int
    trades: int
    realized_pnl: float
    roi: float
    win_rate: float
    maximum_single_token_pnl_share: float
    positive_windows: int
    drawdown: float
    last_trade_age_days: float
    identity_labels: tuple[str, ...] = ()
    invalid_pnl: bool = False


@dataclass(frozen=True, slots=True)
class WalletSelectionPolicy:
    minimum_active_days: int = 7
    minimum_closed_positions: int = 20
    minimum_trades: int = 40
    maximum_single_token_pnl_share: float = 0.50
    minimum_positive_windows: int = 2
    maximum_drawdown: float = 0.70
    maximum_last_trade_age_days: float = 30


@dataclass(frozen=True, slots=True)
class FollowerOutcome:
    wallet: str
    token: str
    delay_seconds: int
    entry_available: bool
    sellable: bool | None
    peak_multiple: float | None
    drawdown: float | None
    failure: bool | None
    first_wallet_sell_seconds: float | None


def select_wallet_corpus(
    histories: Iterable[WalletHistory],
    policy: WalletSelectionPolicy | None = None,
) -> tuple[list[WalletHistory], dict[str, list[str]]]:
    policy = policy or WalletSelectionPolicy()
    selected: list[WalletHistory] = []
    rejected: dict[str, list[str]] = {}
    prohibited = {"arbitrage", "potential_bot", "developer", "hacker", "pool", "spam_dusting"}
    for history in histories:
        reasons = []
        labels = {label.lower() for label in history.identity_labels}
        if labels & prohibited:
            reasons.append("PROHIBITED_IDENTITY")
        if history.invalid_pnl:
            reasons.append("INVALID_PNL")
        if history.active_days < policy.minimum_active_days:
            reasons.append("INSUFFICIENT_ACTIVE_DAYS")
        if history.closed_positions < policy.minimum_closed_positions:
            reasons.append("INSUFFICIENT_CLOSED_POSITIONS")
        if history.trades < policy.minimum_trades:
            reasons.append("INSUFFICIENT_TRADES")
        if history.maximum_single_token_pnl_share > policy.maximum_single_token_pnl_share:
            reasons.append("PNL_CONCENTRATED_IN_ONE_TOKEN")
        if history.positive_windows < policy.minimum_positive_windows:
            reasons.append("INSUFFICIENT_POSITIVE_WINDOWS")
        if history.drawdown > policy.maximum_drawdown:
            reasons.append("DRAWDOWN_TOO_HIGH")
        if history.last_trade_age_days > policy.maximum_last_trade_age_days:
            reasons.append("STALE_ACTIVITY")
        if reasons:
            rejected[history.wallet] = reasons
        else:
            selected.append(history)
    return selected, rejected


def copyability_scores(outcomes: Sequence[FollowerOutcome]) -> dict[str, float | int | None]:
    required_delays = {15, 30, 60, 120}
    observed_delays = {row.delay_seconds for row in outcomes}
    if outcomes and not required_delays.issubset(observed_delays):
        raise ValueError("copyability requires 15, 30, 60 and 120 second follower outcomes")
    usable = [
        row
        for row in outcomes
        if row.entry_available and row.sellable is True and row.peak_multiple is not None
    ]
    if not usable:
        return {
            "positions": len(outcomes),
            "copyability_score": None,
            "copyable_2x_skill": None,
            "copyable_5x_skill": None,
            "copyable_right_tail_skill": None,
        }
    two = sum(row.peak_multiple >= 2 and not row.failure for row in usable) / len(usable)
    five = sum(row.peak_multiple >= 5 and not row.failure for row in usable) / len(usable)
    tail = sum(row.peak_multiple >= 10 and not row.failure for row in usable) / len(usable)
    sellability = len(usable) / len(outcomes)
    drawdown = sum(max(0.0, row.drawdown or 0.0) for row in usable) / len(usable)
    return {
        "positions": len(outcomes),
        "copyability_score": max(0.0, sellability * (0.6 * two + 0.3 * five + 0.1 * tail) - 0.25 * drawdown),
        "copyable_2x_skill": two,
        "copyable_5x_skill": five,
        "copyable_right_tail_skill": tail,
    }


def independent_wallet_consensus(
    wallet_scores: Mapping[str, float],
    relationships: Iterable[tuple[str, str]],
) -> dict[str, float | int]:
    parent = {wallet: wallet for wallet in wallet_scores}

    def root(wallet: str) -> str:
        while parent[wallet] != wallet:
            parent[wallet] = parent[parent[wallet]]
            wallet = parent[wallet]
        return wallet

    for left, right in relationships:
        if left not in parent or right not in parent:
            continue
        left_root, right_root = root(left), root(right)
        if left_root != right_root:
            parent[right_root] = left_root
    components: dict[str, list[float]] = {}
    for wallet, score in wallet_scores.items():
        components.setdefault(root(wallet), []).append(score)
    component_scores = [max(scores) for scores in components.values()]
    return {
        "independent_validated_wallet_count": len(component_scores),
        "copyable_consensus_score": sum(component_scores) / len(component_scores) if component_scores else 0.0,
        "raw_wallet_count": len(wallet_scores),
    }
