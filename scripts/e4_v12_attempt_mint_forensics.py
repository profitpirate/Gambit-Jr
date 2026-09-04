#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit

import aiohttp

E4_WALLET = "E4EzXdwf7NNdqM2XGswWaWHfxgucVCo24PTCcrimTKBz"
PUMP_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
DEFAULT_RPCS = (
    "https://solana-rpc.publicnode.com",
    "https://api.mainnet-beta.solana.com",
)
BUY_NAMES = ("BuyExactSolIn", "Buy")
LEFT_RE = re.compile(r"(?:Program log: )?Left: (\d+)")
RIGHT_RE = re.compile(r"(?:Program log: )?Right: (\d+)")


def integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def median(values: Sequence[float]) -> float | None:
    clean = [float(value) for value in values if math.isfinite(float(value))]
    return statistics.median(clean) if clean else None


def safe_url(url: str) -> str:
    try:
        parsed = urlsplit(url)
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    except Exception:
        return "redacted"


def has_instruction(row: Mapping[str, Any], name: str) -> bool:
    needle = f"Instruction: {name}"
    return any(needle in str(line) for line in row.get("log_messages") or [])


def buy_name(row: Mapping[str, Any]) -> str:
    for name in BUY_NAMES:
        if has_instruction(row, name):
            return name
    return ""


def shortfall(row: Mapping[str, Any]) -> float | None:
    left = None
    right = None
    for line in row.get("log_messages") or []:
        text = str(line)
        left_match = LEFT_RE.search(text)
        right_match = RIGHT_RE.search(text)
        if left_match:
            left = integer(left_match.group(1))
        if right_match:
            right = integer(right_match.group(1))
    if left is None or right is None or right <= 0:
        return None
    return max(-10.0, min(1.0, 1.0 - left / right))


def percentile(values: Sequence[float], fraction: float) -> float | None:
    clean = sorted(float(value) for value in values if math.isfinite(float(value)))
    if not clean:
        return None
    index = min(len(clean) - 1, max(0, int(round((len(clean) - 1) * fraction))))
    return clean[index]


def account_keys(payload: Mapping[str, Any]) -> list[str]:
    message = (((payload.get("transaction") or {}).get("message")) or {})
    output = []
    for value in message.get("accountKeys") or []:
        if isinstance(value, Mapping):
            output.append(str(value.get("pubkey") or ""))
        else:
            output.append(str(value or ""))
    loaded = ((payload.get("meta") or {}).get("loadedAddresses") or {})
    output.extend(str(value or "") for value in loaded.get("writable") or [])
    output.extend(str(value or "") for value in loaded.get("readonly") or [])
    return output


def resolve_program(instruction: Mapping[str, Any], keys: Sequence[str]) -> str:
    explicit = str(instruction.get("programId") or "")
    if explicit:
        return explicit
    index = instruction.get("programIdIndex")
    if isinstance(index, int) and 0 <= index < len(keys):
        return keys[index]
    return ""


def resolve_accounts(instruction: Mapping[str, Any], keys: Sequence[str]) -> list[str]:
    output = []
    for value in instruction.get("accounts") or []:
        if isinstance(value, int):
            output.append(keys[value] if 0 <= value < len(keys) else "")
        elif isinstance(value, Mapping):
            output.append(str(value.get("pubkey") or ""))
        else:
            output.append(str(value or ""))
    return output


def pump_instructions(payload: Mapping[str, Any]) -> list[list[str]]:
    keys = account_keys(payload)
    message = (((payload.get("transaction") or {}).get("message")) or {})
    groups = [message.get("instructions") or []]
    groups.extend(group.get("instructions") or [] for group in (payload.get("meta") or {}).get("innerInstructions") or [])
    output = []
    for instructions in groups:
        for instruction in instructions:
            if isinstance(instruction, Mapping) and resolve_program(instruction, keys) == PUMP_PROGRAM_ID:
                output.append(resolve_accounts(instruction, keys))
    return output


def transaction_index(payload: Mapping[str, Any], fallback: int = -1) -> int:
    return integer(payload.get("transactionIndex"), fallback)


