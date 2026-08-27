from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

import discord
from discord.http import handle_message_parameters

EMBED_TOTAL_LIMIT = 6000
MAX_COMPONENT_ROWS = 5


class DiscordPayloadValidationError(ValueError):
    """Raised before transport when a Discord payload violates a documented limit."""


def _text(value: Any, name: str, maximum: int, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise DiscordPayloadValidationError(f"{name} must be a string")
    if not allow_empty and not value.strip():
        raise DiscordPayloadValidationError(f"{name} must not be empty")
    if len(value) > maximum:
        raise DiscordPayloadValidationError(f"{name} exceeds {maximum} characters")
    return value


def validate_card(payload: dict[str, Any]) -> discord.Embed:
    if not isinstance(payload, dict) or not isinstance(payload.get("embed"), dict):
        raise DiscordPayloadValidationError("card must contain one embed object")
    data = dict(payload["embed"])
    total = 0
    if "title" in data:
        total += len(_text(data["title"], "embed.title", 256))
    if "description" in data:
        description = _text(
            data["description"], "embed.description", 4096, allow_empty=True
        )
        if description:
            total += len(description)
        else:
            data.pop("description")
    color = data.get("color")
    if color is not None and (not isinstance(color, int) or not 0 <= color <= 0xFFFFFF):
        raise DiscordPayloadValidationError("embed.color must be an RGB integer")
    timestamp = data.get("timestamp")
    if timestamp is not None:
        if not isinstance(timestamp, str):
            raise DiscordPayloadValidationError("embed.timestamp must be ISO8601 text")
        try:
            datetime.fromisoformat(timestamp)
        except ValueError as error:
            raise DiscordPayloadValidationError("embed.timestamp must be valid ISO8601") from error
    fields = data.get("fields", [])
    if not isinstance(fields, list) or len(fields) > 25:
        raise DiscordPayloadValidationError("embed.fields must contain at most 25 fields")
    for index, field in enumerate(fields):
        if not isinstance(field, dict):
            raise DiscordPayloadValidationError(f"embed.fields[{index}] must be an object")
        total += len(_text(field.get("name"), f"embed.fields[{index}].name", 256))
        total += len(_text(field.get("value"), f"embed.fields[{index}].value", 1024))
        if "inline" in field and not isinstance(field["inline"], bool):
            raise DiscordPayloadValidationError(f"embed.fields[{index}].inline must be boolean")
    footer = data.get("footer")
    if footer is not None:
        if not isinstance(footer, dict):
            raise DiscordPayloadValidationError("embed.footer must be an object")
        total += len(_text(footer.get("text"), "embed.footer.text", 2048))
    author = data.get("author")
    if author is not None:
        if not isinstance(author, dict):
            raise DiscordPayloadValidationError("embed.author must be an object")
        total += len(_text(author.get("name"), "embed.author.name", 256))
    if total > EMBED_TOTAL_LIMIT:
        raise DiscordPayloadValidationError(
            f"embed total character count {total} exceeds {EMBED_TOTAL_LIMIT}"
        )
    links = payload.get("links", [])
    if not isinstance(links, list) or len(links) > 5:
        raise DiscordPayloadValidationError("card links must contain at most 5 buttons")
    for index, link in enumerate(links):
        if not isinstance(link, (list, tuple)) or len(link) != 2:
            raise DiscordPayloadValidationError(f"links[{index}] must be a label/url pair")
        _text(link[0], f"links[{index}].label", 80)
        parsed = urlparse(_text(link[1], f"links[{index}].url", 512))
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise DiscordPayloadValidationError(f"links[{index}].url must be HTTP(S)")
    embed = discord.Embed.from_dict(data)
    serialized = embed.to_dict()
    with handle_message_parameters(embed=embed) as parameters:
        json.dumps(parameters.payload)
    if len(embed) != total:
        raise DiscordPayloadValidationError(
            f"embed serialization character count changed from {total} to {len(embed)}"
        )
    if serialized.get("type") not in {None, "rich"}:
        raise DiscordPayloadValidationError("outbound embed type must be rich")
    return embed


def validate_view(view: discord.ui.View | None) -> list[dict[str, Any]]:
    if view is None:
        return []
    rows = view.to_components()
    if len(rows) > MAX_COMPONENT_ROWS:
        raise DiscordPayloadValidationError("view exceeds 5 action rows")
    custom_ids: set[str] = set()
    for row_index, row in enumerate(rows):
        components = row.get("components")
        if not isinstance(components, list) or not components:
            raise DiscordPayloadValidationError(f"component row {row_index} is empty")
        component_types = {component.get("type") for component in components}
        if component_types == {2} and len(components) > 5:
            raise DiscordPayloadValidationError(f"button row {row_index} exceeds 5 buttons")
        if component_types != {2} and len(components) != 1:
            raise DiscordPayloadValidationError(
                f"select row {row_index} must contain exactly one component"
            )
        for component in components:
            custom_id = component.get("custom_id")
            if custom_id is not None:
                _text(custom_id, "component.custom_id", 100)
                if custom_id in custom_ids:
                    raise DiscordPayloadValidationError(
                        f"duplicate component custom_id: {custom_id}"
                    )
                custom_ids.add(custom_id)
            label = component.get("label")
            if label is not None:
                _text(label, "component.label", 80)
            placeholder = component.get("placeholder")
            if placeholder is not None:
                _text(placeholder, "component.placeholder", 150)
            options = component.get("options")
            if options is not None:
                if not 1 <= len(options) <= 25:
                    raise DiscordPayloadValidationError(
                        "select must contain between 1 and 25 options"
                    )
                for option_index, option in enumerate(options):
                    _text(option.get("label"), f"select.options[{option_index}].label", 100)
                    _text(option.get("value"), f"select.options[{option_index}].value", 100)
                    description = option.get("description")
                    if description is not None:
                        _text(
                            description,
                            f"select.options[{option_index}].description",
                            100,
                        )
    json.dumps(rows)
    return rows


def component_count(view: discord.ui.View | None) -> int:
    return sum(len(row.get("components", [])) for row in validate_view(view))


def validate_message(
    *, content: str | None = None, card_payload: dict[str, Any] | None = None, view: discord.ui.View | None = None
) -> discord.Embed | None:
    if content is None and card_payload is None and view is None:
        raise DiscordPayloadValidationError("Discord message cannot be empty")
    if content is not None:
        _text(content, "message.content", 2000)
    embed = validate_card(card_payload) if card_payload is not None else None
    validate_view(view)
    return embed


def validate_webhook_payload(message: dict[str, Any]) -> None:
    """Validate the automatic-alert JSON path that bypasses discord.py objects."""
    if not isinstance(message, dict):
        raise DiscordPayloadValidationError("webhook payload must be an object")
    content = message.get("content")
    if content is not None:
        _text(content, "message.content", 2000)
    embeds = message.get("embeds") or []
    if not isinstance(embeds, list) or len(embeds) > 10:
        raise DiscordPayloadValidationError("message.embeds must contain at most 10 embeds")
    total = 0
    for index, embed_data in enumerate(embeds):
        if not isinstance(embed_data, dict):
            raise DiscordPayloadValidationError(f"message.embeds[{index}] must be an object")
        total += len(validate_card({"embed": embed_data}))
    if total > EMBED_TOTAL_LIMIT:
        raise DiscordPayloadValidationError(
            f"message embed total character count {total} exceeds {EMBED_TOTAL_LIMIT}"
        )
    rows = message.get("components") or []
    if not isinstance(rows, list) or len(rows) > MAX_COMPONENT_ROWS:
        raise DiscordPayloadValidationError("message.components exceeds 5 action rows")
    custom_ids: set[str] = set()
    for row_index, row in enumerate(rows):
        if not isinstance(row, dict) or row.get("type") != 1:
            raise DiscordPayloadValidationError(f"message.components[{row_index}] is not a row")
        children = row.get("components") or []
        if not 1 <= len(children) <= 5:
            raise DiscordPayloadValidationError(
                f"message.components[{row_index}] must contain 1-5 components"
            )
        for component_index, component in enumerate(children):
            if not isinstance(component, dict):
                raise DiscordPayloadValidationError(
                    f"message.components[{row_index}][{component_index}] must be an object"
                )
            custom_id = component.get("custom_id")
            if custom_id is not None:
                _text(custom_id, "component.custom_id", 100)
                if custom_id in custom_ids:
                    raise DiscordPayloadValidationError(
                        f"duplicate component custom_id: {custom_id}"
                    )
                custom_ids.add(custom_id)
            label = component.get("label")
            if label is not None:
                _text(label, "component.label", 80)
            url = component.get("url")
            if url is not None:
                parsed = urlparse(_text(url, "component.url", 512))
                if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                    raise DiscordPayloadValidationError("component.url must be HTTP(S)")
    if content is None and not embeds and not rows:
        raise DiscordPayloadValidationError("Discord webhook message cannot be empty")
    json.dumps(message)
