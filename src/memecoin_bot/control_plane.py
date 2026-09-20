"""Secure authenticated control-plane API for a small V12 beta."""
from __future__ import annotations

import hmac
import json
import os
import sqlite3
import time
from collections import deque
from pathlib import Path
from typing import Any

from aiohttp import web

from memecoin_bot.access_service import AccessService, SoldersWalletSignatureVerifier
from memecoin_bot.discord.notifier import DiscordNotifier


COOKIE_NAME = "gambit_session"


def _json_error(message: str, status: int) -> web.Response:
    return web.json_response({"ok": False, "error": message}, status=status)


class RateLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = {}

    def check(self, key: str, *, limit: int, window_seconds: float) -> bool:
        now = time.monotonic()
        rows = self._hits.setdefault(key, deque())
        while rows and now - rows[0] > window_seconds:
            rows.popleft()
        if len(rows) >= limit:
            return False
        rows.append(now)
        return True


@web.middleware
async def security_headers_middleware(
    request: web.Request,
    handler: Any,
) -> web.StreamResponse:
    response = await handler(request)
    response.headers.update(
        {
            "Content-Security-Policy": (
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; "
                "base-uri 'none'; form-action 'self'"
            ),
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "X-Frame-Options": "DENY",
            "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
            "Cross-Origin-Opener-Policy": "same-origin",
            "Cross-Origin-Resource-Policy": "same-origin",
            "Cache-Control": "no-store",
        }
    )
    return response


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
        self.web_root = Path(__file__).with_name("portal_web")
        self.rate = RateLimiter()
        self.state_root = Path(os.getenv("V12_ACCOUNTS_ROOT", "data/accounts"))
        self.cookie_secure = os.getenv(
            "GAMBIT_COOKIE_SECURE",
            "true",
        ).lower() in {"1", "true", "yes", "on"}
        self.admin_ips = {
            item.strip()
            for item in os.getenv(
                "GAMBIT_ADMIN_ALLOWED_IPS",
                "127.0.0.1,::1",
            ).split(",")
            if item.strip()
        }

    def app(self) -> web.Application:
        app = web.Application(
            client_max_size=64 * 1024,
            middlewares=[security_headers_middleware],
        )
        app.add_routes(
            [
                web.get("/", self.portal),
                web.get("/login", self.portal),
                web.get("/app", self.portal),
                web.get("/static/app.js", self.portal_js),
                web.get("/static/styles.css", self.portal_css),
                web.get("/health", self.health),
                web.post("/v1/admin/invite", self.invite),
                web.post("/v1/admin/execution", self.admin_execution),
                web.post("/v1/auth/redeem", self.redeem),
                web.post("/v1/auth/logout", self.logout),
                web.get("/v1/me", self.me),
                web.get("/v1/providers", self.providers),
                web.post("/v1/wallet/challenge", self.wallet_challenge),
                web.post("/v1/wallet/verify", self.wallet_verify),
                web.post("/v1/mandate", self.mandate),
                web.get("/v1/runtime/status", self.runtime_status),
                web.post("/v1/runtime/kill", self.runtime_kill),
                web.post("/v1/runtime/resume", self.runtime_resume),
            ]
        )
        return app

    async def portal(self, _request: web.Request) -> web.StreamResponse:
        return web.FileResponse(self.web_root / "index.html")

    async def portal_js(self, _request: web.Request) -> web.StreamResponse:
        return web.FileResponse(
            self.web_root / "app.js",
            headers={"Content-Type": "application/javascript; charset=utf-8"},
        )

    async def portal_css(self, _request: web.Request) -> web.StreamResponse:
        return web.FileResponse(
            self.web_root / "styles.css",
            headers={"Content-Type": "text/css; charset=utf-8"},
        )

    async def body(self, request: web.Request) -> dict[str, Any]:
        value = await request.json()
        if not isinstance(value, dict):
            raise TypeError("JSON object required")
        return value

    def _remote(self, request: web.Request) -> str:
        return str(request.remote or "unknown")

    def _limit(
        self,
        request: web.Request,
        name: str,
        *,
        limit: int,
        window: float,
    ) -> None:
        key = f"{self._remote(request)}|{name}"
        if not self.rate.check(key, limit=limit, window_seconds=window):
            raise web.HTTPTooManyRequests(text="rate limit exceeded")

    def _session_token(self, request: web.Request) -> str:
        cookie = request.cookies.get(COOKIE_NAME)
        if cookie:
            return cookie
        value = request.headers.get("Authorization", "")
        if value.startswith("Bearer "):
            return value[7:].strip()
        raise PermissionError("authenticated session is required")

    def user_id(self, request: web.Request, *, csrf: bool = False) -> int:
        token = self._session_token(request)
        user_id = self.access.authenticate(token)
        if csrf and request.cookies.get(COOKIE_NAME):
            self.access.validate_csrf(
                token,
                request.headers.get("X-CSRF-Token", ""),
            )
        return user_id

    def _admin(self, request: web.Request) -> None:
        self._limit(request, "admin", limit=10, window=60)
        if self._remote(request) not in self.admin_ips:
            raise PermissionError("admin endpoint is not available from this address")
        provided = request.headers.get("X-Gambit-Admin-Key", "")
        if not self.admin_key or not hmac.compare_digest(provided, self.admin_key):
            raise PermissionError("admin authentication failed")

    async def health(self, _request: web.Request) -> web.Response:
        return web.json_response(
            {
                "ok": True,
                "discord_commands_enabled": False,
                "axiom_direct_link": False,
                "wallet_link": True,
                "session_cookie": "httponly",
                "csrf": True,
            }
        )

    async def invite(self, request: web.Request) -> web.Response:
        try:
            self._admin(request)
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
        except PermissionError as exc:
            return _json_error(str(exc), 401)
        except (KeyError, TypeError, ValueError) as exc:
            return _json_error(str(exc), 400)
        except RuntimeError as exc:
            return _json_error(str(exc), 503)

    async def admin_execution(self, request: web.Request) -> web.Response:
        try:
            self._admin(request)
            body = await self.body(request)
            connection = self.access.set_execution_connection(
                int(body["user_id"]),
                provider=str(body["provider"]),
                mode=str(body.get("mode") or "VAULT_TRANSIT"),
                public_identifier=str(body["public_identifier"]),
                secret_ref=str(body["secret_ref"]),
            )
            return web.json_response({"ok": True, "connection_id": connection})
        except PermissionError as exc:
            return _json_error(str(exc), 401)
        except (KeyError, TypeError, ValueError) as exc:
            return _json_error(str(exc), 400)
        except RuntimeError as exc:
            return _json_error(str(exc), 409)

    async def redeem(self, request: web.Request) -> web.Response:
        self._limit(request, "redeem", limit=10, window=60)
        try:
            body = await self.body(request)
            session, csrf = self.access.redeem_login_with_csrf(str(body["code"]))
            response = web.json_response({"ok": True, "csrf": csrf})
            response.set_cookie(
                COOKIE_NAME,
                session,
                httponly=True,
                secure=self.cookie_secure,
                samesite="Strict",
                max_age=self.access.session_ttl_seconds,
                path="/",
            )
            return response
        except (KeyError, TypeError, ValueError) as exc:
            return _json_error(str(exc), 400)
        except PermissionError as exc:
            return _json_error(str(exc), 401)

    async def logout(self, request: web.Request) -> web.Response:
        try:
            token = self._session_token(request)
            self.user_id(request, csrf=True)
            self.access.revoke_session(token)
        except PermissionError as exc:
            return _json_error(str(exc), 401)
        response = web.json_response({"ok": True})
        response.del_cookie(COOKIE_NAME, path="/")
        return response

    async def me(self, request: web.Request) -> web.Response:
        self._limit(request, "api", limit=180, window=60)
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
                        "id": "MANAGED_WALLET",
                        "state": "SUPPORTED",
                        "execution_connection": "vault_transit",
                        "description": (
                            "Isolated trading wallet provisioned in Vault; user keeps "
                            "storage/withdrawal wallet separate."
                        ),
                    },
                    {
                        "id": "NATIVE_WALLET",
                        "state": "SUPPORTED_WITH_VAULT_SIGNER",
                        "wallet_ownership_link": True,
                        "execution_connection": "vault_transit",
                    },
                    {
                        "id": "AXIOM",
                        "state": "WAITING_FOR_OFFICIAL_THIRD_PARTY_AUTH",
                        "execution_connection": None,
                        "accepts_passwords": False,
                        "accepts_browser_cookies": False,
                        "accepts_recovery_phrase": False,
                    },
                ],
            }
        )

    async def wallet_challenge(self, request: web.Request) -> web.Response:
        self._limit(request, "wallet", limit=30, window=60)
        try:
            user_id = self.user_id(request, csrf=True)
            body = await self.body(request)
            challenge = self.access.create_wallet_challenge(
                user_id,
                str(body["wallet"]),
            )
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
        except (KeyError, TypeError, ValueError) as exc:
            return _json_error(str(exc), 400)

    async def wallet_verify(self, request: web.Request) -> web.Response:
        self._limit(request, "wallet", limit=30, window=60)
        try:
            user_id = self.user_id(request, csrf=True)
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
        except (KeyError, TypeError, ValueError) as exc:
            return _json_error(str(exc), 400)

    async def mandate(self, request: web.Request) -> web.Response:
        try:
            user_id = self.user_id(request, csrf=True)
            body = await self.body(request)
            self.access.set_mandate(
                user_id,
                enabled=bool(body.get("enabled")),
                max_active_bankroll_sol=float(body["max_active_bankroll_sol"]),
                max_position_fraction=float(
                    body.get("max_position_fraction", 0.10)
                ),
                max_concurrent_positions=int(
                    body.get("max_concurrent_positions", 2)
                ),
                storage_wallet=(
                    str(body["storage_wallet"])
                    if body.get("storage_wallet")
                    else None
                ),
            )
            return web.json_response({"ok": True})
        except PermissionError as exc:
            return _json_error(str(exc), 401)
        except (KeyError, TypeError, ValueError) as exc:
            return _json_error(str(exc), 400)

    def _runtime_dir(self, user_id: int) -> Path:
        return self.state_root / str(int(user_id))

    async def runtime_status(self, request: web.Request) -> web.Response:
        try:
            user_id = self.user_id(request)
        except PermissionError as exc:
            return _json_error(str(exc), 401)
        runtime = self._runtime_dir(user_id)
        heartbeat = runtime / "heartbeat.json"
        payload: dict[str, Any] = {
            "running": False,
            "heartbeat": None,
            "kill_switch": (runtime / "KILL").exists(),
            "execution": None,
        }
        if heartbeat.exists():
            try:
                hb = json.loads(heartbeat.read_text(encoding="utf-8"))
                payload["heartbeat"] = hb
                payload["running"] = (
                    time.time_ns() - int(hb.get("ts_ns") or 0)
                ) < 30_000_000_000
            except (OSError, ValueError, json.JSONDecodeError):
                pass
        database = runtime / "execution.db"
        if database.exists():
            try:
                conn = sqlite3.connect(
                    f"file:{database.resolve()}?mode=ro",
                    uri=True,
                    timeout=1,
                )
                conn.row_factory = sqlite3.Row
                positions = {
                    str(row[0]): int(row[1])
                    for row in conn.execute(
                        "SELECT status,COUNT(*) FROM e4_positions GROUP BY status"
                    )
                }
                open_positions = [
                    dict(row)
                    for row in conn.execute(
                        """
                        SELECT mint,status,opened_ns,entry_sol,remaining,last_price,
                               realized_sol,entry_signature,close_signature
                        FROM e4_positions
                        WHERE status IN ('OPEN','PARTIAL','EXITING')
                        ORDER BY opened_ns
                        """
                    )
                ]
                recent_trades = [
                    {
                        **dict(row),
                        "pnl_sol": float(row["realized_sol"]) - float(row["entry_sol"]),
                    }
                    for row in conn.execute(
                        """
                        SELECT mint,opened_ns,entry_sol,realized_sol,close_signature
                        FROM e4_positions
                        WHERE status='CLOSED'
                        ORDER BY updated_ns DESC
                        LIMIT 20
                        """
                    )
                ]
                pnl_row = conn.execute(
                    """
                    SELECT COALESCE(SUM(realized_sol-entry_sol),0),
                           COUNT(*)
                    FROM e4_positions
                    WHERE status='CLOSED'
                    """
                ).fetchone()
                sweeps = [
                    dict(row)
                    for row in conn.execute(
                        """
                        SELECT amount,signature,confirmed,confirmation_slot,error,created_ns
                        FROM e4_orders
                        WHERE side='SWEEP'
                        ORDER BY created_ns DESC
                        LIMIT 20
                        """
                    )
                ]
                safety = conn.execute(
                    "SELECT mode,reason,peak_equity_sol,day_start_equity_sol,"
                    "consecutive_losses,tx_failures_window "
                    "FROM v12_safety_state WHERE singleton=1"
                ).fetchone()
                journal = {
                    str(row[0]): int(row[1])
                    for row in conn.execute(
                        "SELECT state,COUNT(*) FROM v12_execution_journal GROUP BY state"
                    )
                }
                payload["execution"] = {
                    "positions": positions,
                    "open_positions": open_positions,
                    "recent_trades": recent_trades,
                    "closed_pnl_sol": float(pnl_row[0]) if pnl_row else 0.0,
                    "closed_trades": int(pnl_row[1]) if pnl_row else 0,
                    "storage_sweeps": sweeps,
                    "safety": dict(safety) if safety else None,
                    "journal": journal,
                }
                conn.close()
            except sqlite3.Error:
                payload["execution"] = {"status": "database_unavailable"}
        return web.json_response({"ok": True, "runtime": payload})

    async def runtime_kill(self, request: web.Request) -> web.Response:
        try:
            user_id = self.user_id(request, csrf=True)
        except PermissionError as exc:
            return _json_error(str(exc), 401)
        runtime = self._runtime_dir(user_id)
        runtime.mkdir(parents=True, exist_ok=True)
        kill = runtime / "KILL"
        kill.write_text(
            json.dumps(
                {"reason": "user_kill_switch", "ts_ns": time.time_ns()},
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        os.chmod(kill, 0o600)
        self.access.store.audit(user_id, "USER_KILL_SWITCH")
        return web.json_response({"ok": True})

    async def runtime_resume(self, request: web.Request) -> web.Response:
        try:
            user_id = self.user_id(request, csrf=True)
        except PermissionError as exc:
            return _json_error(str(exc), 401)
        snapshot = self.access.store.account_snapshot(user_id)
        mandate = snapshot.get("mandate") or {}
        if not bool(mandate.get("enabled")):
            return _json_error("trading mandate is disabled", 409)
        if not snapshot.get("execution_connections"):
            return _json_error("no execution connection is provisioned", 409)
        (self._runtime_dir(user_id) / "KILL").unlink(missing_ok=True)
        self.access.store.audit(user_id, "USER_RUNTIME_RESUME")
        return web.json_response({"ok": True})


def build_from_env(access: AccessService) -> ControlPlane:
    token = os.getenv("DISCORD_TOKEN")
    discord = DiscordNotifier(token, None, None) if token else None
    return ControlPlane(
        access,
        discord=discord,
        admin_key=os.getenv("GAMBIT_PORTAL_ADMIN_KEY", ""),
    )