def load_capture(pairs: Sequence[str]) -> tuple[dict[str, dict[str, Any]], int, int]:
    launches: dict[str, dict[str, Any]] = {}
    minimum_ns = 2**63 - 1
    maximum_ns = 0
    for item in pairs:
        batch_text, events_text = item.split(":", 1)
        batch_path = Path(batch_text)
        events_path = Path(events_text)
        batch = json.loads(batch_path.read_text(encoding="utf-8"))
        selected = {
            str(row.get("mint") or "")
            for row in (batch.get("actual_e4_fresh_sample") or {}).get("positions") or []
            if str(row.get("mint") or "")
        }
        with events_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                row = json.loads(line)
                kind = str(row.get("kind") or "").upper()
                mint = str(row.get("mint") or "")
                received_ns = integer(row.get("received_ns"))
                if received_ns > 0:
                    minimum_ns = min(minimum_ns, received_ns)
                    maximum_ns = max(maximum_ns, received_ns)
                if kind != "CREATE" or not mint:
                    continue
                raw = row.get("raw") if isinstance(row.get("raw"), Mapping) else {}
                launches[mint] = {
                    "mint": mint,
                    "creator": str(row.get("creator") or raw.get("creator") or row.get("trader") or ""),
                    "create_ns": received_ns,
                    "create_slot": integer(row.get("slot")),
                    "create_signature": str(row.get("signature") or ""),
                    "bonding_curve": str(raw.get("bonding_curve") or ""),
                    "metadata_uri": str(raw.get("uri") or ""),
                    "token_program": str(raw.get("token_program") or ""),
                    "mayhem": bool(raw.get("is_mayhem_mode")),
                    "cashback": bool(raw.get("is_cashback_enabled")),
                    "captured_success": mint in selected,
                }
    if minimum_ns == 2**63 - 1:
        raise ValueError("capture contains no timestamps")
    return launches, minimum_ns, maximum_ns


class RpcPool:
    def __init__(self, urls: Sequence[str], *, timeout: float, concurrency: int) -> None:
        self.urls = tuple(dict.fromkeys(str(value).strip() for value in urls if str(value).strip()))
        if not self.urls:
            raise ValueError("no RPC URLs configured")
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.sem = asyncio.Semaphore(concurrency)
        self.session: aiohttp.ClientSession | None = None
        self.request_id = 0
        self.cursor = 0
        self.errors: list[str] = []

    async def __aenter__(self) -> "RpcPool":
        connector = aiohttp.TCPConnector(limit=48, ttl_dns_cache=600, keepalive_timeout=45)
        self.session = aiohttp.ClientSession(timeout=self.timeout, connector=connector)
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self.session is not None:
            await self.session.close()

    async def call(self, method: str, params: list[Any], *, retries: int = 4) -> Any:
        assert self.session is not None
        async with self.sem:
            last: Exception | None = None
            attempts = max(1, retries) * len(self.urls)
            for offset in range(attempts):
                url = self.urls[(self.cursor + offset) % len(self.urls)]
                self.request_id += 1
                try:
                    async with self.session.post(
                        url,
                        json={"jsonrpc": "2.0", "id": self.request_id, "method": method, "params": params},
                    ) as response:
                        text = await response.text()
                        if response.status == 429 or response.status >= 500:
                            raise RuntimeError(f"HTTP {response.status}")
                        body = json.loads(text)
                        if body.get("error"):
                            raise RuntimeError(str(body["error"]))
                        result = body.get("result")
                        if result is None:
                            raise RuntimeError("null result")
                        self.cursor = (self.urls.index(url) + 1) % len(self.urls)
                        return result
                except Exception as exc:
                    last = exc
                    self.errors.append(f"{method}@{safe_url(url)}: {type(exc).__name__}: {exc}")
                    await asyncio.sleep(min(1.2, 0.04 * (offset + 1)))
            raise RuntimeError(f"{method} failed: {last}")


async def fetch_transactions(pool: RpcPool, rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}

    async def one(row: Mapping[str, Any]) -> None:
        signature = str(row.get("signature") or "")
        try:
            payload = await pool.call(
                "getTransaction",
                [signature, {"encoding": "jsonParsed", "commitment": "confirmed", "maxSupportedTransactionVersion": 0}],
            )
            output[signature] = {
                "ok": True,
                "payload": payload,
                "pump_accounts": pump_instructions(payload),
                "transaction_index": transaction_index(payload, integer(row.get("transactionIndex"), -1)),
            }
        except Exception as exc:
            output[signature] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    chunk = 100
    for start in range(0, len(rows), chunk):
        await asyncio.gather(*(one(row) for row in rows[start : start + chunk]))
        print(json.dumps({"fetched": min(len(rows), start + chunk), "target": len(rows)}), flush=True)
    return output


