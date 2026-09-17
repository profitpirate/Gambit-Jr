from __future__ import annotations

import json
import unittest
from pathlib import Path

from scripts import e4_trade_library as lib
from src.memecoin_bot.full3s_v2_library import Full3SV2LibraryPolicy, LibraryIndex


class E4LibraryTests(unittest.TestCase):
    def test_frozen_e4_backfill_is_complete(self):
        expectation = lib.load_json(Path("models/e4/e4-creator-expectancy.json"))
        evidence = lib.load_json(Path("models/e4/e4-v12-forward-evidence.json"))
        tsv = lib.load_winner_tsv(sorted(Path("models/e4").glob("winning-mints-*.tsv")))
        rows = lib.build_e4_records(expectation, evidence, tsv)
        self.assertEqual(316, len(rows))
        self.assertEqual(242, sum(r["outcome"] == "WIN" for r in rows))
        self.assertEqual(74, sum(r["outcome"] == "LOSS" for r in rows))
        self.assertEqual(316, len({r["mint"] for r in rows}))
        self.assertTrue(all(r["creator"] != lib.UNKNOWN_CREATOR for r in rows))

    def test_golden_bunch_rule_matches_frozen_creator_statistics(self):
        expectation = lib.load_json(Path("models/e4/e4-creator-expectancy.json"))
        rows = lib.build_e4_records(expectation, {}, {})
        dev = lib.developer_index(rows)
        actual = sum(d["golden_bunch"] for d in dev.values())
        self.assertEqual(expectation["repeat_pure_winner_creators"], actual)
        for d in dev.values():
            if d["golden_bunch"]:
                self.assertGreaterEqual(d["e4"]["wins"], 2)
                self.assertEqual(0, d["e4"]["losses"])

    def test_missing_post_exit_data_stays_unknown(self):
        p = lib.post_exit_from({"mint": "M", "net_pnl_sol": 1.0})
        self.assertEqual("UNKNOWN", p["status"])
        self.assertIsNone(p["went_higher"])
        self.assertIsNone(p["went_lower"])

    def test_hg_extractor_separates_shadow_and_skips_observer(self):
        state = {
            "campaign_id": "C",
            "engine": {
                "decisions": {"M": {"mint": "M", "creator": "DEV", "decision_ns": 10}},
                "controls": {"accounts": {"FULL_3S": {"ledger": [{
                    "mint": "M", "signal_id": "1:M", "status": "CLOSED", "entry_ns": 11, "exit_ns": 20,
                    "budget": .1, "gross_pnl_sol": .02, "net_pnl_sol": .01, "net_return": .1,
                    "post": {"complete": True, "coverage": "OBSERVED", "went_higher": True, "went_lower": False}
                }]}, "FULL_4S": {"ledger": []}, "FULL_7S": {"ledger": []}}, "rejections": []},
                "v2": {
                    "candidate": {"engine": {"accounts": {"FULL_3S": {"ledger": [{
                        "mint": "M", "signal_id": "1:M", "status": "CLOSED", "entry_ns": 11, "exit_ns": 20,
                        "budget": .1, "gross_pnl_sol": .02, "net_pnl_sol": .01, "net_return": .1,
                        "post": {"complete": True, "coverage": "OBSERVED", "went_higher": True, "went_lower": False}
                    }]}}}},
                    "observer": {"engine": {"accounts": {"FULL_3S": {"ledger": [{
                        "mint": "M", "signal_id": "1:M", "status": "CLOSED", "entry_ns": 11, "exit_ns": 20,
                        "budget": .1, "gross_pnl_sol": .02, "net_pnl_sol": .01, "net_return": .1
                    }]}}}}
                },
                "loss_control": {"accounts": {"FULL_3S": {"ledger": [{
                    "mint": "M", "signal_id": "1:M", "status": "CLOSED", "entry_ns": 11, "exit_ns": 19,
                    "budget": .1, "gross_pnl_sol": .01, "net_pnl_sol": -.01, "net_return": -.1
                }]}}, "rejections": [{"mint": "R", "reason": "TEST", "ns": 15}]},
            }
        }
        rows = lib.extract_hg_records(state, {"checkpoint_kind": "fixture", "run_id": 1})
        models = {r["model"] for r in rows if r["record_type"] == "TRADE"}
        self.assertIn("FULL_3S", models)
        self.assertIn("FULL_3S_V2", models)
        self.assertIn("FULL_3S_2S_LOSS_EXIT", models)
        self.assertEqual(3, sum(r["record_type"] == "TRADE" for r in rows))
        self.assertEqual(1, sum(r["record_type"] == "REJECTION" for r in rows))

    def test_library_policy_refuses_retroactive_label_leakage(self):
        data = {"snapshot_available_ns": 100, "records": [
            {"record_id": "1", "creator": "D", "source": "E4", "record_type": "TRADE", "outcome": "WIN"},
            {"record_id": "2", "creator": "D", "source": "E4", "record_type": "TRADE", "outcome": "WIN"},
        ]}
        idx = LibraryIndex(data)
        self.assertFalse(idx.profile("D", 99)["causal"])
        p = idx.profile("D", 101)
        self.assertTrue(p["causal"])
        self.assertEqual("GOLDEN_BUNCH", p["classification"])
        result = Full3SV2LibraryPolicy(idx).evaluate(baseline_accept=True, creator="D", decision_ns=101)
        self.assertTrue(result["accept"])

    def test_generated_snapshot_has_hg_completion_data(self):
        path = Path("models/e4/library/e4-hg-trade-library.json")
        if not path.exists():
            self.skipTest("snapshot is generated by the library workflow")
        d = json.loads(path.read_text())
        self.assertEqual(316, d["counts"]["e4_trades"])
        self.assertEqual(242, d["counts"]["e4_wins"])
        self.assertEqual(74, d["counts"]["e4_losses"])
        self.assertGreater(d["counts"]["hg_trades"], 0)
        self.assertGreater(d["counts"]["hg_rejections"], 0)
        self.assertEqual(22, d["counts"]["golden_bunch_developers"])
        self.assertTrue(all(x.get("terminal_checkpoint") for x in d["hg_checkpoints"]))


if __name__ == "__main__":
    unittest.main()
