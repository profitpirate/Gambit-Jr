"""Discord magic-login, wallet proof and execution-provider control plane."""
from __future__ import annotations

import hashlib
import json
import secrets
import time
import uuid
from dataclasses import dataclass
from typing import Protocol

from memecoin_bot.access_store import AccessStore


class WalletSignatureVerifier(Protocol):
    def verify(self, wallet: str, message: bytes, signature: str) -> bool: ...


class SoldersWalletSignatureVerifier:
    def verify(self, wallet: str, message: bytes, signature: str) -> bool:
        from solders.pubkey import Pubkey
        from solders.signature import Signature

        return bool(
            Signature.from_string(str(signature)).verify(
                Pubkey.from_string(str(wallet)),
                bytes(message),
            )
        )


@dataclass(frozen=True, slots=True)
class LoginGrant:
    user_id: int
    discord_user_id: str
    code: str
    magic_url: str
    expires_ns: int


@dataclass(frozen=True, slots=True)
class WalletChallenge:
    challenge_id: str
    wallet: str
    message: str
    expires_ns: int


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class AccessService:
    def __init__(
        self,
        store: AccessStore,
        *,
        portal_base_url: str,
        login_ttl_seconds: int = 600,
        session_ttl_seconds: int = 86_400,
        wallet_challenge_ttl_seconds: int = 600,
    ):
        self.store = store
        self.portal_base_url = portal_base_url.rstrip("/")
        self.login_ttl_seconds = int(login_ttl_seconds)
        self.session_ttl_seconds = int(session_ttl_seconds)
        self.wallet_challenge_ttl_seconds = int(wallet_challenge_ttl_seconds)

    def issue_discord_login(self, discord_user_id: str) -> LoginGrant:
        row = self.store.user_for_discord(str(discord_user_id))
        if not bool(row["enabled"]):
            raise PermissionError("user access is disabled")
        code = secrets.token_urlsafe(32)
        now = time.time_ns()
        expires = now + int(self.login_ttl_seconds * 1e9)
        self.store.conn.execute(
            """
            INSERT INTO access_login_grants(grant_hash,user_id,expires_ns,created_ns)
            VALUES(?,?,?,?)
            """,
            (_hash(code), int(row["id"]), expires, now),
        )
        self.store.audit(int(row["id"]), "LOGIN_GRANT_ISSUED")
        return LoginGrant(
            user_id=int(row["id"]),
            discord_user_id=str(row["discord_user_id"]),
            code=code,
            magic_url=f"{self.portal_base_url}/login?code={code}",
            expires_ns=expires,
        )

    def redeem_login_with_csrf(self, code: str) -> tuple[str, str]:
        digest = _hash(code)
        now = time.time_ns()
        self.store.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.store.conn.execute(
                """
                SELECT g.*,u.enabled
                FROM access_login_grants g JOIN access_users u ON u.id=g.user_id
                WHERE g.grant_hash=?
                """,
                (digest,),
            ).fetchone()
            if (
                row is None
                or not bool(row["enabled"])
                or row["consumed_ns"] is not None
                or int(row["expires_ns"]) < now
            ):
                self.store.conn.execute("ROLLBACK")
                raise PermissionError("login grant is invalid, expired or already used")
            session = secrets.token_urlsafe(48)
            csrf = secrets.token_urlsafe(32)
            self.store.conn.execute(
                "UPDATE access_login_grants SET consumed_ns=? WHERE grant_hash=?",
                (now, digest),
            )
            self.store.conn.execute(
                """
                INSERT INTO access_sessions(
                    session_hash,user_id,expires_ns,created_ns,csrf_hash
                ) VALUES(?,?,?,?,?)
                """,
                (
                    _hash(session),
                    int(row["user_id"]),
                    now + int(self.session_ttl_seconds * 1e9),
                    now,
                    _hash(csrf),
                ),
            )
            self.store.conn.execute("COMMIT")
        except Exception:
            if self.store.conn.in_transaction:
                self.store.conn.execute("ROLLBACK")
            raise
        self.store.audit(int(row["user_id"]), "LOGIN_GRANT_REDEEMED")
        return session, csrf

    def redeem_login(self, code: str) -> str:
        session, _csrf = self.redeem_login_with_csrf(code)
        return session

    def validate_csrf(self, session_token: str, csrf_token: str) -> None:
        if not session_token or not csrf_token:
            raise PermissionError("CSRF token is required")
        row = self.store.conn.execute(
            "SELECT csrf_hash FROM access_sessions WHERE session_hash=?",
            (_hash(session_token),),
        ).fetchone()
        if row is None or not row["csrf_hash"]:
            raise PermissionError("session has no CSRF binding")
        import hmac

        if not hmac.compare_digest(str(row["csrf_hash"]), _hash(csrf_token)):
            raise PermissionError("CSRF token is invalid")

    def authenticate(self, session_token: str) -> int:
        now = time.time_ns()
        row = self.store.conn.execute(
            """
            SELECT s.user_id,u.enabled,s.expires_ns,s.revoked_ns
            FROM access_sessions s JOIN access_users u ON u.id=s.user_id
            WHERE s.session_hash=?
            """,
            (_hash(session_token),),
        ).fetchone()
        if (
            row is None
            or not bool(row["enabled"])
            or row["revoked_ns"] is not None
            or int(row["expires_ns"]) < now
        ):
            raise PermissionError("session is invalid or expired")
        return int(row["user_id"])

    def revoke_session(self, session_token: str) -> None:
        self.store.conn.execute(
            "UPDATE access_sessions SET revoked_ns=? WHERE session_hash=? AND revoked_ns IS NULL",
            (time.time_ns(), _hash(session_token)),
        )

    def create_wallet_challenge(self, user_id: int, wallet: str) -> WalletChallenge:
        wallet = str(wallet).strip()
        if not wallet:
            raise ValueError("wallet is required")
        challenge_id = uuid.uuid4().hex
        nonce = secrets.token_urlsafe(24)
        now = time.time_ns()
        expires = now + int(self.wallet_challenge_ttl_seconds * 1e9)
        message = (
            "Gambit V12 wallet ownership proof\n"
            f"User: {int(user_id)}\n"
            f"Wallet: {wallet}\n"
            f"Challenge: {challenge_id}\n"
            f"Nonce: {nonce}\n"
            f"Expires: {expires}\n"
            "This signature does not authorize a transaction or transfer funds."
        )
        self.store.conn.execute(
            """
            INSERT INTO access_wallet_challenges(
                challenge_id,user_id,wallet,message,expires_ns,created_ns
            ) VALUES(?,?,?,?,?,?)
            """,
            (challenge_id, int(user_id), wallet, message, expires, now),
        )
        self.store.audit(user_id, "WALLET_CHALLENGE_ISSUED", json.dumps({"wallet": wallet}))
        return WalletChallenge(challenge_id, wallet, message, expires)

    def verify_wallet(
        self,
        user_id: int,
        challenge_id: str,
        signature: str,
        verifier: WalletSignatureVerifier,
        *,
        label: str | None = None,
    ) -> str:
        now = time.time_ns()
        self.store.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.store.conn.execute(
                """
                SELECT * FROM access_wallet_challenges
                WHERE challenge_id=? AND user_id=?
                """,
                (str(challenge_id), int(user_id)),
            ).fetchone()
            if (
                row is None
                or row["consumed_ns"] is not None
                or int(row["expires_ns"]) < now
            ):
                self.store.conn.execute("ROLLBACK")
                raise PermissionError("wallet challenge is invalid, expired or already used")
            wallet = str(row["wallet"])
            if not verifier.verify(wallet, str(row["message"]).encode(), signature):
                self.store.conn.execute("ROLLBACK")
                raise PermissionError("wallet signature verification failed")
            self.store.conn.execute(
                "UPDATE access_wallet_challenges SET consumed_ns=? WHERE challenge_id=?",
                (now, str(challenge_id)),
            )
            self.store.conn.execute(
                """
                INSERT INTO access_wallets(user_id,wallet,verified_ns,label)
                VALUES(?,?,?,?)
                ON CONFLICT(user_id,wallet)
                DO UPDATE SET verified_ns=excluded.verified_ns,label=excluded.label
                """,
                (int(user_id), wallet, now, label),
            )
            self.store.conn.execute("COMMIT")
        except Exception:
            if self.store.conn.in_transaction:
                self.store.conn.execute("ROLLBACK")
            raise
        self.store.audit(user_id, "WALLET_VERIFIED", json.dumps({"wallet": wallet}))
        return wallet

    def set_execution_connection(
        self,
        user_id: int,
        *,
        provider: str,
        mode: str,
        public_identifier: str,
        secret_ref: str | None,
    ) -> int:
        provider = provider.upper()
        mode = mode.upper()
        if provider == "AXIOM":
            raise RuntimeError(
                "Axiom direct account linking is disabled until an official supported "
                "third-party authorization/API mechanism is verified."
            )
        if provider not in {"NATIVE_WALLET", "MANAGED_WALLET"}:
            raise ValueError("unsupported execution provider")
        if not secret_ref or not secret_ref.startswith("vault://"):
            raise ValueError(
                "live execution requires a HashiCorp Vault Transit signer reference; "
                "raw private keys and unimplemented signer schemes are forbidden"
            )
        if provider == "NATIVE_WALLET":
            owned = self.store.conn.execute(
                "SELECT 1 FROM access_wallets WHERE user_id=? AND wallet=?",
                (int(user_id), str(public_identifier)),
            ).fetchone()
            if owned is None:
                raise PermissionError(
                    "native execution connection requires a verified wallet"
                )
        now = time.time_ns()
        cursor = self.store.conn.execute(
            """
            INSERT INTO access_execution_connections(
                user_id,provider,mode,public_identifier,secret_ref,state,created_ns,updated_ns
            ) VALUES(?,?,?,?,?,'READY',?,?)
            ON CONFLICT(user_id,provider,public_identifier)
            DO UPDATE SET mode=excluded.mode,secret_ref=excluded.secret_ref,
                          state='READY',updated_ns=excluded.updated_ns
            """,
            (
                int(user_id),
                provider,
                mode,
                str(public_identifier),
                secret_ref,
                now,
                now,
            ),
        )
        self.store.audit(
            user_id,
            "EXECUTION_CONNECTION_SET",
            json.dumps(
                {
                    "provider": provider,
                    "mode": mode,
                    "public_identifier": public_identifier,
                }
            ),
        )
        if cursor.lastrowid:
            return int(cursor.lastrowid)
        row = self.store.conn.execute(
            """
            SELECT id FROM access_execution_connections
            WHERE user_id=? AND provider=? AND public_identifier=?
            """,
            (int(user_id), provider, str(public_identifier)),
        ).fetchone()
        assert row is not None
        return int(row["id"])

    def set_mandate(
        self,
        user_id: int,
        *,
        enabled: bool,
        max_active_bankroll_sol: float,
        max_position_fraction: float,
        max_concurrent_positions: int,
        storage_wallet: str | None,
    ) -> None:
        if max_active_bankroll_sol < 0:
            raise ValueError("max active bankroll cannot be negative")
        if enabled and max_active_bankroll_sol <= 0:
            raise ValueError("enabled mandate requires positive active bankroll")
        if not 0 < max_position_fraction <= 0.20:
            raise ValueError("max position fraction must be >0 and <=20%")
        if not 1 <= int(max_concurrent_positions) <= 2:
            raise ValueError("maximum concurrent positions must be 1 or 2")
        normalized_storage = str(storage_wallet or "").strip()
        if enabled:
            connection = self.store.conn.execute(
                """
                SELECT provider,public_identifier,state,secret_ref
                FROM access_execution_connections
                WHERE user_id=? AND state='READY'
                ORDER BY updated_ns DESC LIMIT 1
                """,
                (int(user_id),),
            ).fetchone()
            if connection is None:
                raise PermissionError(
                    "enabled mandate requires a provisioned execution connection"
                )
            if not str(connection["secret_ref"] or "").startswith("vault://"):
                raise PermissionError("live mandate requires Vault-backed signing")
            if not normalized_storage:
                raise ValueError("enabled mandate requires a storage wallet")
            try:
                from solders.pubkey import Pubkey

                Pubkey.from_string(normalized_storage)
            except (ValueError, TypeError) as exc:
                raise ValueError("storage wallet is not a valid Solana public key") from exc
            if normalized_storage == str(connection["public_identifier"] or ""):
                raise ValueError(
                    "storage wallet must be distinct from the trading wallet"
                )

        now = time.time_ns()
        self.store.conn.execute(
            """
            INSERT INTO access_mandates(
                user_id,enabled,max_active_bankroll_sol,max_position_fraction,
                max_concurrent_positions,storage_wallet,updated_ns
            ) VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(user_id)
            DO UPDATE SET enabled=excluded.enabled,
                          max_active_bankroll_sol=excluded.max_active_bankroll_sol,
                          max_position_fraction=excluded.max_position_fraction,
                          max_concurrent_positions=excluded.max_concurrent_positions,
                          storage_wallet=excluded.storage_wallet,
                          updated_ns=excluded.updated_ns
            """,
            (
                int(user_id),
                int(enabled),
                float(max_active_bankroll_sol),
                float(max_position_fraction),
                int(max_concurrent_positions),
                normalized_storage or None,
                now,
            ),
        )
        self.store.audit(user_id, "EXECUTION_MANDATE_UPDATED")