def infer_layout(
    successful: Sequence[Mapping[str, Any]],
    details: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    counters: dict[str, Counter[int]] = defaultdict(Counter)
    samples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in successful:
        signature = str(row.get("signature") or "")
        primary_mint = str(row.get("primary_mint") or "")
        name = buy_name(row)
        detail = details.get(signature) or {}
        if not name or not primary_mint or not detail.get("ok"):
            continue
        for accounts in detail.get("pump_accounts") or []:
            for index, account in enumerate(accounts):
                if account == primary_mint:
                    counters[name][index] += 1
                    samples[name].append({"signature": signature, "mint": primary_mint, "index": index, "account_count": len(accounts)})
    output = {}
    for name, counter in counters.items():
        modal_index, count = counter.most_common(1)[0]
        total = sum(counter.values())
        output[name] = {
            "modal_mint_account_index": modal_index,
            "modal_support": count,
            "observations": total,
            "modal_rate": count / total if total else 0.0,
            "index_counts": {str(index): value for index, value in sorted(counter.items())},
            "samples": samples[name][:20],
        }
    return output


def map_attempts(
    failed: Sequence[Mapping[str, Any]],
    details: Mapping[str, Mapping[str, Any]],
    layout: Mapping[str, Mapping[str, Any]],
    launches: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    captured_mints = set(launches)
    output = []
    for row in failed:
        signature = str(row.get("signature") or "")
        name = buy_name(row)
        detail = details.get(signature) or {}
        mapped_mint = ""
        method = "unresolved"
        account_index = -1
        accounts_used: list[str] = []
        if detail.get("ok"):
            instructions = list(detail.get("pump_accounts") or [])
            expected = integer((layout.get(name) or {}).get("modal_mint_account_index"), -1)
            for accounts in instructions:
                intersections = [account for account in accounts if account in captured_mints]
                if expected >= 0 and expected < len(accounts):
                    candidate = accounts[expected]
                    if candidate in captured_mints:
                        mapped_mint = candidate
                        method = "learned_layout_captured"
                        account_index = expected
                        accounts_used = accounts
                        break
                if len(intersections) == 1:
                    mapped_mint = intersections[0]
                    method = "unique_captured_account"
                    account_index = accounts.index(mapped_mint)
                    accounts_used = accounts
                    break
            if not mapped_mint and expected >= 0:
                for accounts in instructions:
                    if expected < len(accounts):
                        mapped_mint = accounts[expected]
                        method = "learned_layout_outside_capture"
                        account_index = expected
                        accounts_used = accounts
                        break
        launch = dict(launches.get(mapped_mint) or {})
        create_slot = integer(launch.get("create_slot"), -1)
        attempt_slot = integer(row.get("slot"), -1)
        output.append(
            {
                "signature": signature,
                "buy_instruction": name,
                "mapped_mint": mapped_mint,
                "mapping_method": method,
                "mapping_ok": bool(mapped_mint),
                "captured_mint": mapped_mint in captured_mints,
                "captured_success": bool(launch.get("captured_success")),
                "attempt_slot": attempt_slot,
                "attempt_transaction_index": integer(detail.get("transaction_index"), integer(row.get("transactionIndex"), -1)),
                "create_slot": create_slot,
                "slot_gap": attempt_slot - create_slot if create_slot >= 0 and attempt_slot >= 0 else None,
                "account_index": account_index,
                "pump_account_count": len(accounts_used),
                "creator": str(launch.get("creator") or ""),
                "mayhem": bool(launch.get("mayhem")),
                "cashback": bool(launch.get("cashback")),
                "block_time": integer(row.get("blockTime")),
                "priority_fee_sol": integer(row.get("estimated_priority_fee_lamports")) / 1_000_000_000,
                "compute_units": integer(row.get("compute_units_consumed")),
                "shortfall_fraction": shortfall(row),
                "error": row.get("error"),
                "detail_error": detail.get("error"),
            }
        )
    return output


def distribution(rows: Sequence[Mapping[str, Any]], key: str) -> dict[str, Any]:
    values = [finite(row.get(key), float("nan")) for row in rows]
    clean = [value for value in values if math.isfinite(value)]
    return {
        "count": len(clean),
        "median": median(clean),
        "p10": percentile(clean, 0.10),
        "p90": percentile(clean, 0.90),
        "min": min(clean) if clean else None,
        "max": max(clean) if clean else None,
    }


async def main_async(args: argparse.Namespace) -> int:
    launches, minimum_ns, maximum_ns = load_capture(args.pair)
    history = json.loads(args.full_history.read_text(encoding="utf-8"))
    minimum_time = minimum_ns // 1_000_000_000 - 60
    maximum_time = maximum_ns // 1_000_000_000 + 300
    transactions = [
        row
        for row in history.get("transactions") or []
        if row.get("detail_ok")
        and minimum_time <= integer(row.get("blockTime")) <= maximum_time
        and buy_name(row)
    ]
    successful = [row for row in transactions if not row.get("error") and str(row.get("primary_mint") or "") in launches]
    failed = [row for row in transactions if row.get("error")]
    unique_rows = list({str(row.get("signature") or ""): row for row in successful + failed}.values())

    urls = []
    for name in ("E4_PRIMARY_RPC_URL", "HELIUS_RPC_URL"):
        if os.getenv(name, "").strip():
            urls.append(os.getenv(name, "").strip())
    urls.extend(DEFAULT_RPCS)
    async with RpcPool(urls, timeout=args.timeout, concurrency=args.concurrency) as pool:
        details = await fetch_transactions(pool, unique_rows)
        rpc_errors = pool.errors[-500:]

    layout = infer_layout(successful, details)
    mapped = map_attempts(failed, details, layout, launches)
    mapped_captured = [row for row in mapped if row.get("captured_mint")]
    mapped_ignored = [row for row in mapped_captured if not row.get("captured_success")]
    mapped_selected = [row for row in mapped_captured if row.get("captured_success")]
    failed_mints = {str(row.get("mapped_mint") or "") for row in mapped_ignored if row.get("mapped_mint")}
    success_mints = {str(row.get("primary_mint") or "") for row in successful}
    truly_ignored = set(launches) - failed_mints - success_mints

    result = {
        "version": "e4-v12-attempt-mint-forensics-v1",
        "wallet": E4_WALLET,
        "methodology": {
            "capture_launches": len(launches),
            "capture_start_ns": minimum_ns,
            "capture_end_ns": maximum_ns,
            "success_layout_learning": "find the known positive token-balance mint inside each successful Pump instruction account list",
            "failed_mapping": "apply the modal per-instruction mint account index, then require/cross-check captured account intersection",
            "important": "a mapped failed BuyExactSolIn is an E4-selected candidate whose on-chain price protection rejected the fill, not a true ignored launch",
        },
        "coverage": {
            "candidate_transactions": len(transactions),
            "captured_successful_buys": len(successful),
            "failed_buy_attempts": len(failed),
            "transaction_details_resolved": sum(bool((details.get(str(row.get("signature") or "")) or {}).get("ok")) for row in unique_rows),
            "transaction_details_requested": len(unique_rows),
            "mapped_failed_attempts": sum(bool(row.get("mapping_ok")) for row in mapped),
            "mapped_failed_to_captured_mints": len(mapped_captured),
            "mapped_failed_to_captured_ignored": len(mapped_ignored),
            "mapped_failed_to_captured_success": len(mapped_selected),
            "unique_failed_candidate_mints_in_capture": len(failed_mints),
            "successful_mints_in_capture": len(success_mints),
            "truly_ignored_captured_mints": len(truly_ignored),
        },
        "layout": layout,
        "failed_attempts": {
            "mapping_methods": dict(Counter(str(row.get("mapping_method") or "") for row in mapped)),
            "slot_gaps_captured": dict(Counter(str(row.get("slot_gap")) for row in mapped_captured)),
            "mayhem_captured": sum(bool(row.get("mayhem")) for row in mapped_ignored),
            "cashback_captured": sum(bool(row.get("cashback")) for row in mapped_ignored),
            "priority_fee_sol": distribution(mapped, "priority_fee_sol"),
            "shortfall_fraction": distribution(mapped, "shortfall_fraction"),
            "rows": mapped,
        },
        "captured_labels": {
            "successful": sorted(success_mints),
            "failed_attempt_candidate": sorted(failed_mints),
            "truly_ignored": sorted(truly_ignored),
        },
        "rpc_errors": rpc_errors,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({
        "coverage": result["coverage"],
        "layout": layout,
        "mapping_methods": result["failed_attempts"]["mapping_methods"],
        "slot_gaps_captured": result["failed_attempts"]["slot_gaps_captured"],
        "shortfall_fraction": result["failed_attempts"]["shortfall_fraction"],
    }, indent=2, sort_keys=True), flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Map successful and failed E4 Pump buys to exact target mints")
    parser.add_argument("--full-history", type=Path, required=True)
    parser.add_argument("--pair", action="append", default=[], metavar="BATCH:EVENTS")
    parser.add_argument("--concurrency", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.pair:
        parser.error("at least one --pair is required")
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
