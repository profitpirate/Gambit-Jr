"""Keep an explicitly installed monitor patch across trading release updates."""

import hashlib
import json
import re
import subprocess
from pathlib import Path

STAGE = Path("/opt/gambit-v12-staging")


def select_collector(stage, bundled):
    record = stage / "operator-data/monitor-install.json"
    if not record.exists():
        return bundled
    value = json.loads(record.read_text())
    digest = value["sha256"]
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("MONITOR_OVERRIDE_INVALID")
    source = stage / "monitor-releases" / digest / "collector.py"
    if source.resolve() != source or source.is_symlink():
        raise ValueError("MONITOR_OVERRIDE_PATH_INVALID")
    if hashlib.sha256(source.read_bytes()).hexdigest() != digest:
        raise ValueError("MONITOR_OVERRIDE_CHECKSUM_FAILED")
    return source


def install(stage=STAGE):
    source = select_collector(stage, Path(__file__).with_name("gambit-monitor-collector.py"))
    targets = [(stage / "gambit-monitor-collector.py", "ubuntu", "0700")]
    if (stage / "monitor").is_dir():
        targets.append((stage / "monitor/collector.py", "root", "0755"))
    for target, owner, mode in targets:
        temporary = target.with_suffix(".installing")
        subprocess.run(
            ["sudo", "install", "-o", owner, "-g", owner, "-m", mode, str(source), str(temporary)],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["sudo", "mv", "-T", str(temporary), str(target)], check=True, capture_output=True
        )


if __name__ == "__main__":
    install()
