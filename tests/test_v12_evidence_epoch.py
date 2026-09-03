from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import e4_v12_evidence_epoch as epoch_module


class V12EvidenceEpochTests(unittest.TestCase):
    def test_strategy_change_archives_old_evidence_and_starts_clean_epoch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = root / "active.json"
            markdown = root / "active.md"
            epoch = root / "epoch.txt"
            archive = root / "archive"
            evidence.write_text(
                json.dumps(
                    {
                        "version": "e4-v12-forward-evidence-v1",
                        "strategy_fingerprint": "old-fingerprint",
                        "batches": [{"batch_id": "old"}],
                        "gambit_positions": {"old": {"pnl_sol": -1}},
                        "same_window_e4_positions": {},
                    }
                ),
                encoding="utf-8",
            )
            markdown.write_text("old report\n", encoding="utf-8")
            epoch.write_text("v12-new-role-model-epoch\n", encoding="utf-8")

            with patch.object(epoch_module, "fingerprint", return_value="new-fingerprint"):
                result = epoch_module.prepare_epoch(evidence, markdown, epoch, archive)

            self.assertEqual(result["action"], "ROLLED_OVER")
            active = json.loads(evidence.read_text(encoding="utf-8"))
            self.assertEqual(active["evidence_epoch"], "v12-new-role-model-epoch")
            self.assertEqual(active["strategy_fingerprint"], "new-fingerprint")
            self.assertEqual(active["batches"], [])
            self.assertEqual(active["gambit_positions"], {})
            archived = list(archive.glob("*.json"))
            self.assertEqual(len(archived), 1)
            old = json.loads(archived[0].read_text(encoding="utf-8"))
            self.assertEqual(old["batches"][0]["batch_id"], "old")

    def test_strategy_change_inside_same_epoch_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = root / "active.json"
            markdown = root / "active.md"
            epoch = root / "epoch.txt"
            archive = root / "archive"
            evidence.write_text(
                json.dumps(
                    {
                        "version": "e4-v12-forward-evidence-v1",
                        "evidence_epoch": "v12-current",
                        "strategy_fingerprint": "old-fingerprint",
                        "batches": [],
                        "gambit_positions": {},
                        "same_window_e4_positions": {},
                    }
                ),
                encoding="utf-8",
            )
            epoch.write_text("v12-current\n", encoding="utf-8")
            with patch.object(epoch_module, "fingerprint", return_value="changed-fingerprint"):
                with self.assertRaises(RuntimeError):
                    epoch_module.prepare_epoch(evidence, markdown, epoch, archive)


if __name__ == "__main__":
    unittest.main()
