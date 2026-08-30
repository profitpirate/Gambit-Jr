from __future__ import annotations

import copy
import functools
import importlib
import re
from typing import Any

MADE_BY_JAY = "Made by Jay"

_LEGACY_INTERNAL_EVENTS = frozenset(
    {
        "GENESIS_RADAR",
        "EARLY_RADAR",
        "HOT_RADAR",
        "PRIORITY_RADAR",
        "RADAR_MILESTONE",
        "RADAR_RISK",
    }
)

_IDENTIFIER_LABELS = {
    "VERIFIED_DIRECT_LAUNCH_EVENT": "Fresh launch confirmed on-chain",
    "ULTRA_EARLY_ENTRY_WINDOW": "Entry is still ultra-early",
    "liquidity_usd": "liquidity",
    "bonding_curve_progress_percent": "bonding-curve progress",
    "buyer_count": "buyer count",
    "market_cap_usd": "market cap",
    "volume_5m_usd": "5-minute volume",
    "evidence_coverage": "evidence coverage",
    "runner_score": "runner outlook",
    "failure_score": "risk outlook",
    "launch_event_verified": "launch confirmed",
    "creator_address": "creator address",
    "age_seconds": "token age",
}

_PHRASE_REPLACEMENTS = (
    (r"`?/radar`?", "`/candidates`"),
    (r"GAMBIT JR\s*[—-]\s*GENESIS RADAR", "GAMBIT JR — EARLY OBSERVATION"),
    (r"GAMBIT JR\s*[—-]\s*STANDARD RADAR", "GAMBIT JR — DEVELOPING SETUP"),
    (r"GAMBIT JR\s*[—-]\s*HOT RADAR", "GAMBIT JR — HOT SETUP"),
    (r"GAMBIT JR\s*[—-]\s*PRIORITY RADAR", "GAMBIT JR — PRIORITY SETUP"),
    (r"COMMAND CENTER\s*•\s*RADAR", "COMMAND CENTER • CALLS"),
    (r"ACTIVE RADAR", "ACTIVE CALLS"),
    (r"No active Radar evidence", "No active calls yet"),
    (r"Radar and candidate state", "call and developing-setup state"),
    (r"Radar calls", "calls"),
    (r"Radar or signal", "call"),
    (r"Radar is not a buy instruction", "An observation is not a buy instruction"),
    (r"GENESIS\s*→\s*QUALIFIED", "OBSERVE → THESIS → VERIFY → CALL"),
    (r"SIGNAL UPGRADE", "CALL UPGRADE"),
    (r"SIGNAL FAILED", "CALL FAILED"),
    (r"READ-ONLY SHADOW SIGNAL", "READ-ONLY SHADOW CALL"),
    (r"Signal timestamp", "Call timestamp"),
    (r"Signal MC", "Call MC"),
    (r"Active signals", "Active calls"),
    (r"Signals:", "Calls:"),
    (r"Radar:", "Developing setups:"),
    (r"\bRADAR\b", "CALLS"),
    (r"\bGENESIS\b", "EARLY"),
    (r"\bQUALIFIED SIGNAL\b", "QUALIFIED CALL"),
    (r"\bSIGNALS\b", "CALLS"),
    (r"\bSIGNAL\b", "CALL"),
)

_SNAKE_CASE = re.compile(r"\b[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+\b")
_ACRONYMS = {
    "api": "API",
    "ca": "CA",
    "mc": "MC",
    "rpc": "RPC",
    "sol": "SOL",
    "usd": "USD",
    "wss": "WSS",
}


def _humanize_identifier(token: str) -> str:
    mapped = _IDENTIFIER_LABELS.get(token)
    if mapped is None:
        mapped = _IDENTIFIER_LABELS.get(token.upper())
    if mapped is not None:
        return mapped
    return " ".join(_ACRONYMS.get(word, word) for word in token.lower().split("_"))


