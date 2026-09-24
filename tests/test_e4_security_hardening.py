from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from memecoin_bot import e4_live
from memecoin_bot.e4_prearmed_readiness import validate_keypair_file


ROOT = Path(__file__).resolve().parents[1]


class E4SecurityHardeningTests(unittest.TestCase):
    def test_cli_requires_environment_and_command_interlock(self) -> None:
        with (
            patch.dict(os.environ, {"E4_LIVE": "false"}, clear=False),
            patch.object(sys, "argv", ["e4", "run", "--live"]),
        ):
            with self.assertRaisesRegex(SystemExit, "both E4_LIVE=true and --live"):
                e4_live.main()

    def test_raw_secret_environment_is_rejected_before_live_configuration(self) -> None:
        settings = e4_live.Settings(live=True)
        with patch.dict(
            os.environ,
            {"E4_SEED_PHRASE": "never-put-this-in-an-environment-variable"},
            clear=False,
        ):
            with self.assertRaisesRegex(ValueError, "raw secret material"):
                settings.validate()

    def test_keypair_permissions_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "e4.json"
            path.write_text("[1,2,3]", encoding="utf-8")
            os.chmod(path, 0o644)
            self.assertIn("KEYPAIR_PERMISSIONS_TOO_OPEN", validate_keypair_file(path))
            settings = e4_live.Settings(
                live=True,
                wallet="wallet",
                keypair_path=path,
                builder_command=("builder",),
                direct_rpc_route=True,
            )
            with self.assertRaises(PermissionError):
                settings.validate()

    def test_keypair_symlink_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "target.json"
            link = Path(directory) / "link.json"
            target.write_text("[1,2,3]", encoding="utf-8")
            os.chmod(target, 0o600)
            try:
                link.symlink_to(target)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks unavailable on this platform")
            settings = e4_live.Settings(
                live=True,
                wallet="wallet",
                keypair_path=link,
                builder_command=("builder",),
                direct_rpc_route=True,
            )
            with self.assertRaisesRegex(ValueError, "symlink"):
                settings.validate()

    def test_docker_context_excludes_secret_and_state_patterns(self) -> None:
        body = (ROOT / ".dockerignore").read_text(encoding="utf-8")
        for expected in (
            ".env.*",
            "*.key",
            "*keypair*.json",
            "*.db",
            "*.db-wal",
            "data",
            "var/e4",
        ):
            self.assertIn(expected, body)

    def test_production_compose_has_sandbox_controls(self) -> None:
        body = (ROOT / "docker-compose.e4-prod.yml").read_text(encoding="utf-8")
        for expected in (
            "read_only: true",
            "cap_drop:",
            "- ALL",
            "no-new-privileges:true",
            "noexec,nosuid",
            "pids_limit:",
            ":/run/secrets/e4-solana-keypair.json:ro",
        ):
            self.assertIn(expected, body)

    def test_remote_builder_fallback_is_opt_in(self) -> None:
        body = (ROOT / "tools" / "e4-builder" / "daemon-v2.mjs").read_text(encoding="utf-8")
        self.assertIn('E4_REMOTE_BUILDER_FALLBACK || "false"', body)
        env = (ROOT / ".env.e4.example").read_text(encoding="utf-8")
        self.assertIn("E4_REMOTE_BUILDER_FALLBACK=false", env)
        self.assertIn("E4_LIVE=false", env)


if __name__ == "__main__":
    unittest.main()
