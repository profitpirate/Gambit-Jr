from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping

import aiohttp

LOGGER = logging.getLogger("memecoin_bot.discord.cupsey_watch")

OFFICIAL_URL = "https://www.cupsey.wtf/"
PUMPPORTAL_WS_URL = "wss://pumpportal.fun/api/data"
CUPSEY_WALLET = "2fg5QD1eD7rzNNCsvnhmXFm5hqNgwTTG8p7kQ6f3rx6f"
LAUNCH_AT = datetime(2026, 9, 5, 18, 0, 0, tzinfo=UTC)
EARLIEST_ALERT_AT = LAUNCH_AT - timedelta(seconds=90)
EXPIRES_AT = LAUNCH_AT + timedelta(hours=1)
BASE58 = re.compile(r"(?<![1-9A-HJ-NP-Za-km-z])([1-9A-HJ-NP-Za-km-z]{32,44})(?![1-9A-HJ-NP-Za-km-z])")
CONTRACT_LINK = re.compile(
    r"(?:pump\.fun/(?:coin/)?|solscan\.io/token/|dexscreener\.com/solana/)"
    r"([1-9A-HJ-NP-Za-km-z]{32,44})",
    re.IGNORECASE,
)
EXCLUDED_ADDRESSES = {
    CUPSEY_WALLET,
    "11111111111111111111111111111111",
    "So11111111111111111111111111111111111111112",
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
}


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _base58_decode(value: str) -> bytes:
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    number = 0
    for character in value:
        index = alphabet.find(character)
        if index < 0:
            raise ValueError("invalid base58 character")
        number = number * 58 + index
    payload = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    leading = len(value) - len(value.lstrip("1"))
    return b"\0" * leading + payload


def valid_solana_address(value: str) -> bool:
    try:
        return len(_base58_decode(value)) == 32
    except ValueError:
        return False


def extract_official_contract(html: str) -> str | None:
    """Extract only a credible mint exposed by Cupsey's official website.

    Linked Pump/Solscan/Dexscreener addresses take priority. A bare base58 value
    is accepted only when it occurs close to a contract/CA label, preventing JS
    asset hashes or unrelated wallet strings from triggering an @everyone ping.
    """
    for candidate in CONTRACT_LINK.findall(html):
        if candidate not in EXCLUDED_ADDRESSES and valid_solana_address(candidate):
            return candidate

    lowered = html.lower()
    for match in BASE58.finditer(html):
        candidate = match.group(1)
        if candidate in EXCLUDED_ADDRESSES or not valid_solana_address(candidate):
            continue
        context = lowered[max(0, match.start() - 160) : min(len(lowered), match.end() + 160)]
        if any(label in context for label in ("contract address", "contract", "copy ca", "token mint", "official ca")):
            return candidate
    return None


def extract_wallet_launch(payload: Mapping[str, Any]) -> str | None:
    transaction_type = str(
        payload.get("txType")
        or payload.get("tx_type")
        or payload.get("type")
        or payload.get("event")
        or ""
    ).lower()
    creator = str(
        payload.get("traderPublicKey")
        or payload.get("trader_public_key")
        or payload.get("creator")
        or payload.get("user")
        or ""
    )
    mint = str(payload.get("mint") or payload.get("tokenAddress") or payload.get("token_address") or "")
    if creator != CUPSEY_WALLET:
        return None
    if transaction_type and not any(term in transaction_type for term in ("create", "newtoken", "launch")):
        return None
    if mint in EXCLUDED_ADDRESSES or not valid_solana_address(mint):
        return None
    return mint


@dataclass(frozen=True, slots=True)
class DropEvidence:
    mint: str
    source: str
    detected_at: datetime
    detail: str