def humanize_text(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value
    for identifier, label in sorted(
        _IDENTIFIER_LABELS.items(), key=lambda item: len(item[0]), reverse=True
    ):
        text = re.sub(rf"\b{re.escape(identifier)}\b", label, text, flags=re.IGNORECASE)
    text = _SNAKE_CASE.sub(lambda match: _humanize_identifier(match.group(0)), text)
    for pattern, replacement in _PHRASE_REPLACEMENTS:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    return text


def _footer_text(value: Any) -> str:
    text = humanize_text(str(value or "GAMBIT JR"))
    if MADE_BY_JAY.lower() in text.lower():
        return text[:2048]
    if text.upper().startswith("GAMBIT JR"):
        remainder = text[len("GAMBIT JR") :].lstrip(" •")
        text = f"GAMBIT JR • {MADE_BY_JAY}"
        if remainder:
            text += f" • {remainder}"
    else:
        text = f"{text} • {MADE_BY_JAY}"
    return text[:2048]


def _transform_embed(embed: dict[str, Any]) -> None:
    if embed.get("title") is not None:
        embed["title"] = humanize_text(str(embed["title"]))[:256]
    if embed.get("description") is not None:
        embed["description"] = humanize_text(str(embed["description"]))[:4096]
    fields = []
    for raw in embed.get("fields") or []:
        field = dict(raw)
        field["name"] = humanize_text(str(field.get("name") or "Details"))[:256]
        field["value"] = humanize_text(str(field.get("value") or "UNKNOWN"))[:1024]
        fields.append(field)
    embed["fields"] = fields[:25]
    footer = dict(embed.get("footer") or {})
    footer["text"] = _footer_text(footer.get("text"))
    embed["footer"] = footer


def apply_product_presentation(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("_gambit_internal_event") is True:
        return payload
    if payload.get("content") is not None:
        payload["content"] = humanize_text(str(payload["content"]))[:2000]
    embed = payload.get("embed")
    if isinstance(embed, dict):
        _transform_embed(embed)
    for item in payload.get("embeds") or []:
        if isinstance(item, dict):
            _transform_embed(item)
    return payload


def _clean_delivered_signal(payload: dict[str, Any]) -> None:
    embeds = payload.get("embeds") or []
    if not embeds:
        return
    embed = embeds[0]
    original_title = str(embed.get("title") or "CALL")
    title_parts = [part.strip() for part in original_title.split("•", 1)]
    category = humanize_text(title_parts[0]).upper()
    identity = title_parts[1] if len(title_parts) > 1 else ""
    embed["title"] = f"GAMBIT JR — {category} CALL" + (f" • {identity}" if identity else "")

    fields = []
    for raw in embed.get("fields") or []:
        name = str(raw.get("name") or "")
        if name in {"Runner potential", "Failure risk", "Historical context", "Contract address"}:
            continue
        field = dict(raw)
        if name == "Tier":
            field["name"] = "Call category"
        elif name == "Confidence / evidence coverage":
            field["name"] = "Evidence confidence"
        elif name == "Why now":
            field["name"] = "Why it matters now"
        fields.append(field)
    embed["fields"] = fields

    description_lines = []
    for line in str(embed.get("description") or "").splitlines():
        stripped = line.strip()
        if stripped.startswith(("Score:", "V1.5 Tier:")):
            continue
        description_lines.append(line)
    embed["description"] = "\n".join(description_lines)


def prepare_outbound_message(payload: dict[str, Any]) -> dict[str, Any]:
    message = copy.deepcopy(payload)
    embeds = message.get("embeds") or []
    if embeds and any(field.get("name") == "Tier" for field in embeds[0].get("fields") or []):
        _clean_delivered_signal(message)
    return apply_product_presentation(message)


def install_discord_product_policy() -> None:
    cards = importlib.import_module("memecoin_bot.discord.cards")
    if getattr(cards, "_gambit_product_policy_installed", False):
        return

    command_center = importlib.import_module("memecoin_bot.discord.command_center")
    bot_runtime = importlib.import_module("memecoin_bot.discord.bot_runtime")
    formatting = importlib.import_module("memecoin_bot.signals.formatting")
    signals_package = importlib.import_module("memecoin_bot.signals")

    original_card = cards.card

    @functools.wraps(original_card)
    def branded_card(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return apply_product_presentation(original_card(*args, **kwargs))

    cards.card = branded_card
    command_center.card = branded_card

    original_status_card = cards.status_card

    @functools.wraps(original_status_card)
    def reliability_status_card(stats: dict[str, Any]) -> dict[str, Any]:
        # The base status card already consumes runtime/pipeline diagnostics.
        # Do not append internal audit sections back into the user-facing UI.
        return apply_product_presentation(original_status_card(stats))

    cards.status_card = reliability_status_card
    bot_runtime.status_card = reliability_status_card
    command_center.status_card = reliability_status_card

    original_format = formatting.format_discord_event

    @functools.wraps(original_format)
    def calls_first_format(event_type: str, evidence: dict[str, Any]) -> dict[str, Any]:
        payload = original_format(event_type, evidence)
        if event_type in _LEGACY_INTERNAL_EVENTS:
            payload["_gambit_internal_event"] = True
            payload["event_type"] = event_type
        return payload

    formatting.format_discord_event = calls_first_format
    signals_package.format_discord_event = calls_first_format

    original_menu_init = command_center.MenuView.__init__

    @functools.wraps(original_menu_init)
    def safe_menu_init(self: Any, *args: Any, **kwargs: Any) -> None:
        original_menu_init(self, *args, **kwargs)
        for child in self.children:
            if getattr(child, "custom_id", None) == "gambit:menu:refresh":
                child.emoji = None

    command_center.MenuView.__init__ = safe_menu_init

    original_select_init = command_center.MenuSelect.__init__

    @functools.wraps(original_select_init)
    def calls_select_init(self: Any, *args: Any, **kwargs: Any) -> None:
        original_select_init(self, *args, **kwargs)
        for option in self.options:
            if option.value == "radar":
                option.label = "Calls"
                option.description = "Calls, developing setups and measured outcomes"

    command_center.MenuSelect.__init__ = calls_select_init
    command_center.PAGE_TITLES["radar"] = "COMMAND CENTER • CALLS"

    # Acknowledge every slash command before any database/provider work. The
    # InteractionResponder then edits the original response, preventing Discord's
    # three-second timeout from becoming "application failed to respond".
    bot_runtime.DEFERRED_COMMANDS = bot_runtime.EXPECTED_COMMAND_NAMES

    cards._gambit_product_policy_installed = True
