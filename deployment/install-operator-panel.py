"""Install the independent owner-only Discord gateway after a verified bot release."""

import argparse
import importlib.util
import json
import os
import subprocess
import time
from pathlib import Path

STAGE = Path("/opt/gambit-v12-staging")
OWNER = "1533142248596836619"


def call(*args):
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError("SETUP_COMMAND_FAILED:" + Path(args[0]).name)


def main(*, defer_start=False):
    os.umask(0o077)
    installed = json.loads((STAGE / "prearmed-install.json").read_text())
    if installed.get("offline_tests_passed") is not True:
        raise RuntimeError("VERIFIED_RELEASE_REQUIRED")
    release = Path(installed["release"])
    spec = importlib.util.spec_from_file_location("control", STAGE / "prearmed-control.py")
    control = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(control)
    values = control.env_read(STAGE / "prearmed.env")
    values["V12_DISCORD_USER_ID"] = OWNER
    control.env_write(STAGE / "prearmed.env", values)
    call("python3", str(release / "deployment/install-monitor-collector.py"))
    venv = STAGE / "operator-venv"
    if not (venv / "bin/python").exists():
        call("python3", "-m", "venv", str(venv))
    call(
        str(venv / "bin/python"),
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--only-binary=:all:",
        "pip==26.2.0",
    )
    call(
        str(venv / "bin/python"),
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--only-binary=:all:",
        "discord.py==2.7.1",
        "aiohttp==3.14.3",
        "solders==0.27.1",
    )
    directory = STAGE / "operator-data"
    directory.mkdir(exist_ok=True, mode=0o700)
    service = directory / "gambit-operator.service"
    service.write_text(
        "[Unit]\nDescription=Gambit owner Discord controls and health monitor\nAfter=network-online.target docker.service\n"
        "Wants=network-online.target\nStartLimitIntervalSec=120\nStartLimitBurst=10\n\n[Service]\nType=simple\n"
        "User=ubuntu\nGroup=ubuntu\nSupplementaryGroups=docker\nUMask=0077\n"
        f"WorkingDirectory={STAGE}\nEnvironmentFile={STAGE}/prearmed.env\nEnvironment=PYTHONPATH={release}/src\n"
        "Environment=PYTHONDONTWRITEBYTECODE=1\nEnvironment=PYTHONUNBUFFERED=1\n"
        f"ExecStart={venv}/bin/python -m memecoin_bot.v12_prearmed_discord\n"
        "Restart=always\nRestartSec=5\nTimeoutStopSec=20\n\n[Install]\nWantedBy=multi-user.target\n"
    )
    call(
        "sudo", "install", "-m", "0644", str(service), "/etc/systemd/system/gambit-operator.service"
    )
    call("sudo", "systemctl", "daemon-reload")
    call("sudo", "systemctl", "enable", "gambit-operator")
    if defer_start:
        call(
            "sudo",
            "systemd-run",
            "--collect",
            "--unit=gambit-operator-refresh-" + str(time.time_ns()),
            "--on-active=10s",
            "/bin/systemctl",
            "restart",
            "gambit-operator",
        )
    else:
        call("sudo", "systemctl", "restart", "gambit-operator")
    print(json.dumps({"installed": True, "owner_id": OWNER, "service": "gambit-operator"}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--defer-start", action="store_true")
    main(defer_start=parser.parse_args().defer_start)
