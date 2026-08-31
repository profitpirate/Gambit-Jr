#!/usr/bin/env python3
from __future__ import annotations

import re
from pathlib import Path


def replace_method(path: Path, method: str, replacement: str) -> None:
    text = path.read_text(encoding="utf-8")
    pattern = re.compile(
        rf"(?ms)^    def {re.escape(method)}\(self\).*?(?=^    def |^class |^if __name__)",
    )
    updated, count = pattern.subn(replacement.rstrip() + "\n\n", text, count=1)
    if count != 1:
        raise RuntimeError(f"could not uniquely replace {method} in {path}; matches={count}")
    path.write_text(updated, encoding="utf-8")


def main() -> int:
    replace_method(
        Path("tests/test_e4_hardening_v6.py"),
        "test_public_capital_burst_is_accepted",
        '''    def test_public_capital_burst_is_rejected_by_current_identity_policy(self) -> None:
        state = public_burst_state()
        accepted, _, _, reason, features = core.E4Policy(
            core.Settings(model_path=Path("missing.json"))
        ).entry(state)
        self.assertFalse(accepted)
        self.assertIn("identity", reason.lower())
        self.assertGreaterEqual(features["creator_buy_sol"], 2.5)''',
    )
    replace_method(
        Path("tests/test_e4_live.py"),
        "test_observed_bundled_microburst_can_enter",
        '''    def test_observed_bundled_microburst_cannot_bypass_identity_authority(self) -> None:
        state = e4_live.TokenState("mint")
        now = time.time_ns()
        state.apply(
            self.event(
                1,
                e4_live.EventKind.CREATE,
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
                    e4_live.EventKind.BUY,
                    0.001 * (1.0 + 0.11 * (index - 1)),
                    trader,
                    amount,
                    now + (index - 1) * 150_000,
                    signature=signature,
                    fdv=5_800,
                ),
                None,
            )
        accepted, _, _, reason, features = e4_live.E4Policy(
            e4_live.Settings(model_path=Path("missing.json"))
        ).entry(state)
        self.assertFalse(accepted)
        self.assertIn("identity", reason.lower())
        self.assertEqual(features["microburst_buyers"], 7)
        self.assertEqual(features["microburst_bundled_buys"], 6)''',
    )
    replace_method(
        Path("tests/test_e4_live.py"),
        "test_unbundled_fast_buyers_are_rejected",
        '''    def test_unbundled_fast_buyers_are_rejected(self) -> None:
        state = e4_live.TokenState("mint")
        now = time.time_ns()
        state.apply(
            self.event(1, e4_live.EventKind.CREATE, 0.001, "creator", 0, now, signature="create"),
            None,
        )
        for index in range(2, 10):
            state.apply(
                self.event(
                    index,
                    e4_live.EventKind.BUY,
                    0.001 * (1 + index * 0.1),
                    f"buyer-{index}",
                    2.0,
                    now + index * 100_000,
                    signature=f"single-{index}",
                ),
                None,
            )
        accepted, _, _, reason, _ = e4_live.E4Policy(
            e4_live.Settings(model_path=Path("missing.json"))
        ).entry(state)
        self.assertFalse(accepted)
        self.assertTrue(
            "identity" in reason.lower() or "creator seed" in reason.lower(),
            reason,
        )''',
    )
    print("migrated legacy E4 policy tests to V10 identity authority")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
