#!/usr/bin/env python3
"""Fail-closed verification of one live Discord acceptance session."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REQUIRED_COMMANDS = {"menu", "help", "status", "performance", "radar", "watchlist", "scan"}
REQUIRED_COMPONENTS = {"gambit:scan:refresh", "gambit:scan:watch"}
FORBIDDEN_MESSAGES = {
    "command_error",
    "component_failed",
    "component_callback_failed",
    "discord_payload_rejected",
    "discord_response_failed",
}
FORBIDDEN_TEXT = {
    "application did not respond",
    "gambit jr couldn't complete that request",
    "discord couldn't deliver gambit jr's response",
}


def _events(path: Path) -> tuple[list[dict[str, Any]], str]:
    raw = path.read_text(encoding="utf-8", errors="replace")
    rows = []
    for line in raw.splitlines():
        start = line.find("{")
        if start < 0:
            continue
        try:
            value = json.loads(line[start:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows, raw.lower()


def verify(path: Path) -> dict[str, Any]:
    events, raw = _events(path)
    successful_commands = {
        str(row.get("command_name"))
        for row in events
        if row.get("message") == "command_completed" and row.get("result") == "success"
    }
    successful_responses = {
        str(row.get("command_name"))
        for row in events
        if row.get("message") == "discord_response_success" and row.get("result") == "success"
    }
    successful_components = {
        str(row.get("custom_id"))
        for row in events
        if row.get("message") == "component_completed" and row.get("result") == "success"
    }
    forbidden_events = [
        {"message": row.get("message"), "command": row.get("command_name")}
        for row in events
        if row.get("message") in FORBIDDEN_MESSAGES
    ]
    forbidden_text = sorted(value for value in FORBIDDEN_TEXT if value in raw)
    missing_commands = sorted(REQUIRED_COMMANDS - successful_commands)
    missing_responses = sorted(REQUIRED_COMMANDS - successful_responses)
    missing_components = sorted(REQUIRED_COMPONENTS - successful_components)
    passed = not any(
        (missing_commands, missing_responses, missing_components, forbidden_events, forbidden_text)
    )
    return {
        "result": "PASS" if passed else "FAIL",
        "required_commands": sorted(REQUIRED_COMMANDS),
        "successful_commands": sorted(successful_commands & REQUIRED_COMMANDS),
        "successful_primary_responses": sorted(successful_responses & REQUIRED_COMMANDS),
        "successful_components": sorted(successful_components & REQUIRED_COMPONENTS),
        "missing_commands": missing_commands,
        "missing_primary_responses": missing_responses,
        "missing_components": missing_components,
        "forbidden_events": forbidden_events,
        "forbidden_text": forbidden_text,
        "parsed_event_count": len(events),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log_file")
    result = verify(Path(parser.parse_args().log_file))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["result"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
