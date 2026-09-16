"""Fresh forward A/B: parent 3s models versus a single 2s net-loss checkpoint.

PAPER ONLY. The two shadow arms receive the same parent-admitted signals and the
same observed reserve states. At the first valid management tick at/after 2.0s
from the modelled BUY fill, a shadow exits all remaining inventory iff its
current net mark-to-market return (including modelled exit costs) is negative.
A non-negative checkpoint permanently passes; the parent 3s horizon then stays
unchanged even if the trade later turns red.

The checkpoint schedules a sell intent at 2s; the existing execution model still
applies its configured inclusion delay (250ms in the registered campaign).
"""
from __future__ import annotations

import copy
import math
from dataclasses import asdict
from typing import Any

# Apply the corrected event-stream liveness semantics before subclassing V2.
import golden_top4_v2_eventstream as _eventstream_fix  # noqa: F401
import golden_full3s_v2 as v2
import golden_top4_engine as top4

FULL2 = "FULL_3S_2S_LOSS_EXIT"
V2_LOSS2 = "FULL_3S_V2_2S_LOSS_EXIT"
ARMS = (*top4.ARMS, FULL2, V2_LOSS2)
VERSION = "golden-loss2s-forward-1"
CHECKPOINT_MS = 2000
Config = top4.Config
write_json = top4.write_json


class _Loss2sCheckpointMixin:
    checkpoint_ms = CHECKPOINT_MS

    def _mark(self, p: dict[str, Any], s: dict[str, Any], now: int) -> None:
        elapsed_ms = (now - p["entry_ns"]) / 1e6
        triggered_now = False
        if (
            p["status"] == "OPEN"
            and p.get("intent") is None
            and not p.get("loss2s_checked", False)
            and elapsed_ms >= self.checkpoint_ms
        ):
            value = v2.base.sell_quote(p["remaining"], s, p["ds"], p["dt"])
            net = (
                p["sell_gross"]
                + value * (1 - self.c.percent)
                - self.c.fixed
                - p["curve_sol"]
                - sum(p["fees"].values())
            )
            ret = net / p["buy_cost"]
            p["loss2s_checked"] = True
            p["loss2s_checkpoint_ns"] = now
            p["loss2s_checkpoint_elapsed_ms"] = elapsed_ms
            p["loss2s_checkpoint_net_return"] = ret
            p["loss2s_triggered"] = ret < 0
            p["parent_horizon_ms"] = p["horizon_ms"]
            if ret < 0:
                # Let the frozen parent _mark create the ordinary full-exit intent,
                # then relabel it so reporting identifies the causal rule.
                p["horizon_ms"] = self.checkpoint_ms
                triggered_now = True

        super()._mark(p, s, now)

        if triggered_now and p.get("intent") is not None:
            p["intent"]["reason"] = "LOSS_2S_CHECKPOINT"
            p["intent"]["scheduled_ns"] = p["entry_ns"] + self.checkpoint_ms * 1_000_000


class LossControlEngine(_Loss2sCheckpointMixin, v2.base.Engine):
    """FULL_3S accounting plus the one-shot 2s loss checkpoint."""


class LossV2Engine(_Loss2sCheckpointMixin, v2.SingleEngine):
    """Corrected-event-stream FULL_3S_V2 execution plus the same checkpoint."""


