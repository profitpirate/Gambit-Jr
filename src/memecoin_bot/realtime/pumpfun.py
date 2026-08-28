from __future__ import annotations

import base64
import binascii
import struct
from dataclasses import dataclass
from typing import Any

PUMP_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
WRAPPED_SOL_MINT = "So11111111111111111111111111111111111111112"
SYSTEM_PROGRAM_ID = "11111111111111111111111111111111"

BONDING_CURVE_DISCRIMINATOR = bytes((23, 183, 248, 55, 96, 216, 172, 96))
CREATE_EVENT_DISCRIMINATOR = bytes((27, 114, 169, 77, 222, 235, 99, 118))
TRADE_EVENT_DISCRIMINATOR = bytes((189, 219, 127, 211, 78, 230, 97, 238))
COMPLETE_EVENT_DISCRIMINATOR = bytes((95, 114, 97, 156, 212, 46, 152, 8))
MIGRATION_EVENT_DISCRIMINATOR = bytes((189, 233, 93, 185, 92, 148, 234, 148))

_BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58encode(raw: bytes) -> str:
    number = int.from_bytes(raw, "big")
    encoded = ""
    while number:
        number, remainder = divmod(number, 58)
        encoded = _BASE58_ALPHABET[remainder] + encoded
    leading = len(raw) - len(raw.lstrip(b"\0"))
    return "1" * leading + (encoded or ("" if leading else "1"))


class BorshDecodeError(ValueError):
    pass


@dataclass(slots=True)
class _Reader:
    data: bytes
    offset: int = 0

    def take(self, length: int) -> bytes:
        if length < 0 or self.offset + length > len(self.data):
            raise BorshDecodeError("truncated Borsh payload")
        result = self.data[self.offset : self.offset + length]
        self.offset += length
        return result

    def u8(self) -> int:
        return self.take(1)[0]

    def boolean(self) -> bool:
        value = self.u8()
        if value not in (0, 1):
            raise BorshDecodeError("invalid Borsh bool")
        return bool(value)

    def u16(self) -> int:
        return struct.unpack("<H", self.take(2))[0]

    def u32(self) -> int:
        return struct.unpack("<I", self.take(4))[0]

    def u64(self) -> int:
        return struct.unpack("<Q", self.take(8))[0]

    def i64(self) -> int:
        return struct.unpack("<q", self.take(8))[0]

    def pubkey(self) -> str:
        return b58encode(self.take(32))

    def string(self, max_length: int = 4_096) -> str:
        length = self.u32()
        if length > max_length:
            raise BorshDecodeError("unreasonable Borsh string length")
        try:
            return self.take(length).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise BorshDecodeError("invalid UTF-8 Borsh string") from exc


def _is_sol_quote(quote_mint: str | None) -> bool:
    return quote_mint in (None, SYSTEM_PROGRAM_ID, WRAPPED_SOL_MINT)


def decode_bonding_curve_account(raw: bytes) -> dict[str, Any]:
    """Decode both legacy SOL curves and the current 115-byte Pump account."""
    if len(raw) < 81:
        raise BorshDecodeError(f"bonding curve account is too short: {len(raw)}")
    if raw[:8] != BONDING_CURVE_DISCRIMINATOR:
        raise BorshDecodeError("invalid Pump BondingCurve discriminator")
    reader = _Reader(raw, 8)
    value: dict[str, Any] = {
        "virtual_token_reserves": reader.u64(),
        "virtual_quote_reserves": reader.u64(),
        "real_token_reserves": reader.u64(),
        "real_quote_reserves": reader.u64(),
        "token_total_supply": reader.u64(),
        "curve_complete": reader.boolean(),
        "creator": reader.pubkey(),
        "is_mayhem_mode": None,
        "is_cashback_coin": None,
        "quote_mint": None,
        "account_layout": "LEGACY_SOL_V1",
    }
    if len(raw) >= 82:
        value["is_mayhem_mode"] = reader.boolean()
        value["account_layout"] = "SOL_EXTENDED_V2"
    if len(raw) >= 83:
        value["is_cashback_coin"] = reader.boolean()
    if len(raw) >= 115:
        quote_raw = reader.take(32)
        value["quote_mint"] = None if quote_raw == bytes(32) else b58encode(quote_raw)
        value["account_layout"] = "QUOTE_AWARE_V3"
    value["quote_is_sol"] = _is_sol_quote(value["quote_mint"])
    value["virtual_sol_reserves"] = (
        value["virtual_quote_reserves"] if value["quote_is_sol"] else None
    )
    value["real_sol_reserves"] = value["real_quote_reserves"] if value["quote_is_sol"] else None
    return value


def decode_account_data(value: Any) -> bytes:
    encoded = value[0] if isinstance(value, list) and value else value
    if not isinstance(encoded, str):
        raise BorshDecodeError("account data is not base64 text")
    try:
        return base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise BorshDecodeError("invalid base64 account data") from exc


