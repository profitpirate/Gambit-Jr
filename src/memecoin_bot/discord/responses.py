from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any
from urllib.parse import urlsplit

import discord
from discord.webhook.async_ import async_context

from memecoin_bot.discord.validation import (
    DiscordPayloadValidationError,
    component_count,
    validate_message,
)
from memecoin_bot.observability.logging import event

SAFE_INTERNAL_ERROR = "Gambit Jr couldn't complete that request. The error has been logged."
SAFE_DELIVERY_ERROR = "Discord couldn't deliver Gambit Jr's response. The error has been logged."


class ResponseVisibility(Enum):
    PUBLIC = "public"
    PRIVATE = "private"

    @property
    def ephemeral(self) -> bool:
        return self is ResponseVisibility.PRIVATE


class DomainError(RuntimeError):
    """A truthful user-facing domain result, not an internal failure."""


class ProviderError(RuntimeError):
    """An upstream evidence provider could not complete the request."""


_TOKEN_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*(?:bot|bearer)?\s*)\S+"),
    re.compile(r"(?i)(discord[_-]?token\s*[:=]\s*)\S+"),
    re.compile(r"(?i)(\b(?:interaction[_-]?|webhook[_-]?)?token\s*[:=]\s*)\S+"),
    re.compile(r"([A-Za-z0-9_-]{20,})\.([A-Za-z0-9_-]{6})\.([A-Za-z0-9_-]{20,})"),
    re.compile(r"(?i)(/webhooks/\d+/)[^/?\s]+"),
    re.compile(r"(?i)(/interactions/\d+/)[^/?\s]+"),
)


def sanitize_discord_text(value: Any, limit: int = 500) -> str:
    text = str(value or "")
    for pattern in _TOKEN_PATTERNS:
        text = pattern.sub(lambda match: f"{match.group(1)}[REDACTED]", text)
    return text[:limit]


def _response_route(error: discord.HTTPException) -> tuple[str | None, str | None]:
    response = getattr(error, "response", None)
    method = getattr(response, "method", None)
    url = getattr(response, "url", None)
    if url is None:
        request_info = getattr(response, "request_info", None)
        method = method or getattr(request_info, "method", None)
        url = getattr(request_info, "url", None)
    if url is None:
        return method, None
    parsed = urlsplit(str(url))
    route = sanitize_discord_text(parsed.path, 300)
    return method, route


def log_discord_http_failure(
    logger: logging.Logger,
    error: discord.HTTPException,
    *,
    command_name: str,
    interaction: discord.Interaction,
    response_state: str,
    defer_occurred: bool,
    ephemeral: bool,
    payload_kind: str,
    has_content: bool,
    embed_count: int,
    components: int,
    started: float,
) -> None:
    method, route = _response_route(error)
    event(
        logger,
        logging.ERROR,
        "discord_response_failed",
        command_name=command_name,
        error_type=type(error).__name__,
        interaction_type=getattr(getattr(interaction, "type", None), "name", "UNKNOWN"),
        http_status=error.status,
        discord_code=error.code,
        discord_message=sanitize_discord_text(error.text),
        response_method=method,
        response_route=route,
        response_state=response_state,
        response_acknowledged=interaction.response.is_done(),
        defer_occurred=defer_occurred,
        ephemeral=ephemeral,
        payload_kind=payload_kind,
        has_content=has_content,
        embed_count=embed_count,
        component_count=components,
        has_view=components > 0,
        duration_ms=round((time.monotonic() - started) * 1000, 1),
        result="failure",
    )