class CampaignEngine(top4.CampaignEngine):
    """Original four accounts plus two isolated loss-checkpoint shadow accounts."""

    last_instance = None

    def __init__(self, config: Config, state=None, *, fixture_mode: bool = False):
        self.loss_control = None
        self.loss_v2 = None
        self.loss_control_uncertain: set[str] = set()

        parent_state = None
        if state is not None:
            if state.get("version") != VERSION:
                raise ValueError("wrong loss2s campaign state version")
            parent_state = copy.deepcopy(state)
            parent_state["version"] = top4.VERSION

        # During parent restore, top4 calls self.validate(). The overrides below
        # deliberately expose only the parent four until shadow engines exist.
        super().__init__(config, parent_state, fixture_mode=fixture_mode)

        shadow_config = v2.Config(**asdict(config))
        if state is None:
            self.loss_control = LossControlEngine(shadow_config)
            self.loss_v2 = LossV2Engine(
                shadow_config, quote_age_ms=self.runtime.pc.max_quote_age_ms
            )
        else:
            self.loss_control = LossControlEngine(shadow_config, state["loss_control"])
            self.loss_v2 = LossV2Engine(
                shadow_config,
                state["loss_v2"],
                quote_age_ms=self.runtime.pc.max_quote_age_ms,
            )
            self.loss_control_uncertain = set(state.get("loss_control_uncertain", []))

        CampaignEngine.last_instance = self
        self.validate()

    @property
    def errors(self):
        rows = list(super().errors)
        if self.loss_control is not None:
            rows += self.loss_control.errors
        if self.loss_v2 is not None:
            rows += self.loss_v2.errors
        return list(dict.fromkeys(rows))

    @property
    def accounts(self):
        out = {
            **self.controls.accounts,
            top4.V2: self.runtime.candidate.accounts[v2.ARM],
        }
        if self.loss_control is not None:
            out[FULL2] = self.loss_control.accounts[v2.ARM]
        if self.loss_v2 is not None:
            out[V2_LOSS2] = self.loss_v2.accounts[v2.ARM]
        return out

    def all_engines(self):
        out = [self.controls, self.runtime.candidate, self.runtime.observer]
        if self.loss_control is not None:
            out.append(self.loss_control)
        if self.loss_v2 is not None:
            out.append(self.loss_v2)
        return tuple(out)

    def submit(self, d, s, now):
        result = super().submit(d, s, now)
        mint = d["mint"]
        if self.smoke or mint not in self.decisions:
            return result

        control_shadow = False
        v2_shadow = False

        # Pair each shadow only with an admission its canonical parent actually
        # obtained. Shadow account divergence after that is a real policy effect.
        if mint in self.controls.bundles:
            control_shadow = self.loss_control.submit(
                copy.deepcopy(d), copy.deepcopy(s), now
            )
        if mint in self.runtime.candidate.seen_mints:
            v2_shadow = self.loss_v2.submit(
                copy.deepcopy(d), copy.deepcopy(s), now
            )

        self._event(
            {
                "kind": "LOSS2S_SHADOW_SELECTION",
                "mint": mint,
                "decision": d,
                "full3_parent_admitted": mint in self.controls.bundles,
                "full3_shadow_submitted": control_shadow,
                "v2_parent_admitted": mint in self.runtime.candidate.seen_mints,
                "v2_shadow_submitted": v2_shadow,
                "checkpoint_ms": CHECKPOINT_MS,
                "checkpoint_rule": "single net-after-costs mark < 0",
            }
        )
        return bool(result or control_shadow or v2_shadow)

    @staticmethod
    def _engine_positions(display_arm, engine, underlying_arm):
        a = engine.accounts[underlying_arm]
        positions = {
            p["signal_id"]: p for p in a["ledger"] + a["aborted_entries"]
        }
        for b in engine.bundles.values():
            positions[b["signal_id"]] = b["positions"][underlying_arm]
        for p in positions.values():
            yield display_arm, p

    def positions(self):
        for arm in top4.CONTROLS:
            yield from self._engine_positions(arm, self.controls, arm)
        yield from self._engine_positions(top4.V2, self.runtime.candidate, v2.ARM)
        if self.loss_control is not None:
            yield from self._engine_positions(FULL2, self.loss_control, v2.ARM)
        if self.loss_v2 is not None:
            yield from self._engine_positions(V2_LOSS2, self.loss_v2, v2.ARM)

    def tick(self, now, states, last_feed_ns):
        if now < self.last_now:
            super().tick(now, states, last_feed_ns)
            return

        if now - last_feed_ns > self.c.feed_stale_ms * 1_000_000:
            self.loss_control_uncertain.update(self.loss_control.bundles)

        # Parent tick remains byte-for-byte behavior through its original engines.
        super().tick(now, states, last_feed_ns)
        self.loss_control.tick(now, states, last_feed_ns)
        self.loss_v2.tick(now, states, last_feed_ns)

        self._propagate_proofs()
        # Parent tick emitted any shadow actions that already existed. Emit again
        # after shadow management so newly created intents/fills are journaled.
        self.emit_actions()

    def _uncertain_for(self, arm: str) -> set[str]:
        if arm in top4.CONTROLS:
            return self.control_uncertain
        if arm == top4.V2:
            return self.runtime.candidate.uncertain_mints
        if arm == FULL2:
            return self.loss_control_uncertain
        if arm == V2_LOSS2:
            return self.loss_v2.uncertain_mints
        raise KeyError(arm)

    def verified_mints(self, arm: str) -> set[str]:
        if self.fixture_mode:
            return set()
        self._propagate_proofs()
        uncertain = self._uncertain_for(arm)
        return {
            p["mint"]
            for p in self.accounts[arm]["ledger"]
            if p["mint"] not in uncertain
            and self.proofs.get(p["mint"], {}).get("verified") is True
            and (p.get("post") or {}).get("complete") is True
            and (p.get("post") or {}).get("coverage") == "OBSERVED"
        }

    def forward_counts(self):
        return {arm: len(self.verified_mints(arm)) for arm in ARMS}

    def summary(self):
        rows = self.controls.summary()
        rows[top4.V2] = self.runtime.candidate.summary()[v2.ARM]
        if self.loss_control is not None:
            rows[FULL2] = self.loss_control.summary()[v2.ARM]
            rows[V2_LOSS2] = self.loss_v2.summary()[v2.ARM]

        counts = self.forward_counts()
        for arm, row in rows.items():
            row["verified_forward_closed"] = counts.get(arm, 0)
            row["unverified_or_incomplete_count"] = (
                row["closed_trades"] - counts.get(arm, 0)
            )
            if arm in (FULL2, V2_LOSS2):
                ledger = self.accounts[arm]["ledger"]
                row.update(
                    parent_model=top4.V2 if arm == V2_LOSS2 else "FULL_3S",
                    loss_checkpoint_ms=CHECKPOINT_MS,
                    loss_checkpoint_rule="one-shot net-after-costs mark < 0",
                    loss_checkpoint_triggered=sum(
                        bool(p.get("loss2s_triggered")) for p in ledger
                    ),
                    loss_checkpoint_passed=sum(
                        p.get("loss2s_checked") is True
                        and not p.get("loss2s_triggered", False)
                        for p in ledger
                    ),
                )
        return rows

    def persistence(self):
        data = super().persistence()
        data.update(
            version=VERSION,
            experiment="golden-2s-loss-exit-ab",
            loss_control=self.loss_control.persistence(),
            loss_v2=self.loss_v2.snapshot(),
            loss_control_uncertain=sorted(self.loss_control_uncertain),
            checkpoint_ms=CHECKPOINT_MS,
            checkpoint_semantics="single checkpoint; negative net mark exits; otherwise parent 3s horizon",
        )
        return data

    def validate(self):
        # This branch is used while top4's restore constructor is rebuilding the
        # parent four, before the shadow engines have been restored.
        if self.loss_control is None or self.loss_v2 is None:
            self.controls.validate()
            self.runtime.candidate.validate()
            self.runtime.observer.validate()
            expected = set(top4.ARMS)
        else:
            self.controls.validate()
            self.runtime.candidate.validate()
            self.runtime.observer.validate()
            self.loss_control.validate()
            self.loss_v2.validate()
            expected = set(ARMS)

        if set(self.accounts) != expected:
            raise ValueError("wrong loss2s model set")
        if len({e["event_id"] for e in self.events}) != len(self.events):
            raise ValueError("duplicate report event IDs")

        for arm, p in self.positions():
            key = arm + ":" + p["signal_id"]
            if self.action_cursors.get(key, 0) != len(p["actions"]):
                raise ValueError("unreported transaction attempt")
            if p["status"] == "CLOSED":
                fees = sum(
                    sum(x.get("fees", {}).values()) for x in p["actions"]
                )
                if not math.isclose(
                    fees, sum(p["fees"].values()), abs_tol=1e-9
                ):
                    raise ValueError("attempt fees do not reconcile")