class CupseyDropWatcher:
    def __init__(
        self,
        *,
        discord_token: str | None,
        channel_id: int | None,
        webhook_url: str | None = None,
        state_path: Path | None = None,
        official_url: str = OFFICIAL_URL,
        pumpportal_url: str = PUMPPORTAL_WS_URL,
    ) -> None:
        self.discord_token = discord_token
        self.channel_id = channel_id
        self.webhook_url = webhook_url
        self.state_path = state_path or Path(
            os.getenv("CUPSEY_DROP_STATE_PATH", "data/cupsey-drop-watch.json")
        )
        self.official_url = official_url
        self.pumpportal_url = pumpportal_url
        self._candidate: DropEvidence | None = None
        self._completed = asyncio.Event()

    @property
    def enabled(self) -> bool:
        now = datetime.now(UTC)
        return (
            _bool_env("CUPSEY_DROP_WATCH_ENABLED", True)
            and now < EXPIRES_AT
            and bool(self.webhook_url or (self.discord_token and self.channel_id))
            and not self._already_alerted()
        )

    def _already_alerted(self) -> bool:
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return bool(payload.get("alerted"))

    def _persist(self, evidence: DropEvidence, message_id: str | None) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "alerted": True,
                    "mint": evidence.mint,
                    "source": evidence.source,
                    "detected_at": evidence.detected_at.isoformat(),
                    "message_id": message_id,
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        temporary.replace(self.state_path)

    async def run(self) -> None:
        if not self.enabled:
            LOGGER.info("Cupsey drop watcher inactive or already completed")
            return
        timeout = max(0.0, (EXPIRES_AT - datetime.now(UTC)).total_seconds())
        if timeout <= 0:
            return
        LOGGER.warning(
            "Cupsey watcher armed official_url=%s wallet=%s channel_id=%s",
            self.official_url,
            CUPSEY_WALLET,
            self.channel_id,
        )
        tasks = {
            asyncio.create_task(self._official_site_loop(), name="cupsey-official-site"),
            asyncio.create_task(self._pumpportal_loop(), name="cupsey-wallet-launch"),
        }
        try:
            await asyncio.wait_for(self._completed.wait(), timeout=timeout)
        except TimeoutError:
            LOGGER.warning("Cupsey watcher expired without verified contract")
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _accept(self, evidence: DropEvidence) -> None:
        if self._completed.is_set() or self._already_alerted():
            return
        now = datetime.now(UTC)
        if now < EARLIEST_ALERT_AT:
            self._candidate = evidence
            await asyncio.sleep(max(0.0, (EARLIEST_ALERT_AT - now).total_seconds()))
            if self._completed.is_set() or self._already_alerted():
                return
            evidence = self._candidate or evidence
        message_id = await self._send_alert(evidence)
        self._persist(evidence, message_id)
        self._completed.set()

    async def _official_site_loop(self) -> None:
        timeout = aiohttp.ClientTimeout(total=4, connect=2, sock_read=3)
        headers = {"user-agent": "Gambit-Jr-Cupsey-Watch/1.0", "cache-control": "no-cache"}
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            while not self._completed.is_set() and datetime.now(UTC) < EXPIRES_AT:
                try:
                    nonce = int(datetime.now(UTC).timestamp() * 1000)
                    separator = "&" if "?" in self.official_url else "?"
                    async with session.get(f"{self.official_url}{separator}_gambit={nonce}") as response:
                        html = await response.text()
                    if response.status == 200:
                        mint = extract_official_contract(html)
                        if mint:
                            await self._accept(
                                DropEvidence(
                                    mint=mint,
                                    source="official_website",
                                    detected_at=datetime.now(UTC),
                                    detail="Contract exposed by Cupsey's official website",
                                )
                            )
                            return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - watchdog must survive provider failure
                    LOGGER.debug("Cupsey official-site poll failed: %s", exc)
                seconds_to_launch = (LAUNCH_AT - datetime.now(UTC)).total_seconds()
                await asyncio.sleep(0.35 if seconds_to_launch <= 120 else 1.5)

    async def _pumpportal_loop(self) -> None:
        timeout = aiohttp.ClientTimeout(total=None, connect=5, sock_read=None)
        while not self._completed.is_set() and datetime.now(UTC) < EXPIRES_AT:
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.ws_connect(
                        self.pumpportal_url,
                        heartbeat=15,
                        receive_timeout=30,
                        autoclose=True,
                    ) as socket:
                        await socket.send_json({"method": "subscribeNewToken"})
                        async for message in socket:
                            if self._completed.is_set():
                                return
                            if message.type == aiohttp.WSMsgType.TEXT:
                                try:
                                    payload = json.loads(message.data)
                                except json.JSONDecodeError:
                                    continue
                                if not isinstance(payload, Mapping):
                                    continue
                                mint = extract_wallet_launch(payload)
                                if mint:
                                    await self._accept(
                                        DropEvidence(
                                            mint=mint,
                                            source="cupsey_wallet_onchain",
                                            detected_at=datetime.now(UTC),
                                            detail="New token creation emitted by Cupsey's known public wallet",
                                        )
                                    )
                                    return
                            elif message.type in {
                                aiohttp.WSMsgType.CLOSED,
                                aiohttp.WSMsgType.CLOSING,
                                aiohttp.WSMsgType.ERROR,
                            }:
                                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect loop
                LOGGER.debug("Cupsey PumpPortal watcher reconnecting: %s", exc)
            await asyncio.sleep(0.5)

    def _payload(self, evidence: DropEvidence) -> dict[str, Any]:
        detected = evidence.detected_at.astimezone(UTC).strftime("%H:%M:%S.%f")[:-3] + " UTC"
        return {
            "content": "@everyone 🚨 **CUPSEY DROP DETECTED**",
            "allowed_mentions": {"parse": ["everyone"]},
            "embeds": [
                {
                    "title": "CUPSEY • VERIFIED LAUNCH SIGNAL",
                    "description": (
                        f"**Contract address**\n```\n{evidence.mint}\n```\n"
                        "Detected from an authorised public source. Verify the CA before trading."
                    ),
                    "color": 0xD36A28,
                    "fields": [
                        {"name": "Source", "value": evidence.source, "inline": True},
                        {"name": "Detected", "value": detected, "inline": True},
                        {"name": "Evidence", "value": evidence.detail, "inline": False},
                        {
                            "name": "Links",
                            "value": (
                                f"[Pump.fun](https://pump.fun/coin/{evidence.mint}) • "
                                f"[Solscan](https://solscan.io/token/{evidence.mint}) • "
                                f"[Dexscreener](https://dexscreener.com/solana/{evidence.mint})"
                            ),
                            "inline": False,
                        },
                    ],
                    "footer": {"text": "Gambit Jr. • one-shot Cupsey launch watch"},
                    "timestamp": evidence.detected_at.isoformat(),
                }
            ],
        }

    async def _send_alert(self, evidence: DropEvidence) -> str | None:
        payload = self._payload(evidence)
        if self.webhook_url:
            url = self.webhook_url + ("&" if "?" in self.webhook_url else "?") + "wait=true"
            headers = {"content-type": "application/json"}
        else:
            if not self.discord_token or not self.channel_id:
                raise RuntimeError("Cupsey alert destination is not configured")
            url = f"https://discord.com/api/v10/channels/{self.channel_id}/messages"
            headers = {
                "content-type": "application/json",
                "authorization": f"Bot {self.discord_token}",
            }
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            for attempt in range(4):
                async with session.post(url, json=payload) as response:
                    body = await response.text()
                    if response.status in {200, 201, 204}:
                        if not body:
                            return None
                        try:
                            return str(json.loads(body).get("id") or "") or None
                        except json.JSONDecodeError:
                            return None
                    if response.status == 429 and attempt < 3:
                        try:
                            delay = min(15.0, float(json.loads(body).get("retry_after", 1.0)))
                        except (ValueError, json.JSONDecodeError):
                            delay = 1.0
                        await asyncio.sleep(delay)
                        continue
                    raise RuntimeError(f"Discord HTTP {response.status}: {body[:300]}")
        raise RuntimeError("Cupsey Discord alert retry limit exceeded")


