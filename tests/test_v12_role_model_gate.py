from __future__ import annotations

import unittest

from scripts.e4_v12_forward_accumulate import (
    E4_HISTORICAL_NET_PF,
    E4_HISTORICAL_NET_POSITIONS,
    E4_HISTORICAL_NET_WR,
    V12_ROLE_MODEL_MIN_PF,
    V12_ROLE_MODEL_MIN_POSITIONS,
    V12_ROLE_MODEL_MIN_WR,
    aggregate,
)
from scripts.e4_v12_role_model_gate import evaluate_evidence


def evidence_for(*, wins: int, losses: int, profit_factor: float, broken_invariant: bool = False):
    positions = {}
    for index in range(wins):
        positions[f"winner-{index}|{index}"] = {
            "mint": f"winner-{index}",
            "entry_ns": index,
            "pnl_sol": 1.0,
            "hold_ms": 4000,
            "entry_fdv_usd": 4900.0,
        }
    loss_value = wins / (profit_factor * losses) if losses else 0.0
    for index in range(losses):
        positions[f"loser-{index}|{wins + index}"] = {
            "mint": f"loser-{index}",
            "entry_ns": wins + index,
            "pnl_sol": -loss_value,
            "hold_ms": 4000,
            "entry_fdv_usd": 4900.0,
        }
    batches = [
        {
            "batch_id": str(index),
            "launches": 3000,
            "reentries": 1 if broken_invariant and index == 0 else 0,
            "max_concurrent_positions": 1,
        }
        for index in range(3)
    ]
    return {
        "version": "e4-v12-forward-evidence-v1",
        "strategy_fingerprint": "synthetic-v12",
        "batches": batches,
        "gambit_positions": positions,
        "same_window_e4_positions": {},
    }


class V12RoleModelGateTests(unittest.TestCase):
    def test_v12_targets_are_the_exact_e4_role_model_benchmarks(self):
        self.assertEqual(V12_ROLE_MODEL_MIN_WR, E4_HISTORICAL_NET_WR)
        self.assertEqual(V12_ROLE_MODEL_MIN_PF, E4_HISTORICAL_NET_PF)
        self.assertEqual(V12_ROLE_MODEL_MIN_POSITIONS, E4_HISTORICAL_NET_POSITIONS)
        self.assertEqual(V12_ROLE_MODEL_MIN_POSITIONS, 258)
        self.assertEqual(155, round(V12_ROLE_MODEL_MIN_WR * V12_ROLE_MODEL_MIN_POSITIONS))

    def test_exact_e4_role_model_cohort_passes(self):
        evidence = evidence_for(wins=155, losses=103, profit_factor=4.92)
        passed, summary = evaluate_evidence(evidence)
        self.assertTrue(passed)
        self.assertTrue(summary["sufficient_evidence"])
        self.assertEqual(summary["classification"], "E4_ROLE_MODEL_TARGETS_MET")
        self.assertAlmostEqual(summary["gambit_net_win_rate"], 155 / 258)
        self.assertAlmostEqual(summary["gambit_profit_factor"], 4.92)
        self.assertTrue(all(summary["role_model_checks"].values()))

    def test_previous_relaxed_gate_can_no_longer_pass(self):
        evidence = evidence_for(wins=53, losses=47, profit_factor=2.10)
        summary = aggregate(evidence)
        old_relaxed_gate_would_pass = (
            summary["gambit_net_win_rate"] >= E4_HISTORICAL_NET_WR - 0.08
            and summary["gambit_profit_factor"] >= 2.0
            and summary["gambit_net_pnl_sol"] > 0
        )
        self.assertTrue(old_relaxed_gate_would_pass)
        self.assertFalse(summary["role_model_targets_met"])
        self.assertFalse(summary["sufficient_evidence"])
        self.assertEqual(summary["classification"], "INSUFFICIENT_EVIDENCE")

    def test_full_sample_below_e4_win_rate_fails(self):
        evidence = evidence_for(wins=154, losses=104, profit_factor=5.0)
        summary = aggregate(evidence)
        self.assertTrue(summary["sufficient_evidence"])
        self.assertFalse(summary["role_model_checks"]["win_rate"])
        self.assertFalse(summary["role_model_targets_met"])
        self.assertEqual(summary["classification"], "FAILED_E4_ROLE_MODEL_TARGETS")

    def test_full_sample_below_e4_profit_factor_fails(self):
        evidence = evidence_for(wins=155, losses=103, profit_factor=4.91)
        summary = aggregate(evidence)
        self.assertTrue(summary["sufficient_evidence"])
        self.assertFalse(summary["role_model_checks"]["profit_factor"])
        self.assertFalse(summary["role_model_targets_met"])
        self.assertEqual(summary["classification"], "FAILED_E4_ROLE_MODEL_TARGETS")

    def test_reentry_or_concurrency_invariant_failure_blocks_certification(self):
        evidence = evidence_for(
            wins=155,
            losses=103,
            profit_factor=4.92,
            broken_invariant=True,
        )
        summary = aggregate(evidence)
        self.assertFalse(summary["invariants_ok"])
        self.assertFalse(summary["role_model_targets_met"])
        self.assertFalse(summary["sufficient_evidence"])
        self.assertEqual(summary["classification"], "INSUFFICIENT_EVIDENCE")


if __name__ == "__main__":
    unittest.main()