async def edit_deferred_original_exact(
    interaction: discord.Interaction,
    *,
    content: str | None = None,
    embed: discord.Embed | None = None,
    view: discord.ui.View | None = None,
) -> None:
    """Edit an acknowledged interaction using the documented PATCH field set."""
    if not hasattr(interaction, "_state"):
        await interaction.edit_original_response(
            **{
                key: value
                for key, value in {"content": content, "embed": embed, "view": view}.items()
                if value is not None
            }
        )
        return
    payload: dict[str, Any] = {"allowed_mentions": {"parse": []}}
    if content is not None:
        payload["content"] = content
    if embed is not None:
        payload["embeds"] = [embed.to_dict()]
    if view is not None:
        payload["components"] = view.to_components()

    adapter = async_context.get()
    http = interaction._state.http
    data = await adapter.edit_original_interaction_response(
        interaction.application_id,
        interaction.token,
        session=interaction._session,
        proxy=http.proxy,
        proxy_auth=http.proxy_auth,
        payload=payload,
    )
    if view is not None and not view.is_finished() and view.is_dispatchable():
        interaction._state.store_view(view, int(data["id"]))


@dataclass
class InteractionResponder:
    interaction: discord.Interaction
    command_name: str
    visibility: ResponseVisibility
    logger: logging.Logger
    deferred: bool = False
    primary_completed: bool = False

    async def defer(self) -> None:
        if self.interaction.response.is_done():
            raise discord.InteractionResponded(self.interaction)
        started = time.monotonic()
        try:
            await self.interaction.response.defer(ephemeral=self.visibility.ephemeral)
        except discord.HTTPException as error:
            log_discord_http_failure(
                self.logger,
                error,
                command_name=self.command_name,
                interaction=self.interaction,
                response_state="unacknowledged",
                defer_occurred=False,
                ephemeral=self.visibility.ephemeral,
                payload_kind="defer",
                has_content=False,
                embed_count=0,
                components=0,
                started=started,
            )
            raise
        self.deferred = True

    def _view_with_links(
        self, payload: dict[str, Any], view: discord.ui.View | None
    ) -> discord.ui.View | None:
        links = payload.get("links") or []
        if links and view is None:
            view = discord.ui.View(timeout=900 if self.visibility.ephemeral else 300)
        for label, url in links:
            view.add_item(discord.ui.Button(label=label, url=url))
        return view

    async def primary_card(
        self, payload: dict[str, Any], view: discord.ui.View | None = None
    ) -> discord.InteractionMessage | None:
        started = time.monotonic()
        try:
            view = self._view_with_links(payload, view)
            embed = validate_message(card_payload=payload, view=view)
        except DiscordPayloadValidationError as error:
            self._log_validation_failure(
                error,
                payload_kind="embed_view" if view else "embed",
                has_content=False,
                embed_count=1,
                view=view,
                started=started,
            )
            raise
        return await self._primary(embed=embed, view=view, payload_kind="embed_view" if view else "embed")

    async def primary_text(self, content: str) -> discord.InteractionMessage | None:
        started = time.monotonic()
        try:
            validate_message(content=content)
        except DiscordPayloadValidationError as error:
            self._log_validation_failure(
                error,
                payload_kind="content",
                has_content=True,
                embed_count=0,
                view=None,
                started=started,
            )
            raise
        return await self._primary(content=content, payload_kind="content")

    def _log_validation_failure(
        self,
        error: DiscordPayloadValidationError,
        *,
        payload_kind: str,
        has_content: bool,
        embed_count: int,
        view: discord.ui.View | None,
        started: float,
    ) -> None:
        components = len(view.children) if view is not None else 0
        event(
            self.logger,
            logging.ERROR,
            "discord_payload_rejected",
            command_name=self.command_name,
            error_type=type(error).__name__,
            validation_message=sanitize_discord_text(error),
            interaction_type=getattr(
                getattr(self.interaction, "type", None), "name", "UNKNOWN"
            ),
            response_state="deferred" if self.deferred else "unacknowledged",
            response_acknowledged=self.interaction.response.is_done(),
            defer_occurred=self.deferred,
            ephemeral=self.visibility.ephemeral,
            payload_kind=payload_kind,
            has_content=has_content,
            embed_count=embed_count,
            component_count=components,
            has_view=view is not None,
            duration_ms=round((time.monotonic() - started) * 1000, 1),
            result="failure",
        )

    async def _primary(
        self,
        *,
        content: str | None = None,
        embed: discord.Embed | None = None,
        view: discord.ui.View | None = None,
        payload_kind: str,
    ) -> discord.InteractionMessage | None:
        if self.primary_completed:
            raise RuntimeError("primary Discord response already completed")
        started = time.monotonic()
        components = component_count(view)
        try:
            if self.deferred:
                message = await self._edit_deferred_original_exact(
                    content=content, embed=embed, view=view
                )
                state = "deferred_original_exact_edit"
            elif not self.interaction.response.is_done():
                initial_kwargs: dict[str, Any] = {"ephemeral": self.visibility.ephemeral}
                if content is not None:
                    initial_kwargs["content"] = content
                if embed is not None:
                    initial_kwargs["embed"] = embed
                if view is not None:
                    initial_kwargs["view"] = view
                await self.interaction.response.send_message(**initial_kwargs)
                message = None
                state = "initial_response"
            else:
                raise RuntimeError("interaction was acknowledged outside its response session")
        except discord.HTTPException as error:
            log_discord_http_failure(
                self.logger,
                error,
                command_name=self.command_name,
                interaction=self.interaction,
                response_state="deferred" if self.deferred else "unacknowledged",
                defer_occurred=self.deferred,
                ephemeral=self.visibility.ephemeral,
                payload_kind=payload_kind,
                has_content=content is not None,
                embed_count=int(embed is not None),
                components=components,
                started=started,
            )
            raise
        except DiscordPayloadValidationError:
            raise
        self.primary_completed = True
        event(
            self.logger,
            logging.INFO,
            "discord_response_success",
            command_name=self.command_name,
            response_state=state,
            ephemeral=self.visibility.ephemeral,
            payload_kind=payload_kind,
            embed_count=int(embed is not None),
            component_count=components,
            duration_ms=round((time.monotonic() - started) * 1000, 1),
            result="success",
        )
        return message

    async def _edit_deferred_original_exact(
        self,
        *,
        content: str | None,
        embed: discord.Embed | None,
        view: discord.ui.View | None,
    ) -> None:
        """PATCH the deferred response with only fields Discord accepts for an edit.

        discord.py's shared send/edit serializer includes send-only defaults on an
        interaction-message PATCH. Discord has rejected that shape in production even
        though mocked adapters accepted it. Keep the pinned library for interaction
        lifecycle/state, but make this protocol boundary explicit and observable.
        """
        await edit_deferred_original_exact(
            self.interaction,
            content=content,
            embed=embed,
            view=view,
        )

    async def followup_text(
        self, content: str, visibility: ResponseVisibility = ResponseVisibility.PRIVATE
    ) -> discord.WebhookMessage | None:
        if not self.interaction.response.is_done():
            raise RuntimeError("secondary followup requires an acknowledged interaction")
        validate_message(content=content)
        started = time.monotonic()
        try:
            return await self.interaction.followup.send(
                content, ephemeral=visibility.ephemeral, wait=True
            )
        except discord.HTTPException as error:
            log_discord_http_failure(
                self.logger,
                error,
                command_name=self.command_name,
                interaction=self.interaction,
                response_state="followup",
                defer_occurred=self.deferred,
                ephemeral=visibility.ephemeral,
                payload_kind="content",
                has_content=True,
                embed_count=0,
                components=0,
                started=started,
            )
            raise


async def respond_command_error(interaction: discord.Interaction, message: str) -> None:
    try:
        if interaction.response.is_done():
            await edit_deferred_original_exact(interaction, content=message)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except (discord.HTTPException, discord.InteractionResponded):
        return


async def respond_component_error(interaction: discord.Interaction, message: str) -> None:
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except (discord.HTTPException, discord.InteractionResponded):
        return
