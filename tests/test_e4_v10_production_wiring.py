from __future__ import annotations

import unittest
from pathlib import Path


class ProductionWiringTests(unittest.TestCase):
    def test_entrypoint_loads_v10_and_pipeline_supervisor(self) -> None:
        text = Path("src/memecoin_bot/e4_exec/__main__.py").read_text(encoding="utf-8")
        self.assertIn("e4_hardening_v10", text)
        self.assertIn("start_background_supervisor", text)
        self.assertIn("race-proxy-v3.mjs", text)

    def test_three_pipeline_runtime_files_are_present(self) -> None:
        required = (
            "src/memecoin_bot/e4_pipelines_v10.py",
            "src/memecoin_bot/e4_pipeline_runtime_v10.py",
            "scripts/e4_v10_discovery_worker.py",
            "scripts/e4_v10_discovery_loop.py",
            "scripts/e4_v10_social_stream.py",
            "tools/e4-builder/race-proxy-v3.mjs",
            "tools/e4-builder/fast-preload-v3.mjs",
        )
        for path in required:
            self.assertTrue(Path(path).is_file(), path)


if __name__ == "__main__":
    unittest.main()
