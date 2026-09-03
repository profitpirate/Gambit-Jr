from __future__ import annotations

import unittest

from scripts.e4_v12_forward_accumulate import aggregate


class V12CopyOnlyEvidenceTests(unittest.TestCase):
    def _position(self, mint: str, pnl: float, hold_ms: float = 1000.0) -> dict[str, object]:
        return {
            "mint": mint,
            "entry_ns": 1,
            "pnl_sol": pnl,
            "hold_ms": hold_ms,
            "entry_fdv_usd": 4000.0,
        }

    def test_non_copy_positions_cannot_change_direct_copy_win_rate(self) -> None:
        direct_loss = self._position("direct", -0.01)
        non_copy_wins = [self._position(f"extra-{index}", 0.10) for index in range(10)]
        evidence = {
            "batches": [{"batch_id": "1", "launches": 3000, "reentries": 0, "max_concurrent_positions": 1}],
            "gambit_positions": {"direct": direct_loss, **{f"extra-{i}": row for i, row in enumerate(non_copy_wins)}},
            "direct_copy_positions": {"direct": direct_loss},
            "non_copy_positions": {f"extra-{i}": row for i, row in enumerate(non_copy_wins)},
            "same_window_e4_positions": {"e4-direct": self._position("direct", 1.0)},
            "copy_audits": {
                "1": {
                    "comparison": {"direct_copy_decisions": 1, "direct_copy_filled_candidates": 1},
                    "direct_copy_trades": [{
                        "mint": "direct",
                        "source_to_decision_ms": 0.0,
                        "decision_to_fill_ms": 95.0,
                        "source_to_fill_ms": 95.0,
                        "fill_drift_bps": 1200.0,
                    }],
                    "non_copy_family_counts": {"v12_recent_e4_repeat_launch": 10},
                }
            },
        }
        summary = aggregate(evidence)
        self.assertEqual(summary["gambit_closed_positions"], 11)
        self.assertAlmostEqual(summary["gambit_net_win_rate"], 10 / 11)
        self.assertEqual(summary["direct_copy_closed_positions"], 1)
        self.assertEqual(summary["direct_copy_net_win_rate"], 0.0)
        self.assertEqual(summary["direct_copy_net_pnl_sol"], -0.01)
        self.assertEqual(summary["non_copy_closed_positions"], 10)
        self.assertEqual(summary["non_copy_net_win_rate"], 1.0)
        self.assertEqual(summary["direct_copy_trade_capture"], 1.0)
        self.assertEqual(summary["direct_copy_winner_mint_capture"], 1.0)
        self.assertEqual(summary["direct_copy_both_won"], 0)
        self.assertEqual(summary["direct_copy_median_decision_to_fill_ms"], 95.0)
        self.assertEqual(summary["direct_copy_median_fill_drift_bps"], 1200.0)
        self.assertEqual(summary["classification"], "INSUFFICIENT_COPY_ONLY_EVIDENCE")

    def test_missing_copy_audit_is_explicit_not_silently_mixed(self) -> None:
        evidence = {
            "batches": [{"batch_id": "1", "launches": 3000, "reentries": 0, "max_concurrent_positions": 1}],
            "gambit_positions": {"a": self._position("a", 1.0)},
            "direct_copy_positions": {},
            "non_copy_positions": {},
            "same_window_e4_positions": {"a": self._position("a", 1.0)},
            "copy_audits": {},
        }
        summary = aggregate(evidence)
        self.assertEqual(summary["classification"], "COPY_AUDIT_MISSING")
        self.assertEqual(summary["direct_copy_closed_positions"], 0)
        self.assertEqual(summary["gambit_closed_positions"], 1)

    def test_copy_only_sufficiency_counts_only_audited_batches(self) -> None:
        direct = {f"d-{i}": self._position(f"d-{i}", 0.01 if i < 70 else -0.005) for i in range(100)}
        evidence = {
            "batches": [
                {"batch_id": "1", "launches": 3000, "reentries": 0, "max_concurrent_positions": 1},
                {"batch_id": "2", "launches": 3000, "reentries": 0, "max_concurrent_positions": 1},
                {"batch_id": "3", "launches": 3000, "reentries": 0, "max_concurrent_positions": 1},
            ],
            "gambit_positions": direct,
            "direct_copy_positions": direct,
            "non_copy_positions": {},
            "same_window_e4_positions": {},
            "copy_audits": {
                "1": {"comparison": {}, "direct_copy_trades": [], "non_copy_family_counts": {}},
                "2": {"comparison": {}, "direct_copy_trades": [], "non_copy_family_counts": {}},
            },
        }
        summary = aggregate(evidence)
        self.assertEqual(summary["copy_audited_batch_count"], 2)
        self.assertFalse(summary["sufficient_evidence"])
        self.assertEqual(summary["classification"], "INSUFFICIENT_COPY_ONLY_EVIDENCE")


if __name__ == "__main__":
    unittest.main()
