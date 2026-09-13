from __future__ import annotations

import argparse
import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import e4_v12_social_prearm_runner as runner
runner.install_compatibility()
from scripts import e4_v12_golden_thesis_search as golden
from scripts import e4_v12_social_prearm_search as social
from scripts import e4_v12_true_latency_replay as economics
from scripts import e4_v12_golden_live_report as report


class SocialPipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cache = {}
        self.pairs = []
        for index in range(11):
            directory = self.root / f"r{index}" / "artifacts"
            directory.mkdir(parents=True)
            positions, events = [], []
            for number in range(3):
                mint = f"fixture-{index}-{number}"
                start = 1_785_000_000_000_000_000 + (index*100 + number*3)*1_000_000_000
                creator = f"creator-{number}"
                for offset, kind, trader, sol, tokens in (
                    (0, "CREATE", creator, 30.0, 500_000_000.0),
                    (20_000_000, "BUY", economics.E4_WALLET, 40.0, 400_000_000.0),
                    (400_000_000, "SELL", economics.E4_WALLET, 60.0, 300_000_000.0),
                ):
                    events.append({"mint": mint, "received_ns": start+offset, "kind": kind,
                        "creator": creator, "trader": trader, "slot": index*100+number,
                        "event_index": len(events), "signature": f"{mint}-{kind}",
                        "fdv_usd": 5000.0, "price_sol": sol/tokens, "sol_amount": 0.1,
                        "token_amount": 1_000_000.0,
                        "raw": {"virtual_sol_reserves": sol, "virtual_token_reserves": tokens,
                                "real_token_reserves": 200_000_000.0}})
                positions.append({"mint": mint, "entry_time": (start+20_000_000)/1e9, "pnl_sol": 0.1})
                self.cache[mint] = {"twitter_handle": "fixture", "twitter_status_id": "1", "tweet_age_seconds": 0.5}
            batch, capture = directory / "batch.json", directory / "events.jsonl"
            batch.write_text(json.dumps({"run_id": f"r{index}", "actual_e4_fresh_sample": {"positions": positions}}))
            capture.write_text("\n".join(json.dumps(row) for row in reversed(events))+"\n")
            self.pairs.append((batch, capture))
        self.cache_path = self.root / "cache.json"
        self.cache_path.write_text(json.dumps(self.cache))

    def arguments(self, mode="search"):
        return argparse.Namespace(mode=mode, pair=[f"{a}:{b}" for a, b in self.pairs[:10]],
            latencies="0,1,2,5,10", starting_balance_sol=2.0, minimum_win_rate=0.65,
            minimum_profit_factor=1.25, metadata_cache=self.cache_path,
            metadata_concurrency=1, metadata_timeout=0.25, output=self.root/"report.json",
            model_output=self.root/"model.json", model_input=self.root/"model.json",
            predictions_output=self.root/"predictions.json")

    @staticmethod
    def rule():
        return social.Rule(1.0, 0, 0, 0, 0, 99, 2500.0, 6000.0, "create", 800)

    def test_event_order_and_loader_contract(self):
        grouped = runner.load_events(self.pairs[0][1])
        for rows in grouped.values():
            self.assertEqual([row["kind"] for row in rows], ["CREATE", "BUY", "SELL"])
            self.assertEqual([row["__sequence"] for row in rows], [0, 1, 2])
            self.assertLess(economics.event_order(rows[0]), economics.event_order(rows[1]))
        a = {"received_ns": 10, "slot": 1, "transaction_index": 0}
        b = {"received_ns": 10, "slot": 1, "transaction_index": 1}
        self.assertLess(economics.event_order(a), economics.event_order(b))

    def test_malformed_event_is_rejected(self):
        path = self.root / "invalid.jsonl"
        path.write_text("[]\n")
        with self.assertRaises(ValueError):
            runner.load_events(path)

    def test_latency_parser_rejects_invalid_scenarios(self):
        self.assertEqual(runner.parse_latencies("0,1,2,5,10"), [0,1,2,5,10])
        for invalid in ("", "nan", "inf", "-1", "0,nope"):
            with self.assertRaises(ValueError):
                runner.parse_latencies(invalid)

    def test_full_search_apply_canonical_cli_and_report(self):
        args = self.arguments()
        # Synthetic data is deliberately small; use a single real Rule to test
        # the entire integration, not to claim any market-performance result.
        with patch.object(social, "rules", return_value=iter([self.rule()])):
            self.assertEqual(social.search_mode(args), 0)
        historical = json.loads(args.output.read_text())
        self.assertEqual(historical["status"], "HISTORICAL_HOLDOUT_CONFIRMED")
        self.assertEqual(historical["starting_balance_sol"], 2.0)
        self.assertTrue(all(m["closed"] >= 3 for m in historical["holdout"].values()))
        frozen = hashlib.sha256(args.model_output.read_bytes()).hexdigest()
        args.mode = "apply"
        args.pair = [f"{a}:{b}" for a, b in self.pairs]
        self.assertEqual(social.apply_mode(args), 0)
        self.assertEqual(hashlib.sha256(args.model_output.read_bytes()).hexdigest(), frozen)
        predictions = json.loads(args.predictions_output.read_text())
        self.assertEqual(predictions["live_run_id"], "r10")
        self.assertEqual(len(predictions["predictions"]), 3)
        output = self.root / "economics.json"
        batch, events = self.pairs[10]
        result = subprocess.run([sys.executable, "-m", "scripts.e4_v12_true_latency_replay",
            "--pair", f"r10={batch}:{events}", "--predictions", str(args.predictions_output),
            "--latencies-ms", "0,1,2,5,10", "--output-shortfall-bps", "800",
            "--starting-balance-sol", "2", "--entry-fraction", "0.0185",
            "--pump-fee-bps", "125", "--max-concurrency", "2", "--output", str(output)],
            text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        summary = report.summarize(json.loads(output.read_text()), 800)
        self.assertEqual(summary["status"], "PAPER_FORWARD_GATE_PASSED")
        self.assertFalse(summary["real_money_trades"])
        self.assertTrue(all(row["trades"] == 3 for row in summary["latencies"].values()))
        self.assertIn("Ending SOL", report.markdown(summary))

    def test_no_survivor_is_not_a_pass(self):
        args = self.arguments()
        with patch.object(social, "rules", return_value=iter([])):
            self.assertEqual(social.search_mode(args), 2)
        self.assertEqual(json.loads(args.output.read_text())["status"], "NOT_CONCLUSIVE")
        self.assertFalse(args.model_output.exists())

    def test_bankroll_is_not_reset_between_runs(self):
        runs = golden.load_runs(self.pairs[:2])
        rows = social.build_rows(runs, self.cache)
        selected = social.select(rows, self.rule())
        metrics = golden.aggregate_economics(runs, selected, [0.0], starting_balance_sol=2.0, max_output_shortfall_bps=800)["0"]
        self.assertEqual(metrics["closed"], 6)
        self.assertEqual(metrics["starting_balance_sol"], 2.0)
        self.assertAlmostEqual(metrics["ending_balance_sol"]-2.0, metrics["net_pnl_sol"])
        positions = metrics["positions"]
        self.assertGreater(positions[3]["entry_budget_sol"], positions[0]["entry_budget_sol"])

    def test_unknown_run_fails_instead_of_silent_zero_trades(self):
        runs = golden.load_runs(self.pairs[:1])
        with self.assertRaises(ValueError):
            runner.aggregate_economics(runs, [{"mint": "fixture-0-0", "run_id": "wrong"}], [0.0], starting_balance_sol=2.0, max_output_shortfall_bps=800)

    def test_rejection_fees_are_in_net_pnl_and_profit_factor(self):
        result = {"positions": [{"pnl_sol": 0.1}, {"pnl_sol": -0.04}],
            "rejection_fees_sol": 0.02, "net_pnl_sol": 0.04,
            "ending_balance_sol": 2.04, "closed": 2, "wins": 1,
            "rejections": [{"reason": "floor"}]}
        with patch.object(economics, "portfolio", return_value=result):
            metrics = runner.aggregate_economics([], [], [0.0], starting_balance_sol=2.0, max_output_shortfall_bps=800)["0"]
        self.assertAlmostEqual(metrics["net_pnl_sol"], 0.04)
        self.assertAlmostEqual(metrics["profit_factor"], 0.1/0.06)

    @staticmethod
    def empty_matrix():
        return {"matrix": {"800": {str(value): {"starting_balance_sol": 2.0, "ending_balance_sol": 2.0,
            "net_pnl_sol": 0.0, "rejection_fees_sol": 0.0, "positions": [], "closed": 0, "wins": 0}
            for value in (0.0, 1.0, 2.0, 5.0, 10.0)}}}

    def test_zero_trades_has_no_winrate(self):
        summary = report.summarize(self.empty_matrix(), 800)
        self.assertEqual(summary["status"], "INSUFFICIENT_TRADES")
        self.assertTrue(all(row["win_rate"] is None for row in summary["latencies"].values()))

    def test_missing_matrix_cannot_pass(self):
        for bad in ({"matrix": {}}, {"matrix": {"800": {}}}):
            with self.assertRaises(ValueError):
                report.summarize(bad, 800)

    def test_wrong_bankroll_cannot_pass(self):
        bad = self.empty_matrix()
        bad["matrix"]["800"]["0.0"]["starting_balance_sol"] = 3.0
        with self.assertRaises(ValueError):
            report.summarize(bad, 800)

    def test_unreconciled_profit_cannot_pass(self):
        bad = self.empty_matrix()
        bad["matrix"]["800"]["0.0"]["net_pnl_sol"] = 1.0
        with self.assertRaises(ValueError):
            report.summarize(bad, 800)


if __name__ == "__main__":
    unittest.main()
