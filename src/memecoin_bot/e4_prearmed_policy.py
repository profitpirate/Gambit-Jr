from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .e4_prearmed_readiness import FROZEN_MODEL_SHA256, stable_hash


@dataclass(frozen=True, slots=True)
class SelectorDecision:
    accepted: bool
    reason: str
    position_fraction: float


@dataclass(frozen=True, slots=True)
class EntryGuardDecision:
    accepted: bool
    reason: str
    output_ratio: float
    create_to_fill_price_multiple: float


@dataclass(frozen=True, slots=True)
class ExitDecision:
    action: str
    fraction: float
    reason: str


class FrozenPrearmedPolicy:
    """Pure, side-effect-free representation of the frozen causal V12 policy.

    This module intentionally contains no wallet, signer, RPC submission, route,
    or transaction code. It exists so paper-live, stress, and future integration
    tests can assert that one canonical policy is being reproduced exactly.
    """

    def __init__(self, model: Mapping[str, Any]) -> None:
        self.model = dict(model)
        digest = stable_hash(self.model)
        if digest != FROZEN_MODEL_SHA256:
            raise ValueError(f"frozen model hash mismatch: {digest}")
        self.selector = dict(self.model["selector"])
        self.position_sizing = dict(self.model["position_sizing"])
        self.exit_policy = dict(self.model["exit_policy"])
        self.creator_handles = {
            str(creator): frozenset(str(handle).lower().lstrip("@") for handle in handles)
            for creator, handles in self.model["creator_handles"].items()
        }
        self.prior_attempts = {
            str(creator): int(attempts)
            for creator, attempts in self.model["creator_prior_e4_attempts"].items()
        }

    @classmethod
    def from_path(cls, path: Path) -> "FrozenPrearmedPolicy":
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, Mapping):
            raise ValueError("frozen model must be a JSON object")
        return cls(value)

    def select(
        self,
        *,
        creator: str,
        social_handle: str,
        social_status_ns: int,
        create_ns: int,
        prior_e4_attempts: int | None = None,
        creator_seed_sol: float,
        mayhem_mode: bool,
    ) -> SelectorDecision:
        handle = str(social_handle or "").lower().lstrip("@")
        attempts = self.prior_attempts.get(creator, 0) if prior_e4_attempts is None else int(prior_e4_attempts)
        if mayhem_mode and bool(self.selector.get("reject_mayhem", True)):
            return SelectorDecision(False, "mayhem", 0.0)
        if attempts < int(self.selector["minimum_prior_e4_attempts"]):
            return SelectorDecision(False, "unknown_creator", 0.0)
        if float(creator_seed_sol) < float(self.selector["minimum_creator_seed_sol"]):
            return SelectorDecision(False, "creator_seed_below_floor", 0.0)
        if not handle or handle not in self.creator_handles.get(creator, frozenset()):
            return SelectorDecision(False, "not_prearmed_handle", 0.0)
        age_seconds = (int(create_ns) - int(social_status_ns)) / 1_000_000_000
        if not (
            float(self.selector["minimum_tweet_age_seconds"])
            <= age_seconds
            <= float(self.selector["maximum_tweet_age_seconds"])
        ):
            return SelectorDecision(False, "social_age_outside_window", 0.0)
        return SelectorDecision(
            True,
            "selected",
            float(self.position_sizing["fraction_of_available_cash"]),
        )

    def entry_guard(
        self,
        *,
        expected_token_output: float,
        current_token_output: float,
        create_price: float,
        fill_price: float,
    ) -> EntryGuardDecision:
        expected = max(float(expected_token_output), 1e-18)
        create = max(float(create_price), 1e-18)
        output_ratio = float(current_token_output) / expected
        price_multiple = float(fill_price) / create
        if output_ratio < float(self.selector["minimum_entry_output_ratio"]):
            return EntryGuardDecision(False, "entry_output_guard_rejected", output_ratio, price_multiple)
        if price_multiple > float(self.selector["maximum_create_to_entry_price_multiple"]):
            return EntryGuardDecision(False, "entry_chase_guard_rejected", output_ratio, price_multiple)
        return EntryGuardDecision(True, "accepted", output_ratio, price_multiple)

    def exit(
        self,
        *,
        entry_price: float,
        current_price: float,
        peak_price: float,
        first_partial_done: bool,
        age_ms: int,
        complete: bool = False,
    ) -> ExitDecision:
        entry = max(float(entry_price), 1e-18)
        current = max(float(current_price), 0.0)
        peak = max(float(peak_price), entry)
        multiple = current / entry
        peak_multiple = peak / entry
        first_take = float(self.exit_policy["first_take"])
        first_fraction = float(self.exit_policy["first_fraction"])
        stop = float(self.exit_policy["stop"])
        trail_retrace = float(self.exit_policy["trail_retrace"])
        hold_ms = int(self.exit_policy["hold_ms"])

        if not first_partial_done and first_fraction > 0 and multiple >= first_take:
            return ExitDecision("SELL_PARTIAL", first_fraction, "FIRST_TAKE")
        floor = stop
        if first_partial_done:
            floor = max(floor, 1.02, peak_multiple * (1.0 - trail_retrace))
        elif peak_multiple >= first_take:
            floor = max(floor, peak_multiple * (1.0 - trail_retrace))
        if complete:
            return ExitDecision("SELL_ALL", 1.0, "LIQUIDITY_EMERGENCY")
        if multiple <= floor:
            return ExitDecision("SELL_ALL", 1.0, "TRAILING_OR_STOP")
        if int(age_ms) >= hold_ms:
            return ExitDecision("SELL_ALL", 1.0, "MAXIMUM_HOLD")
        return ExitDecision("HOLD", 0.0, "HOLD")

    def assert_finite(self) -> None:
        for section in (self.selector, self.position_sizing, self.exit_policy):
            for value in section.values():
                if isinstance(value, (int, float)) and not math.isfinite(float(value)):
                    raise ValueError("frozen policy contains non-finite numeric value")
