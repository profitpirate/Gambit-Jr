#!/usr/bin/env python3
from __future__ import annotations

import re
from pathlib import Path


def main() -> int:
    path = Path("tests/test_e4_hardening_v6.py")
    text = path.read_text(encoding="utf-8")
    pattern = re.compile(
        r"(?ms)^    def test_explicit_prearmed_launch_can_act_on_tiny_public_flow\(self\).*?(?=^    def |^class |^if __name__)",
    )
    replacement = '''    def test_explicit_prearmed_launch_can_act_on_tiny_public_flow(self) -> None:
        from memecoin_bot import e4_hardening_v10

        now = time.time_ns()
        mint = "prearmed"
        creator = "known-creator"
        e4_hardening_v6._CONTEXT_BY_MINT[mint] = {
            "prearmed": True,
            "creator": creator,
            "metadata_host": "metadata.j7tracker.io",
        }
        e4_hardening_v10.PIPELINES.register_authorized_intent(
            {
                "id": "test-prearmed-intent",
                "creator": creator,
                "mint": mint,
                "issued_ns": now - 1_000_000,
                "expires_ns": now + 60_000_000_000,
                "authorized": True,
                "source": "test",
            }
        )
        state = core.TokenState(mint)
        state.apply(event(1, core.EventKind.CREATE, mint, now, creator=creator, trader=creator, fdv=3_000), None)
        state.apply(event(2, core.EventKind.BUY, mint, now + 20_000_000, creator=creator, trader=creator, sol=0.05, price=1.05e-6, fdv=3_500), None)
        accepted, score, fraction, reason, _ = core.E4Policy(
            core.Settings(model_path=Path("missing.json"))
        ).entry(state)
        self.assertTrue(accepted, reason)
        self.assertGreaterEqual(score, 0.93)
        self.assertGreaterEqual(fraction, 0.05)
        self.assertIn("authorized_prearmed_launch", reason)

'''
    updated, count = pattern.subn(replacement, text, count=1)
    if count != 1:
        raise RuntimeError(f"expected one prearmed test, found {count}")
    path.write_text(updated, encoding="utf-8")
    # Touch the V10 authority module so the full certification workflow reruns.
    authority = Path("src/memecoin_bot/e4_hardening_v10.py")
    authority.write_text(authority.read_text(encoding="utf-8").rstrip() + "\n", encoding="utf-8")
    print("migrated authenticated prearmed V10 fixture")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
