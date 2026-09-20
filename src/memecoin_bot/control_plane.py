"""Minimal authenticated control-plane API for a small V12 beta."""
from __future__ import annotations

import hmac
import os
from typing import Any

from aiohttp import web

from memecoin_bot.access_service import AccessService, SoldersWalletSignatureVerifier
from memecoin_bot.discord.notifier import DiscordNotifier


def _json_error(message: str, status: int) -> web.Response:
    return web.json_response({"ok": False, "error": message}, status=status)


def _bearer(request: web.Request) -> str:
    value = request.headers.get("Authorization", "")
    if not value.startswith("Bearer "):
        raise PermissionError("bearer session is required")
    return value[7:].strip()


class ControlPlane:
    def __init__(
        self,
        access: AccessService,
        *,
        discord: DiscordNotifier | None,
        admin_key: str,
    ):
        self.access = access
        self.discord = discord
        self.admin_key = admin_key
        self.wallet_verifier = SoldersWalletSignatureVerifier()

    def app(self) -> web.Application:
        app = web.Application(client_max_size=64 * 1024)
        app.add_routes(
            [
                web.get("/health", self.health),
                web.post("/v1/admin/invite", self.invite),
                web.post("/v1/auth/redeem", self.redeem),
                web.get("/v1/me", self.me),
                web.get("/v1/providers", self.providers),
                web.post("/v1/wallet/challenge", self.wallet_challenge),
                web.post("/v1/wallet/verify", self.wallet_verify),
                web.post("/v1/execution/connect", self.execution_connect),
                web.post("/v1/mandate", self.mandate),
            ]
        )
        return app

    async def body(self, request: web.Request) -> dict[str, Any]:
        value = await request.json()
        if not isinstance(value, dict):
            raise ValueError("JSON object required")
        return value

    def user_id(self, request: web.Request) -> int:
        return self.access.authenticate(_bearer(request))

    async def health(self, _request: web.Request) -> web.Response:
        return web.json_response(
            {
                "ok": True,
                "discord_commands_enabled": False,
                "axiom_direct_link": False,
                "wallet_link": True,
            }
        )

    async def invite(self, request: web.Request) -> web.Response:
        provided = request.headers.get("X-Gambit-Admin-Key", "")
        if not self.admin_key or not hmac.compare_digest(provided, self.admin_key):
            return _json_error("unauthorized", 401)
        try:
            body = await self.body(request)
            discord_id = str(body["discord_user_id"])
            grant = self.access.issue_discord_login(discord_id)
            if self.discord is None:
                raise RuntimeError("Discord DM delivery is not configured")
            await self.discord.send_dm(
                int(discord_id),
                (
                    "Your Gambit V12 beta login is ready.\n"
                    f"{grant.magic_url}\n\n"
                    "This link is one-time and expires shortly. "
                    "Gambit will never ask for your seed phrase in Discord."
                ),
            )
            return web.json_response(
                {
                    "ok": True,
                    "user_id": grant.user_id,
                    "expires_ns": grant.expires_ns,
                    "delivered": "discord_dm",
                }
            )
        except (KeyError, ValueError) as exc:
            return _json_error(str(exc), 400)
        except RuntimeError as exc:
            return _json_error(str(exc), 503)

    async def redeem(self, request: web.Request) -> web.Response:
        try:
            body = await self.body(request)
            session = self.access.redeem_login(str(body["code"]))
            return web.json_response({"ok": True, "session": session})
        except (KeyError, ValueError) as exc:
            return _json_error(str(exc), 400)
        except PermissionError as exc:
            return _json_error(str(exc), 401)

    async def me(self, request: web.Request) -> web.Response:
        try:
            user_id = self.user_id(request)
            return web.json_response(
                {"ok": True, "account": self.access.store.account_snapshot(user_id)}
            )
        except PermissionError as exc:
            return _json_error(str(exc), 401)

    async def providers(self, request: web.Request) -> web.Response:
        try:
            self.user_id(request)
        except PermissionError as exc:
            return _json_error(str(exc), 401)
        return web.json_response(
            {
                "ok": True,
                "providers": [
                    {
                        "id": "NATIVE_WALLET",
                        "wallet_ownership_link": True,
                        "execution_connection": "vault_or_delegated_signer",
                        "state": "SUPPORTED",
                    },
                    {
                        "id": "AXIOM",
                        "wallet_ownership_link": True,
                        "execution_connection": None,
                        "state": "WAITING_FOR_OFFICIAL_THIRD_PARTY_AUTH",
                        "accepts_passwords": False,
                        "accepts_browser_cookies": False,
                        "accepts_recovery_phrase": False,
                    },
                ],
            }
        )

    async def wallet_challenge(self, request: web.Request) -> web.Response:
        try:
            user_id = self.user_id(request)
            body = await self.body(request)
            challenge = self.access.create_wallet_challenge(user_id, str(body["wallet"]))
            return web.json_response(
                {
                    "ok": True,
                    "challenge_id": challenge.challenge_id,
                    "wallet": challenge.wallet,
                    "message": challenge.message,
                    "expires_ns": challenge.expires_ns,
                }
            )
        except PermissionError as exc:
            return _json_error(str(exc), 401)
        except (KeyError, ValueError) as exc:
            return _json_error(str(exc), 400)

    async def wallet_verify(self, request: web.Request) -> web.Response:
        try:
            user_id = self.user_id(request)
            body = await self.body(request)
            wallet = self.access.verify_wallet(
                user_id,
                str(body["challenge_id"]),
                str(body["signature"]),
                self.wallet_verifier,
                label=str(body.get("label") or "") or None,
            )
            return web.json_response({"ok": True, "wallet": wallet})
        except PermissionError as exc:
            return _json_error(str(exc), 401)
        except (KeyError, ValueError) as exc:
            return _json_error(str(exc), 400)

    async def execution_connect(self, request: web.Request) -> web.Response:
        try:
            user_id = self.user_id(request)
            body = await self.body(request)
            connection_id = self.access.set_execution_connection(
                user_id,
                provider=str(body["provider"]),
                mode=str(body.get("mode") or "DELEGATED"),
                public_identifier=str(body["public_identifier"]),
                secret_ref=(
                    str(body["secret_ref"])
                    if body.get("secret_ref") is not None
                    else None
                ),
            )
            return web.json_response({"ok": True, "connection_id": connection_id})
        except PermissionError as exc:
            return _json_error(str(exc), 401)
        except (KeyError, ValueError) as exc:
            return _json_error(str(exc), 400)
        except RuntimeError as exc:
            return _json_error(str(exc), 409)

    async def mandate(self, request: web.Request) -> web.Response:
        try:
            user_id = self.user_id(request)
            body = await self.body(request)
            self.access.set_mandate(
                user_id,
                enabled=bool(body.get("enabled")),
                max_active_bankroll_sol=float(body["max_active_bankroll_sol"]),
                max_position_fraction=float(body.get("max_position_fraction", 0.10)),
                max_concurrent_positions=int(body.get("max_concurrent_positions", 2)),
                storage_wallet=(
                    str(body["storage_wallet"])
                    if body.get("storage_wallet")
                    else None
                ),
            )
            return web.json_response({"ok": True})
        except PermissionError as exc:
            return _json_error(str(exc), 401)
        except (KeyError, ValueError) as exc:
            return _json_error(str(exc), 400)


def build_from_env(access: AccessService) -> ControlPlane:
    token = os.getenv("DISCORD_TOKEN")
    discord = DiscordNotifier(token, None, None) if token else None
    return ControlPlane(
        access,
        discord=discord,
        admin_key=os.getenv("GAMBIT_PORTAL_ADMIN_KEY", ""),
    )
