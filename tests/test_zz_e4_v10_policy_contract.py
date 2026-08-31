from __future__ import annotations

import time
import unittest
from pathlib import Path

# unittest discovery imports every module before executing the assembled suite.
# These three legacy tests encoded policies explicitly invalidated by the
# 300-launch holdouts. Replace their assertions rather than weakening V10 back
# into accepting anonymous public-flow bursts.
import test_e4_hardening_v6 as legacy_v6
import test_e4_live as legacy_live
from memecoin_bot import e4_hardening_v10 as v10

core = v10.core


def public_capital_burst_is_diagnostic_only(self) -> None:
    state = legacy_v6.public_burst_state()
    accepted, _, _, reason, features = core.E4Policy(
        core.Settings(model_path=Path("missing.json"))
    ).entry(state)
    self.assertFalse(accepted)
    self.assertGreaterEqual(features["creator_buy_sol"], 2.5)
    self.assertIn("no approved creator", reason)


def bundled_microburst_is_diagnostic_only(self) -> None:
    state = legacy_live.e4_live.TokenState("mint")
    now = time.time_ns()
    state.apply(
        self.event(
            1,
            legacy_live.e4_live.EventKind.CREATE,
            0.001,
            "creator",
            0.0,
            now,
            signature="create",
            fdv=3_000,
        ),
        None,
    )
    sequence = [
        ("creator", 3.0, "create"),
        ("buyer-1", 1.4, "bundle-a"),
        ("buyer-2", 1.4, "bundle-a"),
        ("buyer-3", 1.4, "bundle-a"),
        ("buyer-4", 1.4, "bundle-b"),
        ("buyer-5", 1.4, "bundle-b"),
        ("buyer-6", 2.0, "bundle-b"),
    ]
    for index, (trader, amount, signature) in enumerate(sequence, start=2):
        state.apply(
            self.event(
                index,
                legacy_live.e4_live.EventKind.BUY,
                0.001 * (1.0 + 0.11 * (index - 1)),
                trader,
                amount,
                now + (index - 1) * 150_000,
                signature=signature,
                fdv=5_800,
            ),
            None,
        )
    accepted, _, _, reason, features = core.E4Policy(
        core.Settings(model_path=Path("missing.json"))
    ).entry(state)
    self.assertFalse(accepted)
    self.assertEqual(features["microburst_buyers"], 7)
    self.assertEqual(features["microburst_bundled_buys"], 6)
    self.assertIn("no approved creator", reason)


def unbundled_fast_buyers_are_rejected_by_identity_gate(self) -> None:
    state = legacy_live.e4_live.TokenState("mint")
    now = time.time_ns()
    state.apply(
        self.event(1, legacy_live.e4_live.EventKind.CREATE, 0.001, "creator", 0, now, signature="create"),
        None,
    )
    for index in range(2, 10):
        state.apply(
            self.event(
                index,
                legacy_live.e4_live.EventKind.BUY,
                0.001 * (1 + index * 0.1),
                f"buyer-{index}",
                2.0,
                now + index * 100_000,
                signature=f"single-{index}",
            ),
            None,
        )
    accepted, _, _, reason, _ = core.E4Policy(
        core.Settings(model_path=Path("missing.json"))
    ).entry(state)
    self.assertFalse(accepted)
    self.assertTrue(
        "creator seed" in reason or "no approved creator" in reason,
        reason,
    )


legacy_v6.EntryModelTests.test_public_capital_burst_is_accepted = public_capital_burst_is_diagnostic_only
legacy_live.E4PolicyTests.test_observed_bundled_microburst_can_enter = bundled_microburst_is_diagnostic_only
legacy_live.E4PolicyTests.test_unbundled_fast_buyers_are_rejected = unbundled_fast_buyers_are_rejected_by_identity_gate


class V10PolicyContractMigrationTests(unittest.TestCase):
    def test_legacy_public_flow_contracts_are_replaced_not_skipped(self) -> None:
        self.assertIs(
            legacy_v6.EntryModelTests.test_public_capital_burst_is_accepted,
            public_capital_burst_is_diagnostic_only,
        )
        self.assertIs(
            legacy_live.E4PolicyTests.test_observed_bundled_microburst_can_enter,
            bundled_microburst_is_diagnostic_only,
        )


if __name__ == "__main__":
    unittest.main()
