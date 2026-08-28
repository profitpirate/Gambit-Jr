from __future__ import annotations

import json
from pathlib import Path

from scripts.live_discord_acceptance import REQUIRED_COMMANDS, REQUIRED_COMPONENTS, verify


def write_events(path: Path, events: list[dict]) -> None:
    path.write_text(
        "\n".join(f"bot-1 | {json.dumps(event)}" for event in events), encoding="utf-8"
    )


def passing_events() -> list[dict]:
    rows = []
    for command in REQUIRED_COMMANDS:
        rows.extend(
            [
                {
                    "message": "discord_response_success",
                    "command_name": command,
                    "result": "success",
                },
                {
                    "message": "command_completed",
                    "command_name": command,
                    "result": "success",
                },
            ]
        )
    rows.extend(
        {
            "message": "component_completed",
            "custom_id": component,
            "result": "success",
        }
        for component in REQUIRED_COMPONENTS
    )
    return rows


def test_live_acceptance_requires_all_commands_responses_and_components(tmp_path):
    path = tmp_path / "logs.txt"
    events = passing_events()
    events.pop()
    write_events(path, events)
    report = verify(path)
    assert report["result"] == "FAIL"
    assert report["missing_components"]


def test_live_acceptance_rejects_transport_failure_even_after_success(tmp_path):
    path = tmp_path / "logs.txt"
    events = passing_events()
    events.append({"message": "discord_response_failed", "command_name": "scan"})
    write_events(path, events)
    report = verify(path)
    assert report["result"] == "FAIL"
    assert report["forbidden_events"]


def test_live_acceptance_passes_complete_clean_session(tmp_path):
    path = tmp_path / "logs.txt"
    write_events(path, passing_events())
    report = verify(path)
    assert report["result"] == "PASS"
