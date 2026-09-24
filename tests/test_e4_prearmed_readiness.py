from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from memecoin_bot.e4_prearmed_policy import FrozenPrearmedPolicy
from memecoin_bot.e4_prearmed_readiness import (
    FROZEN_MODEL_SHA256,
    readiness,
    secret_environment_issues,
    stable_hash,
    validate_keypair_file,
)


ROOT = Path(__file__).resolve().parents[1]
MODEL_PATH = ROOT / "research" / "v12-pre-armed-100-trade-frozen-model.json"
CERT_PATH = ROOT / "research" / "v12-pre-armed-certification-status.json"


class FrozenPrearmedPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.model = json.loads(MODEL_PATH.read_text(encoding="utf-8"))
        cls.policy = FrozenPrearmedPolicy(cls.model)
        cls.creator = "29yFzeBZgxf5zqrAkKXwgZtQehRf4pL8WbV2nRJikbw8"
        cls.handle = "ddddddd8a"
        cls.create_ns = 10_000_000_000

    def test_frozen_hash_and_family_are_exact(self) -> None:
        self.assertEqual(stable_hash(self.model), FROZEN_MODEL_SHA256)
        self.assertEqual(self.model["selector"]["family"], "prearmed_repeat_creator_social_status")

    def test_selector_accepts_exact_frozen_conditions(self) -> None:
        decision = self.policy.select(
            creator=self.creator,
            social_handle=self.handle,
            social_status_ns=self.create_ns - 1_000_000_000,
            create_ns=self.create_ns,
            creator_seed_sol=2.0,
            mayhem_mode=False,
        )
        self.assertTrue(decision.accepted)
        self.assertEqual(decision.position_fraction, 0.10)

    def test_selector_rejects_wrong_handle(self) -> None:
        decision = self.policy.select(
            creator=self.creator,
            social_handle="definitely_not_frozen",
            social_status_ns=self.create_ns - 1_000_000_000,
            create_ns=self.create_ns,
            creator_seed_sol=2.0,
            mayhem_mode=False,
        )
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, "not_prearmed_handle")

    def test_selector_rejects_social_older_than_ten_seconds(self) -> None:
        decision = self.policy.select(
            creator=self.creator,
            social_handle=self.handle,
            social_status_ns=self.create_ns - 10_000_000_001,
            create_ns=self.create_ns,
            creator_seed_sol=2.0,
            mayhem_mode=False,
        )
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, "social_age_outside_window")

    def test_selector_rejects_seed_below_floor(self) -> None:
        decision = self.policy.select(
            creator=self.creator,
            social_handle=self.handle,
            social_status_ns=self.create_ns - 1_000_000_000,
            create_ns=self.create_ns,
            creator_seed_sol=1.999999999,
            mayhem_mode=False,
        )
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, "creator_seed_below_floor")

    def test_selector_rejects_mayhem(self) -> None:
        decision = self.policy.select(
            creator=self.creator,
            social_handle=self.handle,
            social_status_ns=self.create_ns - 1_000_000_000,
            create_ns=self.create_ns,
            creator_seed_sol=2.0,
            mayhem_mode=True,
        )
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, "mayhem")

    def test_entry_guard_exact_boundaries(self) -> None:
        accepted = self.policy.entry_guard(
            expected_token_output=100.0,
            current_token_output=65.0,
            create_price=1.0,
            fill_price=1.5,
        )
        self.assertTrue(accepted.accepted)

        output_fail = self.policy.entry_guard(
            expected_token_output=100.0,
            current_token_output=64.999999,
            create_price=1.0,
            fill_price=1.0,
        )
        self.assertFalse(output_fail.accepted)
        self.assertEqual(output_fail.reason, "entry_output_guard_rejected")

        chase_fail = self.policy.entry_guard(
            expected_token_output=100.0,
            current_token_output=100.0,
            create_price=1.0,
            fill_price=1.500001,
        )
        self.assertFalse(chase_fail.accepted)
        self.assertEqual(chase_fail.reason, "entry_chase_guard_rejected")

    def test_exit_first_take_trail_and_max_hold(self) -> None:
        first = self.policy.exit(
            entry_price=1.0,
            current_price=1.15,
            peak_price=1.15,
            first_partial_done=False,
            age_ms=500,
        )
        self.assertEqual((first.action, first.fraction), ("SELL_PARTIAL", 0.30))

        trail = self.policy.exit(
            entry_price=1.0,
            current_price=1.04,
            peak_price=1.40,
            first_partial_done=True,
            age_ms=800,
        )
        self.assertEqual((trail.action, trail.fraction), ("SELL_ALL", 1.0))
        self.assertEqual(trail.reason, "TRAILING_OR_STOP")

        hold = self.policy.exit(
            entry_price=1.0,
            current_price=1.10,
            peak_price=1.10,
            first_partial_done=False,
            age_ms=2000,
        )
        self.assertEqual((hold.action, hold.fraction), ("SELL_ALL", 1.0))
        self.assertEqual(hold.reason, "MAXIMUM_HOLD")


class PrearmedReadinessTests(unittest.TestCase):
    def test_recertified_baseline_and_invalidated_sample_boundary(self) -> None:
        cert = json.loads(CERT_PATH.read_text(encoding="utf-8"))
        self.assertTrue(cert["official_50_trade_baseline_valid"])
        metrics = cert["official_50_trade_metrics"]
        self.assertEqual((metrics["wins"], metrics["losses"]), (35, 15))
        self.assertAlmostEqual(metrics["win_rate"], 0.70)
        self.assertAlmostEqual(metrics["net_pnl_sol"], 2.086015102090403)
        self.assertAlmostEqual(metrics["profit_factor"], 6.00625698411455)
        self.assertAlmostEqual(
            metrics["maximum_closed_equity_drawdown_fraction"],
            0.029421494624602456,
        )
        self.assertEqual(cert["causal_exit_recertification"]["impossible_exit_count_after"], 0)
        self.assertTrue(cert["pre_fix_100_trade_sample_invalidated"])
        self.assertFalse(cert["pre_fix_100_trade_sample_imported"])

    def test_current_readiness_has_no_model_or_baseline_integrity_failure(self) -> None:
        report = readiness()
        blockers = set(report["blockers"])
        self.assertNotIn("FROZEN_MODEL_HASH_MISMATCH", blockers)
        self.assertNotIn("FROZEN_SELECTOR_FAMILY_MISMATCH", blockers)
        self.assertNotIn("OFFICIAL_50_TRADE_BASELINE_INVALID", blockers)
        self.assertNotIn("INVALIDATED_19_TRADE_SAMPLE_NOT_MARKED_INVALID", blockers)
        self.assertNotIn("INVALIDATED_19_TRADE_SAMPLE_IMPORTED", blockers)

    def test_forbidden_secret_environment_is_fail_closed(self) -> None:
        with patch.dict(os.environ, {"E4_SEED_PHRASE": "do-not-ever-store-this-here"}, clear=False):
            issues = secret_environment_issues()
        self.assertIn("FORBIDDEN_SECRET_ENV:E4_SEED_PHRASE", issues)

    def test_keypair_permissions_must_be_private(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "keypair.json"
            path.write_text("[1,2,3]", encoding="utf-8")
            os.chmod(path, 0o644)
            self.assertIn("KEYPAIR_PERMISSIONS_TOO_OPEN", validate_keypair_file(path))
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
            self.assertNotIn("KEYPAIR_PERMISSIONS_TOO_OPEN", validate_keypair_file(path))


if __name__ == "__main__":
    unittest.main()