def schedule_cupsey_watch(
    *,
    discord_token: str | None,
    channel_id: int | None,
    webhook_url: str | None = None,
) -> asyncio.Task[None] | None:
    if not _bool_env("CUPSEY_DROP_WATCH_ENABLED", True):
        return None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    watcher = CupseyDropWatcher(
        discord_token=discord_token,
        channel_id=channel_id,
        webhook_url=webhook_url,
    )
    if not watcher.enabled:
        return None
    return loop.create_task(watcher.run(), name="cupsey-drop-watch")


def main() -> None:
    parser = argparse.ArgumentParser(description="One-shot Cupsey launch watcher")
    parser.add_argument("--channel-id", type=int)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    channel_id = args.channel_id or int(
        os.getenv("CUPSEY_ALERT_CHANNEL_ID") or os.getenv("DISCORD_CHANNEL_ID") or "0"
    )
    watcher = CupseyDropWatcher(
        discord_token=os.getenv("DISCORD_TOKEN"),
        channel_id=channel_id or None,
        webhook_url=os.getenv("DISCORD_WEBHOOK_URL"),
    )
    if args.dry_run:
        print(
            json.dumps(
                {
                    "enabled": watcher.enabled,
                    "official_url": watcher.official_url,
                    "wallet": CUPSEY_WALLET,
                    "launch_at": LAUNCH_AT.isoformat(),
                    "expires_at": EXPIRES_AT.isoformat(),
                    "channel_id": watcher.channel_id,
                },
                indent=2,
            )
        )
        return
    asyncio.run(watcher.run())


if __name__ == "__main__":
    main()