def decode_anchor_event(raw: bytes) -> dict[str, Any] | None:
    if len(raw) < 8:
        return None
    discriminator = raw[:8]
    reader = _Reader(raw, 8)
    if discriminator == CREATE_EVENT_DISCRIMINATOR:
        value = {
            "anchor_event": "CreateEvent",
            "name": reader.string(256),
            "symbol": reader.string(64),
            "uri": reader.string(2_048),
            "mint": reader.pubkey(),
            "bonding_curve": reader.pubkey(),
            "user": reader.pubkey(),
            "creator": reader.pubkey(),
            "timestamp": reader.i64(),
            "virtual_token_reserves": reader.u64(),
            "virtual_sol_reserves": reader.u64(),
            "real_token_reserves": reader.u64(),
            "token_total_supply": reader.u64(),
        }
        # Current CreateEvent appends token program and quote-aware fields.
        if len(raw) - reader.offset >= 32:
            value["token_program"] = reader.pubkey()
        if len(raw) - reader.offset >= 1:
            value["is_mayhem_mode"] = reader.boolean()
        if len(raw) - reader.offset >= 1:
            value["is_cashback_enabled"] = reader.boolean()
        if len(raw) - reader.offset >= 32:
            quote = reader.pubkey()
            value["quote_mint"] = None if quote == SYSTEM_PROGRAM_ID else quote
        if len(raw) - reader.offset >= 8:
            value["virtual_quote_reserves"] = reader.u64()
        value["real_sol_reserves"] = 0 if _is_sol_quote(value.get("quote_mint")) else None
        value["real_quote_reserves"] = value["real_sol_reserves"]
        return value
    if discriminator == TRADE_EVENT_DISCRIMINATOR:
        value = {
            "anchor_event": "TradeEvent",
            "mint": reader.pubkey(),
            "sol_amount": reader.u64(),
            "token_amount": reader.u64(),
            "is_buy": reader.boolean(),
            "user": reader.pubkey(),
            "timestamp": reader.i64(),
            "virtual_sol_reserves": reader.u64(),
            "virtual_token_reserves": reader.u64(),
            "real_sol_reserves": reader.u64(),
            "real_token_reserves": reader.u64(),
        }
        # The tail evolves frequently. The fixed prefix is the authoritative
        # backwards-compatible SOL trade evidence. Preserve remaining bytes.
        value["unparsed_tail_bytes"] = len(raw) - reader.offset
        return value
    if discriminator == COMPLETE_EVENT_DISCRIMINATOR:
        return {
            "anchor_event": "CompleteEvent",
            "user": reader.pubkey(),
            "mint": reader.pubkey(),
            "bonding_curve": reader.pubkey(),
            "timestamp": reader.i64(),
            "quote_mint": reader.pubkey() if len(raw) - reader.offset >= 32 else None,
        }
    if discriminator == MIGRATION_EVENT_DISCRIMINATOR:
        return {
            "anchor_event": "CompletePumpAmmMigrationEvent",
            "user": reader.pubkey(),
            "mint": reader.pubkey(),
            "mint_amount": reader.u64(),
            "sol_amount": reader.u64(),
            "pool_migration_fee": reader.u64(),
            "bonding_curve": reader.pubkey(),
            "timestamp": reader.i64(),
            "pool": reader.pubkey(),
            "quote_mint": reader.pubkey() if len(raw) - reader.offset >= 32 else None,
        }
    return None


def anchor_events_from_logs(
    logs: list[Any], program_id: str | None = None
) -> list[dict[str, Any]]:
    """Decode Anchor events, optionally restricting data to one invocation stack.

    A transaction can contain ``Program data`` emitted by CPI programs. Solana
    does not repeat the program id on that line, so callers parsing full
    transaction logs must follow the invoke/success stack instead of decoding
    every discriminator-shaped payload they encounter.
    """
    events: list[dict[str, Any]] = []
    invocation_stack: list[str] = []
    for line in logs:
        text = str(line)
        if text.startswith("Program ") and " invoke [" in text:
            invocation_stack.append(text.split(" ", 2)[1])
            continue
        if text.startswith("Program ") and (
            text.endswith(" success") or " failed: " in text
        ):
            completed = text.split(" ", 2)[1]
            if completed in invocation_stack:
                while invocation_stack:
                    if invocation_stack.pop() == completed:
                        break
            continue
        marker = "Program data: "
        if marker not in text:
            continue
        if program_id is not None and (
            not invocation_stack or invocation_stack[-1] != program_id
        ):
            continue
        encoded = text.split(marker, 1)[1].strip()
        try:
            decoded = base64.b64decode(encoded, validate=True)
            event = decode_anchor_event(decoded)
        except (binascii.Error, BorshDecodeError, ValueError):
            continue
        if event:
            events.append(event)
    return events


def jito_tip_evidence(
    transaction: dict[str, Any], tip_accounts: set[str]
) -> dict[str, Any]:
    """Find observable transfers to official Jito tip accounts; not a bundle proof."""
    result = transaction.get("result") or transaction
    message = ((result.get("transaction") or {}).get("message") or {})
    meta = result.get("meta") or {}
    instructions = list(message.get("instructions") or [])
    for group in meta.get("innerInstructions") or []:
        instructions.extend(group.get("instructions") or [])
    matches: list[dict[str, Any]] = []
    for instruction in instructions:
        parsed = instruction.get("parsed") if isinstance(instruction, dict) else None
        info = parsed.get("info") or {} if isinstance(parsed, dict) else {}
        if not isinstance(info, dict):
            continue
        destination = str(info.get("destination") or "")
        lamports = info.get("lamports")
        if destination in tip_accounts and lamports is not None:
            matches.append(
                {
                    "tip_payer": info.get("source"),
                    "tip_account": destination,
                    "tip_lamports": int(lamports),
                }
            )
    return {
        "jito_tip_present": bool(matches),
        "jito_tip_lamports": sum(row["tip_lamports"] for row in matches),
        "jito_tip_payers": sorted(
            {str(row["tip_payer"]) for row in matches if row.get("tip_payer")}
        ),
        "likely_bundled": bool(matches),
        "bundle_evidence_state": "PROBABILISTIC_TIP_EVIDENCE" if matches else "NO_TIP_OBSERVED",
    }
