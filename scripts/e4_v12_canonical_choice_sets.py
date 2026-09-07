#!/usr/bin/env python3
from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import math
import re
import statistics
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

E4_WALLET = "E4EzXdwf7NNdqM2XGswWaWHfxgucVCo24PTCcrimTKBz"
BUY_KINDS = {"BUY", "PUMPSWAP_BUY"}
SELL_KINDS = {"SELL", "PUMPSWAP_SELL"}
WINDOWS_MS = (100, 250, 500, 750, 1_000, 1_500)
LAMPORTS = 1_000_000_000.0
TOKEN_SCALE = 1_000_000.0
LEFT_RE = re.compile(r"(?:Program log: )?Left: (\d+)")
RIGHT_RE = re.compile(r"(?:Program log: )?Right: (\d+)")
FEATURE_TIMESTAMP_FIELDS = (
    "candidate_create_ns",
    "feature_max_event_ns",
    "reserve_state_ns",
    "social_feature_ns",
    "creator_history_feature_ns",
    "first_buyer_history_feature_ns",
    "whitelist_feature_ns",
)


def integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def finite(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def stable_id(*values: Any) -> str:
    payload = "\x1f".join(str(value) for value in values).encode()
    return "e4v12-" + hashlib.sha256(payload).hexdigest()[:24]


def tx_index(row: Mapping[str, Any]) -> int:
    raw = row.get("raw") if isinstance(row.get("raw"), Mapping) else {}
    for key in ("transaction_index", "transactionIndex", "tx_index", "txIndex"):
        value = row.get(key)
        if value is None:
            value = raw.get(key)
        if value is not None:
            return integer(value, -1)
    return -1


def event_sort_key(row: Mapping[str, Any]) -> tuple[int, int, int, int, str]:
    """Receipt time is authoritative; chain indexes only resolve exact ties."""
    index = tx_index(row)
    return (
        integer(row.get("received_ns")),
        integer(row.get("slot"), -1),
        index if index >= 0 else 1_000_000,
        integer(row.get("event_index"), -1),
        str(row.get("signature") or ""),
    )


def normalize_sol_reserve(value: Any) -> float | None:
    number = finite(value)
    if number <= 0:
        return None
    return number / LAMPORTS if number >= 1_000_000 else number


def normalize_token_reserve(value: Any) -> float | None:
    number = finite(value)
    if number <= 0:
        return None
    return number / TOKEN_SCALE if number >= 10_000_000_000 else number


def buy_tokens(input_sol: float, virtual_sol: float, virtual_tokens: float) -> float:
    if input_sol <= 0 or virtual_sol <= 0 or virtual_tokens <= 0:
        return 0.0
    return input_sol * virtual_tokens / (virtual_sol + input_sol)


def event_identity(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        str(row.get("signature") or ""),
        integer(row.get("event_index"), -1),
        str(row.get("kind") or "").upper(),
        str(row.get("mint") or ""),
        str(row.get("trader") or ""),
    )


def is_buy_instruction(row: Mapping[str, Any]) -> bool:
    logs = "\n".join(str(value) for value in row.get("log_messages") or [])
    return "Instruction: BuyExactSolIn" in logs or "Instruction: Buy" in logs


def instruction_name(row: Mapping[str, Any]) -> str:
    logs = "\n".join(str(value) for value in row.get("log_messages") or [])
    if "Instruction: BuyExactSolIn" in logs:
        return "BuyExactSolIn"
    if "Instruction: Buy" in logs:
        return "Buy"
    return ""


def output_guard_values(row: Mapping[str, Any]) -> tuple[float | None, float | None]:
    left = None
    right = None
    for value in row.get("log_messages") or []:
        text = str(value)
        if match := LEFT_RE.search(text):
            left = integer(match.group(1)) / TOKEN_SCALE
        if match := RIGHT_RE.search(text):
            right = integer(match.group(1)) / TOKEN_SCALE
    return left, right


def content_addressed_uri(uri: str) -> bool:
    text = str(uri or "").lower()
    if text.startswith(("ipfs://", "ar://")):
        return True
    parsed = urlsplit(text)
    return "/ipfs/" in parsed.path or "arweave.net" in parsed.netloc


def twitter_time_ns(status_id: Any) -> int | None:
    value = integer(status_id)
    if value <= 0:
        return None
    return ((value >> 22) + 1_288_834_974_657) * 1_000_000


def compact_json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


@dataclass(frozen=True)
class SourceSpec:
    run_id: str
    artifact_name: str
    split: str
    batch_path: Path
    events_path: Path
    batch_sha256: str
    events_sha256: str


@dataclass
class Launch:
    run_id: str
    artifact_name: str
    split: str
    mint: str
    creator: str
    create_ns: int
    create_slot: int
    create_signature: str
    create_event_id: int
    create_event_index: int
    raw: dict[str, Any]
    create_transaction_index: int = -1


@dataclass
class Decision:
    run_id: str
    artifact_name: str
    split: str
    label: str
    chosen_mint: str
    signature: str
    decision_ns: int
    decision_slot: int
    decision_event_id: int
    decision_event_index: int
    source_transaction_slot: int
    source_transaction_index: int
    decision_time_quality: str
    decision_ns_upper_bound: int
    source_input_sol: float | None = None
    source_received_tokens: float | None = None
    source_curve_output_tokens: float | None = None
    source_output_floor: float | None = None
    estimated_curve_deterioration_bps: float | None = None
    source_priority_fee_sol: float | None = None
    output_guard_rejected: bool = False
    source_transaction_failed: bool = False
    execution_outcome: str = ""
    error: str = ""
    group_id: str = field(init=False)

    def __post_init__(self) -> None:
        self.group_id = stable_id(
            self.label,
            self.run_id,
            self.signature,
            self.chosen_mint,
            self.decision_ns,
        )


@dataclass
class CaptureScan:
    spec: SourceSpec
    launches: dict[str, Launch]
    successful_events: list[dict[str, Any]]
    slot_receipts: dict[int, tuple[int, int]]
    event_count: int
    parse_errors: int
    duplicate_events: int
    minimum_ns: int
    maximum_ns: int


def find_source_file(root: Path, run_id: str, filename: str) -> Path:
    candidates = sorted((root / run_id).rglob(filename))
    if len(candidates) != 1:
        raise ValueError(f"run {run_id}: expected one {filename}, found {len(candidates)}")
    return candidates[0]


def split_by_run(manifest: Mapping[str, Any]) -> dict[str, str]:
    output: dict[str, str] = {}
    for split, run_ids in (manifest.get("splits") or {}).items():
        for run_id in run_ids:
            key = str(run_id)
            if key in output:
                raise ValueError(f"run {key} occurs in more than one split")
            output[key] = str(split)
    return output


def source_specs(manifest: Mapping[str, Any], root: Path) -> list[SourceSpec]:
    splits = split_by_run(manifest)
    output = []
    for item in manifest.get("captures") or []:
        run_id = str(item["run_id"])
        if run_id not in splits:
            raise ValueError(f"run {run_id} has no split")
        output.append(
            SourceSpec(
                run_id=run_id,
                artifact_name=str(item["artifact_name"]),
                split=splits[run_id],
                batch_path=find_source_file(root, run_id, "e4-v12-forward-batch.json"),
                events_path=find_source_file(
                    root, run_id, "e4-v12-forward-batch-live-events.jsonl"
                ),
                batch_sha256=str(item["batch_sha256"]),
                events_sha256=str(item["events_sha256"]),
            )
        )
    if set(splits) != {item.run_id for item in output}:
        raise ValueError("split and capture run IDs differ")
    return output


def verify_source(spec: SourceSpec) -> None:
    if sha256_path(spec.batch_path) != spec.batch_sha256:
        raise ValueError(f"run {spec.run_id}: batch SHA-256 mismatch")
    if sha256_path(spec.events_path) != spec.events_sha256:
        raise ValueError(f"run {spec.run_id}: event SHA-256 mismatch")
    batch = json.loads(spec.batch_path.read_text(encoding="utf-8"))
    capture = batch.get("capture") or {}
    if integer(capture.get("unique_launches")) != 3_000:
        raise ValueError(f"run {spec.run_id}: capture is not exactly 3,000 launches")
    if not bool(capture.get("target_reached")):
        raise ValueError(f"run {spec.run_id}: capture target was not reached")


def verify_artifact(path: Path, metadata: Mapping[str, Any]) -> None:
    if path.stat().st_size != integer(metadata.get("bytes")):
        raise ValueError(f"{path}: byte-size mismatch")
    if sha256_path(path) != str(metadata.get("sha256") or ""):
        raise ValueError(f"{path}: SHA-256 mismatch")


def scan_capture(spec: SourceSpec) -> CaptureScan:
    launches: dict[str, Launch] = {}
    successful: list[dict[str, Any]] = []
    slots: dict[int, list[int]] = defaultdict(list)
    seen: set[tuple[Any, ...]] = set()
    event_count = 0
    parse_errors = 0
    duplicate_events = 0
    minimum_ns = 2**63 - 1
    maximum_ns = 0
    with spec.events_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                parse_errors += 1
                continue
            if not isinstance(row, Mapping):
                parse_errors += 1
                continue
            event_count += 1
            identity = event_identity(row)
            if identity in seen:
                duplicate_events += 1
                continue
            seen.add(identity)
            timestamp = integer(row.get("received_ns"))
            slot = integer(row.get("slot"), -1)
            if timestamp > 0:
                minimum_ns = min(minimum_ns, timestamp)
                maximum_ns = max(maximum_ns, timestamp)
                if slot >= 0:
                    slots[slot].append(timestamp)
            mint = str(row.get("mint") or "")
            kind = str(row.get("kind") or "").upper()
            if kind == "CREATE" and mint and mint not in launches:
                raw = row.get("raw") if isinstance(row.get("raw"), Mapping) else {}
                launches[mint] = Launch(
                    run_id=spec.run_id,
                    artifact_name=spec.artifact_name,
                    split=spec.split,
                    mint=mint,
                    creator=str(
                        row.get("creator") or raw.get("creator") or row.get("trader") or ""
                    ),
                    create_ns=timestamp,
                    create_slot=slot,
                    create_signature=str(row.get("signature") or ""),
                    create_event_id=integer(row.get("event_id"), -1),
                    create_event_index=integer(row.get("event_index"), -1),
                    raw=dict(raw),
                )
            if kind in BUY_KINDS and str(row.get("trader") or "") == E4_WALLET and mint:
                successful.append(dict(row))
    if minimum_ns == 2**63 - 1:
        minimum_ns = 0
    return CaptureScan(
        spec=spec,
        launches=launches,
        successful_events=successful,
        slot_receipts={slot: (min(values), max(values)) for slot, values in slots.items()},
        event_count=event_count,
        parse_errors=parse_errors,
        duplicate_events=duplicate_events,
        minimum_ns=minimum_ns,
        maximum_ns=maximum_ns,
    )


def pre_transaction_indexes(bundle: Mapping[str, Any]) -> dict[str, int]:
    block_rows = {
        str(row.get("mint") or ""): row
        for row in (bundle.get("block_order") or {}).get("rows") or []
    }
    output: dict[str, int] = {}
    for entry in bundle.get("entries") or []:
        mint = str(entry.get("mint") or "")
        block = block_rows.get(mint) or {}
        signatures = [str(value) for value in entry.get("buy_signatures") or []]
        indexes = [integer(value, -1) for value in block.get("pre_indices") or []]
        if len(signatures) == len(indexes):
            output.update(
                {signature: index for signature, index in zip(signatures, indexes) if index >= 0}
            )
        signature = str(entry.get("e4_entry_signature") or "")
        index = integer(block.get("e4_index"), -1)
        if signature and index >= 0:
            output[signature] = index
    return output


def wallet_transactions(wallet: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(row.get("signature") or ""): dict(row)
        for row in wallet.get("transactions") or []
        if str(row.get("signature") or "")
    }


def annotate_launch_indexes(
    scans: Sequence[CaptureScan], signature_indexes: Mapping[str, int]
) -> None:
    for scan in scans:
        for launch in scan.launches.values():
            launch.create_transaction_index = integer(
                signature_indexes.get(launch.create_signature), -1
            )


def successful_decisions(
    scans: Sequence[CaptureScan],
    wallet_by_signature: Mapping[str, Mapping[str, Any]],
) -> list[Decision]:
    output: list[Decision] = []
    for scan in scans:
        for event in scan.successful_events:
            mint = str(event.get("mint") or "")
            if mint not in scan.launches:
                continue
            signature = str(event.get("signature") or "")
            transaction = wallet_by_signature.get(signature) or {}
            decision_ns = integer(event.get("received_ns"))
            output.append(
                Decision(
                    run_id=scan.spec.run_id,
                    artifact_name=scan.spec.artifact_name,
                    split=scan.spec.split,
                    label="SELECTED_LANDED",
                    chosen_mint=mint,
                    signature=signature,
                    decision_ns=decision_ns,
                    decision_slot=integer(event.get("slot"), -1),
                    decision_event_id=integer(event.get("event_id"), -1),
                    decision_event_index=integer(event.get("event_index"), -1),
                    source_transaction_slot=integer(transaction.get("slot"), -1),
                    source_transaction_index=integer(transaction.get("transactionIndex"), -1),
                    decision_time_quality="observed_landed_event_received_ns",
                    decision_ns_upper_bound=decision_ns,
                    source_input_sol=finite(event.get("sol_amount")),
                    source_received_tokens=finite(event.get("token_amount")),
                    source_priority_fee_sol=(
                        integer(transaction.get("estimated_priority_fee_lamports")) / LAMPORTS
                        if transaction
                        else None
                    ),
                    execution_outcome="LANDED_SUCCESSFULLY",
                )
            )
    return output


def failed_decisions(
    attempts: Sequence[Mapping[str, Any]],
    launch_by_mint: Mapping[str, Launch],
    scan_by_run: Mapping[str, CaptureScan],
    wallet_by_signature: Mapping[str, Mapping[str, Any]],
) -> tuple[list[Decision], list[dict[str, Any]]]:
    output: list[Decision] = []
    exclusions: list[dict[str, Any]] = []
    for row in attempts:
        signature = str(row.get("signature") or "")
        mint = str(row.get("mapped_mint") or "")
        launch = launch_by_mint.get(mint)
        if not bool(row.get("mapping_ok")) or not mint:
            exclusions.append(
                {
                    "selection_kind": "FAILED_ATTEMPT",
                    "signature": signature,
                    "mint": mint,
                    "reason_code": "FAILED_ATTEMPT_MINT_UNRESOLVED",
                }
            )
            continue
        if launch is None or not bool(row.get("captured_mint")):
            exclusions.append(
                {
                    "selection_kind": "FAILED_ATTEMPT",
                    "signature": signature,
                    "mint": mint,
                    "reason_code": "MAPPED_MINT_OUTSIDE_AUTHORITATIVE_CAPTURE",
                }
            )
            continue
        transaction = wallet_by_signature.get(signature) or {}
        calculated, floor = output_guard_values(transaction)
        logs = "\n".join(str(value) for value in transaction.get("log_messages") or [])
        guard_rejected = "BuySlippageBelowMinTokensOut" in logs
        attempt_slot = integer(row.get("attempt_slot"), -1)
        scan = scan_by_run[launch.run_id]
        receipt = scan.slot_receipts.get(attempt_slot)
        fallback_upper = (integer(row.get("block_time")) + 1) * 1_000_000_000 - 1
        upper = max(launch.create_ns, receipt[1] if receipt else fallback_upper)
        deterioration = row.get("shortfall_fraction")
        output.append(
            Decision(
                run_id=launch.run_id,
                artifact_name=launch.artifact_name,
                split=launch.split,
                label="SELECTED_FILL_REJECTED",
                chosen_mint=mint,
                signature=signature,
                decision_ns=launch.create_ns,
                decision_slot=launch.create_slot,
                decision_event_id=launch.create_event_id,
                decision_event_index=launch.create_event_index,
                source_transaction_slot=attempt_slot,
                source_transaction_index=integer(row.get("attempt_transaction_index"), -1),
                decision_time_quality="chosen_launch_received_ns_lower_bound",
                decision_ns_upper_bound=upper,
                source_curve_output_tokens=calculated,
                source_output_floor=floor,
                estimated_curve_deterioration_bps=(
                    finite(deterioration) * 10_000 if deterioration is not None else None
                ),
                source_priority_fee_sol=finite(row.get("priority_fee_sol")),
                output_guard_rejected=guard_rejected,
                source_transaction_failed=True,
                execution_outcome=(
                    "OUTPUT_GUARD_REJECTED" if guard_rejected else "SOURCE_TRANSACTION_FAILED"
                ),
                error=compact_json(row.get("error")),
            )
        )
    return output, exclusions


def launch_visible(launch: Launch, decision: Decision) -> bool:
    if launch.mint == decision.chosen_mint:
        return True
    if launch.create_ns < decision.decision_ns:
        return True
    if launch.create_ns > decision.decision_ns:
        return False
    return bool(
        launch.create_slot == decision.source_transaction_slot
        and launch.create_transaction_index >= 0
        and decision.source_transaction_index >= 0
        and launch.create_transaction_index < decision.source_transaction_index
    )


def candidate_launches(
    scan: CaptureScan, decision: Decision, window_ms: int = 1_500
) -> list[Launch]:
    ordered = sorted(scan.launches.values(), key=lambda row: (row.create_ns, row.mint))
    timestamps = [row.create_ns for row in ordered]
    start = bisect.bisect_left(timestamps, decision.decision_ns - window_ms * 1_000_000)
    stop = bisect.bisect_right(timestamps, decision.decision_ns)
    candidates = [launch for launch in ordered[start:stop] if launch_visible(launch, decision)]
    if decision.chosen_mint not in {row.mint for row in candidates}:
        candidates.append(scan.launches[decision.chosen_mint])
    return sorted(candidates, key=lambda row: (row.create_ns, row.mint))


def event_known_at_decision(event: Mapping[str, Any], decision: Decision, launch: Launch) -> bool:
    timestamp = integer(event.get("received_ns"))
    if timestamp < decision.decision_ns:
        return True
    if timestamp > decision.decision_ns:
        return False
    event_index = integer(event.get("event_index"), -1)
    signature = str(event.get("signature") or "")
    index = tx_index(event)
    if (
        integer(event.get("slot"), -1) == decision.source_transaction_slot
        and index >= 0
        and decision.source_transaction_index >= 0
    ):
        if index != decision.source_transaction_index:
            return index < decision.source_transaction_index
        return event_index < decision.decision_event_index
    if signature == decision.signature:
        return event_index < decision.decision_event_index
    return bool(
        decision.label == "SELECTED_FILL_REJECTED"
        and launch.mint == decision.chosen_mint
        and signature == launch.create_signature
    )


def load_relevant_events(
    scan: CaptureScan,
    candidate_mints: set[str],
    maximum_decision_by_mint: Mapping[str, int],
    signature_indexes: Mapping[str, int],
) -> dict[str, list[dict[str, Any]]]:
    output: dict[str, list[dict[str, Any]]] = defaultdict(list)
    seen: set[tuple[Any, ...]] = set()
    with scan.spec.events_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            mint = str(row.get("mint") or "")
            if mint not in candidate_mints:
                continue
            identity = event_identity(row)
            if identity in seen:
                continue
            seen.add(identity)
            if integer(row.get("received_ns")) > integer(maximum_decision_by_mint.get(mint)):
                continue
            record = dict(row)
            signature = str(record.get("signature") or "")
            if signature in signature_indexes:
                record["transaction_index"] = signature_indexes[signature]
            output[mint].append(record)
    for rows in output.values():
        rows.sort(key=event_sort_key)
    return dict(output)


def reserve_values(row: Mapping[str, Any]) -> tuple[float | None, float | None, float | None]:
    raw = row.get("raw") if isinstance(row.get("raw"), Mapping) else {}
    return (
        normalize_sol_reserve(raw.get("virtual_sol_reserves")),
        normalize_token_reserve(raw.get("virtual_token_reserves")),
        normalize_token_reserve(raw.get("real_token_reserves")),
    )


def social_indexes(
    social: Mapping[str, Any], global_attempt: Mapping[str, Any]
) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    for row in global_attempt.get("rows") or []:
        mint = str(row.get("mint") or "")
        if mint:
            output[mint] = dict(row)
    for row in social.get("rows") or []:
        mint = str(row.get("mint") or "")
        if mint:
            output.setdefault(mint, {}).update(dict(row))
    return output


def whitelist_tier(history: Mapping[str, Any]) -> str:
    trades = integer(history.get("trades"))
    wins = integer(history.get("wins"))
    rate = finite(history.get("win_rate"))
    if trades >= 5 and wins >= 4 and rate >= 0.80:
        return "ELITE"
    if trades >= 3 and wins >= 2 and rate >= 0.75:
        return "PROVEN"
    return "NONE"


def causal_social(launch: Launch, decision: Decision, social: Mapping[str, Any]) -> dict[str, Any]:
    uri = str(launch.raw.get("uri") or social.get("uri") or "")
    immutable = content_addressed_uri(uri)
    twitter = str(social.get("twitter") or "")
    handle = str(social.get("twitter_handle") or "")
    status_id = str(social.get("twitter_status_id") or "")
    post_ns = twitter_time_ns(status_id)
    usable = bool(immutable and twitter and (post_ns is None or post_ns <= decision.decision_ns))
    return {
        "metadata_uri": uri,
        "metadata_uri_host": urlsplit(uri).netloc.lower(),
        "metadata_content_addressed": immutable,
        "metadata_observation_available": bool(social),
        "social_account": handle if usable else "",
        "social_post_timestamp_ns": post_ns if usable else None,
        "social_post_existed_before_launch": (
            post_ns <= launch.create_ns if usable and post_ns is not None else None
        ),
        "social_available_before_decision": usable,
        "website_available_before_decision": bool(usable and social.get("website")),
        "social_feature_ns": launch.create_ns if usable else None,
        "social_evidence_policy": (
            "content_addressed_metadata_only" if usable else "not_causally_usable"
        ),
    }


def snapshot_features(
    launch: Launch,
    decision: Decision,
    events: Sequence[Mapping[str, Any]],
    social: Mapping[str, Any],
    prior_registry: Mapping[str, Any],
    prior_registry_ns: int,
) -> dict[str, Any]:
    known = [dict(row) for row in events if event_known_at_decision(row, decision, launch)]
    known.sort(key=event_sort_key)
    buys = [row for row in known if str(row.get("kind") or "").upper() in BUY_KINDS]
    sells = [row for row in known if str(row.get("kind") or "").upper() in SELL_KINDS]
    public_buys = [
        row for row in buys if str(row.get("trader") or "") not in {launch.creator, E4_WALLET}
    ]
    creator_buys = [row for row in buys if str(row.get("trader") or "") == launch.creator]
    first_buyers = list(dict.fromkeys(str(row.get("trader") or "") for row in public_buys))
    signatures = Counter(str(row.get("signature") or "") for row in buys)
    state = next(
        (
            row
            for row in reversed(known)
            if reserve_values(row)[0] is not None and reserve_values(row)[1] is not None
        ),
        None,
    )
    virtual_sol, virtual_tokens, real_tokens = (
        reserve_values(state) if state is not None else (None, None, None)
    )
    quote = (
        buy_tokens(0.1, virtual_sol, virtual_tokens)
        if virtual_sol is not None and virtual_tokens is not None
        else None
    )
    if quote is not None and real_tokens is not None:
        quote = min(quote, real_tokens)
    price_rows = [row for row in known if finite(row.get("price_sol")) > 0]
    initial_price = finite(price_rows[0].get("price_sol")) if price_rows else 0.0
    latest_price = finite(price_rows[-1].get("price_sol")) if price_rows else 0.0
    latest_fdv = finite(price_rows[-1].get("fdv_usd")) if price_rows else 0.0
    history_available = prior_registry_ns > 0 and prior_registry_ns <= decision.decision_ns
    history = (
        (prior_registry.get("creators") or {}).get(launch.creator) or {}
        if history_available
        else {}
    )
    raw = launch.raw
    output = {
        "creator_address": launch.creator,
        "creator_seed_sol": sum(finite(row.get("sol_amount")) for row in creator_buys),
        "public_buy_sol": sum(finite(row.get("sol_amount")) for row in public_buys),
        "buy_count": len(buys),
        "sell_count": len(sells),
        "unique_buyers": len(first_buyers),
        "first_buyer_identities_json": compact_json(first_buyers[:8]),
        "first_buyer_cluster": "|".join(first_buyers[:3]),
        "same_slot_buyer_count": len(
            {
                str(row.get("trader") or "")
                for row in public_buys
                if integer(row.get("slot"), -1) == launch.create_slot
            }
        ),
        "same_transaction_buyer_count": len(
            {
                str(row.get("trader") or "")
                for row in public_buys
                if str(row.get("signature") or "") == launch.create_signature
            }
        ),
        "transaction_count": len({str(row.get("signature") or "") for row in known}),
        "max_buys_in_one_transaction": max(signatures.values(), default=0),
        "create_transaction_event_count": sum(
            str(row.get("signature") or "") == launch.create_signature for row in known
        ),
        "create_transaction_buy_count": sum(
            str(row.get("signature") or "") == launch.create_signature for row in buys
        ),
        "bundle_shape": (
            f"create_events={sum(str(row.get('signature') or '') == launch.create_signature for row in known)};"
            f"create_buys={sum(str(row.get('signature') or '') == launch.create_signature for row in buys)}"
        ),
        "reserve_state_ns": integer(state.get("received_ns")) if state else None,
        "virtual_sol_reserve": virtual_sol,
        "virtual_token_reserve": virtual_tokens,
        "real_token_reserve": real_tokens,
        "executable_token_output_0_1_sol": quote,
        "price_sol": latest_price or None,
        "fdv_usd": latest_fdv or None,
        "price_multiple_from_create": (
            latest_price / initial_price if initial_price > 0 and latest_price > 0 else None
        ),
        "mayhem_mode": bool(raw.get("is_mayhem_mode")),
        "cashback_enabled": bool(raw.get("is_cashback_enabled")),
        "token_program": str(raw.get("token_program") or ""),
        "metadata_name": str(raw.get("name") or ""),
        "metadata_symbol": str(raw.get("symbol") or ""),
        "feature_max_event_ns": max(
            (integer(row.get("received_ns")) for row in known), default=None
        ),
        "creator_history_available": history_available,
        "creator_history_trades": integer(history.get("trades")) if history else 0,
        "creator_history_wins": integer(history.get("wins")) if history else 0,
        "creator_history_losses": integer(history.get("losses")) if history else 0,
        "creator_history_win_rate": finite(history.get("win_rate")) if history else 0.0,
        "creator_history_feature_ns": prior_registry_ns if history_available else None,
        "prior_whitelist_membership": (
            whitelist_tier(history) != "NONE" if history_available else None
        ),
        "prior_whitelist_tier": whitelist_tier(history) if history_available else "UNKNOWN",
        "whitelist_feature_ns": prior_registry_ns if history_available else None,
        "funding_wallet": "",
        "funding_wallet_history_available": False,
    }
    for window in WINDOWS_MS:
        cutoff = launch.create_ns + window * 1_000_000
        window_buys = [row for row in buys if integer(row.get("received_ns")) <= cutoff]
        output[f"buy_count_first_{window}ms"] = len(window_buys)
        output[f"buy_sol_first_{window}ms"] = sum(
            finite(row.get("sol_amount")) for row in window_buys
        )
        output[f"unique_buyers_first_{window}ms"] = len(
            {
                str(row.get("trader") or "")
                for row in window_buys
                if str(row.get("trader") or "") not in {launch.creator, E4_WALLET}
            }
        )
    output.update(causal_social(launch, decision, social))
    return output


def dense_ranks(
    rows: Sequence[dict[str, Any]], field_name: str, *, descending: bool
) -> dict[str, int | None]:
    values = [
        finite(row.get(field_name), float("nan")) for row in rows if row.get(field_name) is not None
    ]
    clean = sorted({value for value in values if math.isfinite(value)}, reverse=descending)
    positions = {value: index + 1 for index, value in enumerate(clean)}
    output: dict[str, int | None] = {}
    for row in rows:
        value = finite(row.get(field_name), float("nan"))
        output[str(row["candidate_mint"])] = positions.get(value)
    return output


def add_relative_ranks(rows: list[dict[str, Any]]) -> None:
    scopes: list[tuple[str, list[dict[str, Any]]]] = [("1500ms", rows)]
    scopes.append(("same_slot", [row for row in rows if row["same_slot_alternative"]]))
    for window in WINDOWS_MS[:-1]:
        scopes.append(
            (
                f"{window}ms",
                [row for row in rows if row[f"within_{window}ms"]],
            )
        )
    rank_fields = (
        ("creator_seed_sol", True, "creator_seed_rank"),
        ("fdv_usd", False, "fdv_rank"),
        ("unique_buyers", True, "unique_buyers_rank"),
        ("buy_sol_first_500ms", True, "launch_velocity_rank"),
        ("executable_token_output_0_1_sol", True, "executable_output_rank"),
    )
    for scope_name, scope_rows in scopes:
        for field_name, descending, output_name in rank_fields:
            ranks = dense_ranks(scope_rows, field_name, descending=descending)
            for row in rows:
                row[f"{output_name}_{scope_name}"] = ranks.get(row["candidate_mint"])


def reproduce_observed_buy(event: Mapping[str, Any]) -> float | None:
    virtual_sol, virtual_tokens, _ = reserve_values(event)
    source_sol = finite(event.get("sol_amount"))
    source_tokens = finite(event.get("token_amount"))
    if virtual_sol is None or virtual_tokens is None or source_sol <= 0 or source_tokens <= 0:
        return None
    pre_sol = virtual_sol - source_sol
    pre_tokens = virtual_tokens + source_tokens
    if pre_sol <= 0 or pre_tokens <= 0:
        return None
    reconstructed = buy_tokens(source_sol, pre_sol, pre_tokens)
    return abs(reconstructed / source_tokens - 1.0) * 10_000


def add_history_features(groups: list[list[dict[str, Any]]]) -> None:
    creator_selected: Counter[str] = Counter()
    buyer_selected: Counter[str] = Counter()
    pair_selected: Counter[tuple[str, str]] = Counter()
    cluster_selected: Counter[str] = Counter()
    by_time: dict[int, list[list[dict[str, Any]]]] = defaultdict(list)
    for group in groups:
        by_time[integer(group[0]["decision_ns"])].append(group)
    for timestamp in sorted(by_time):
        pending: list[tuple[str, list[str], str]] = []
        for group in sorted(by_time[timestamp], key=lambda rows: rows[0]["decision_group_id"]):
            for row in group:
                creator = str(row.get("creator_address") or "")
                buyers = json.loads(str(row.get("first_buyer_identities_json") or "[]"))
                cluster = str(row.get("first_buyer_cluster") or "")
                row["prior_creator_e4_selection_count"] = creator_selected[creator]
                row["prior_first_buyer_e4_overlap_count"] = sum(
                    buyer_selected[buyer] > 0 for buyer in buyers
                )
                row["prior_first_buyer_e4_selection_weight"] = sum(
                    buyer_selected[buyer] for buyer in buyers
                )
                row["prior_creator_buyer_pair_selection_max"] = max(
                    (pair_selected[(creator, buyer)] for buyer in buyers), default=0
                )
                row["prior_buyer_cluster_e4_selection_count"] = (
                    cluster_selected[cluster] if cluster else 0
                )
                row["first_buyer_history_feature_ns"] = timestamp
            chosen = next(row for row in group if row["selected_by_e4"])
            pending.append(
                (
                    str(chosen.get("creator_address") or ""),
                    json.loads(str(chosen.get("first_buyer_identities_json") or "[]")),
                    str(chosen.get("first_buyer_cluster") or ""),
                )
            )
        for creator, buyers, cluster in pending:
            if creator:
                creator_selected[creator] += 1
            for buyer in buyers:
                buyer_selected[buyer] += 1
                if creator:
                    pair_selected[(creator, buyer)] += 1
            if cluster:
                cluster_selected[cluster] += 1


def build_rows(
    scans: Sequence[CaptureScan],
    decisions: Sequence[Decision],
    signature_indexes: Mapping[str, int],
    social_by_mint: Mapping[str, Mapping[str, Any]],
    prior_registry: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    scan_by_run = {scan.spec.run_id: scan for scan in scans}
    decisions_by_run: dict[str, list[Decision]] = defaultdict(list)
    candidates_by_group: dict[str, list[Launch]] = {}
    candidate_mints_by_run: dict[str, set[str]] = defaultdict(set)
    maximum_decision_by_run_mint: dict[str, dict[str, int]] = defaultdict(dict)
    ambiguous_same_receipt = 0
    for decision in decisions:
        scan = scan_by_run[decision.run_id]
        candidates = candidate_launches(scan, decision)
        candidates_by_group[decision.group_id] = candidates
        decisions_by_run[decision.run_id].append(decision)
        tied = [
            launch
            for launch in scan.launches.values()
            if launch.create_ns == decision.decision_ns
            and launch.mint != decision.chosen_mint
            and not launch_visible(launch, decision)
        ]
        ambiguous_same_receipt += len(tied)
        for launch in candidates:
            candidate_mints_by_run[decision.run_id].add(launch.mint)
            previous = maximum_decision_by_run_mint[decision.run_id].get(launch.mint, 0)
            maximum_decision_by_run_mint[decision.run_id][launch.mint] = max(
                previous, decision.decision_ns
            )

    prior_registry_ns = integer(prior_registry.get("first_capture_epoch")) * 1_000_000_000
    groups: list[list[dict[str, Any]]] = []
    decision_by_id = {decision.group_id: decision for decision in decisions}
    successful_event_by_signature = {
        str(row.get("signature") or ""): row for scan in scans for row in scan.successful_events
    }
    for scan in scans:
        relevant = load_relevant_events(
            scan,
            candidate_mints_by_run[scan.spec.run_id],
            maximum_decision_by_run_mint[scan.spec.run_id],
            signature_indexes,
        )
        for decision in sorted(
            decisions_by_run[scan.spec.run_id],
            key=lambda item: (
                item.decision_ns,
                item.source_transaction_index if item.source_transaction_index >= 0 else 1_000_000,
                item.signature,
            ),
        ):
            group_rows: list[dict[str, Any]] = []
            candidates = candidates_by_group[decision.group_id]
            for launch in candidates:
                age_ms = max(0.0, (decision.decision_ns - launch.create_ns) / 1_000_000)
                selected = launch.mint == decision.chosen_mint
                features = snapshot_features(
                    launch,
                    decision,
                    relevant.get(launch.mint, []),
                    social_by_mint.get(launch.mint) or {},
                    prior_registry,
                    prior_registry_ns,
                )
                row = {
                    "schema_version": "e4-v12-canonical-choice-set-v1",
                    "decision_group_id": decision.group_id,
                    "source_run_id": decision.run_id,
                    "source_artifact_name": decision.artifact_name,
                    "source_batch_filename": scan.spec.batch_path.name,
                    "source_batch_sha256": scan.spec.batch_sha256,
                    "source_events_filename": scan.spec.events_path.name,
                    "source_events_sha256": scan.spec.events_sha256,
                    "split": decision.split,
                    "label": (
                        decision.label
                        if selected
                        else str(launch.raw.get("_global_label") or "TRUE_IGNORE")
                    ),
                    "selected_by_e4": selected,
                    "launch_ever_selected_by_e4": str(
                        launch.raw.get("_global_label") or "TRUE_IGNORE"
                    )
                    != "TRUE_IGNORE",
                    "landed_successfully": bool(selected and decision.label == "SELECTED_LANDED"),
                    "output_guard_rejected": bool(selected and decision.output_guard_rejected),
                    "source_transaction_failed": bool(
                        selected and decision.source_transaction_failed
                    ),
                    "execution_outcome": (
                        decision.execution_outcome if selected else "NOT_SELECTED_IN_GROUP"
                    ),
                    "chosen_mint": decision.chosen_mint,
                    "candidate_mint": launch.mint,
                    "decision_ns": decision.decision_ns,
                    "decision_ns_upper_bound": decision.decision_ns_upper_bound,
                    "decision_time_quality": decision.decision_time_quality,
                    "decision_slot": decision.decision_slot,
                    "decision_event_id": decision.decision_event_id,
                    "decision_event_index": decision.decision_event_index,
                    "decision_signature": decision.signature,
                    "source_transaction_slot": decision.source_transaction_slot,
                    "source_transaction_index": decision.source_transaction_index,
                    "candidate_create_ns": launch.create_ns,
                    "candidate_create_slot": launch.create_slot,
                    "candidate_create_transaction_index": (
                        launch.create_transaction_index
                        if launch.create_transaction_index >= 0
                        else None
                    ),
                    "candidate_create_event_id": launch.create_event_id,
                    "candidate_create_event_index": launch.create_event_index,
                    "candidate_create_signature": launch.create_signature,
                    "candidate_age_ms": age_ms,
                    "same_slot_alternative": launch.create_slot == decision.source_transaction_slot,
                    "source_input_sol": decision.source_input_sol if selected else None,
                    "source_received_tokens": (
                        decision.source_received_tokens if selected else None
                    ),
                    "source_curve_output_tokens": (
                        decision.source_curve_output_tokens if selected else None
                    ),
                    "source_output_floor": decision.source_output_floor if selected else None,
                    "estimated_curve_deterioration_bps": (
                        decision.estimated_curve_deterioration_bps if selected else None
                    ),
                    "source_priority_fee_sol": (
                        decision.source_priority_fee_sol if selected else None
                    ),
                    "source_error": decision.error if selected else "",
                    **features,
                }
                for window in WINDOWS_MS:
                    row[f"within_{window}ms"] = bool(age_ms <= window)
                group_rows.append(row)
            for row in group_rows:
                row["decision_group_candidate_count"] = len(group_rows)
                row["decision_group_alternative_count"] = len(group_rows) - 1
                row["same_slot_candidate_count"] = sum(
                    bool(value["same_slot_alternative"]) for value in group_rows
                )
                for window in WINDOWS_MS:
                    row[f"candidate_count_within_{window}ms"] = sum(
                        bool(value[f"within_{window}ms"]) for value in group_rows
                    )
            add_relative_ranks(group_rows)
            if decision.label == "SELECTED_LANDED":
                error = reproduce_observed_buy(
                    successful_event_by_signature.get(decision.signature) or {}
                )
                for row in group_rows:
                    row["reserve_reproduction_error_bps"] = error if row["selected_by_e4"] else None
            else:
                for row in group_rows:
                    row["reserve_reproduction_error_bps"] = None
            groups.append(group_rows)

    add_history_features(groups)
    rows = [row for group in groups for row in group]
    rows.sort(
        key=lambda row: (
            integer(row["decision_ns"]),
            str(row["decision_group_id"]),
            0 if row["selected_by_e4"] else 1,
            str(row["candidate_mint"]),
        )
    )
    if set(decision_by_id) != {str(row["decision_group_id"]) for row in rows}:
        raise ValueError("one or more decision groups produced no rows")
    return rows, {"same_receipt_unordered_alternatives": ambiguous_same_receipt}


def assign_global_labels(
    scans: Sequence[CaptureScan], labels: Mapping[str, Sequence[str]]
) -> tuple[set[str], set[str], set[str]]:
    successful = {str(value) for value in labels.get("successful") or []}
    failed = {str(value) for value in labels.get("failed_attempt_candidate") or []}
    ignored = {str(value) for value in labels.get("truly_ignored") or []}
    if successful & failed or successful & ignored or failed & ignored:
        raise ValueError("authoritative launch labels overlap")
    launches = {mint for scan in scans for mint in scan.launches}
    if successful | failed | ignored != launches:
        raise ValueError("authoritative labels do not partition captured launches")
    for scan in scans:
        for mint, launch in scan.launches.items():
            launch.raw["_global_label"] = (
                "SELECTED_LANDED"
                if mint in successful
                else "SELECTED_FILL_REJECTED"
                if mint in failed
                else "TRUE_IGNORE"
            )
    return successful, failed, ignored


def decision_exclusions(
    wallet: Mapping[str, Any],
    included: Sequence[Decision],
    failed_forensics_signatures: set[str],
    capture_start_ns: int,
    capture_end_ns: int,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    included_signatures = {item.signature for item in included}
    exclusions: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    capture_start_second = capture_start_ns // 1_000_000_000
    capture_end_second = capture_end_ns // 1_000_000_000
    for row in wallet.get("transactions") or []:
        if not row.get("detail_ok") or not is_buy_instruction(row):
            continue
        signature = str(row.get("signature") or "")
        if signature in included_signatures:
            counts["included"] += 1
            continue
        failed = bool(row.get("error"))
        block_time = integer(row.get("blockTime"))
        if block_time < capture_start_second or block_time > capture_end_second:
            reason = "OUTSIDE_AUTHORITATIVE_CAPTURE_INTERVAL"
        elif failed and signature not in failed_forensics_signatures:
            reason = "FAILED_ATTEMPT_NOT_IN_AUTHORITATIVE_MAPPING"
        else:
            reason = "TARGET_LAUNCH_OUTSIDE_AUTHORITATIVE_CAPTURE"
        kind = "FAILED_ATTEMPT" if failed else "SUCCESSFUL_BUY"
        counts[f"discovered_{kind.lower()}"] += 1
        counts[f"excluded_{kind.lower()}"] += 1
        exclusions.append(
            {
                "selection_kind": kind,
                "signature": signature,
                "mint": str(row.get("primary_mint") or ""),
                "slot": integer(row.get("slot"), -1),
                "transaction_index": integer(row.get("transactionIndex"), -1),
                "block_time": integer(row.get("blockTime")),
                "reason_code": reason,
            }
        )
    return exclusions, dict(counts)


def split_ranges(scans: Sequence[CaptureScan]) -> dict[str, dict[str, int]]:
    output: dict[str, dict[str, int]] = {}
    for split in ("train", "validation", "holdout"):
        values = [scan for scan in scans if scan.spec.split == split]
        output[split] = {
            "runs": len(values),
            "minimum_received_ns": min((scan.minimum_ns for scan in values), default=0),
            "maximum_received_ns": max((scan.maximum_ns for scan in values), default=0),
        }
    return output


def integrity_violations(
    rows: Sequence[Mapping[str, Any]],
    decisions: Sequence[Decision],
    exclusions: Sequence[Mapping[str, Any]],
    ranges: Mapping[str, Mapping[str, int]],
) -> list[dict[str, Any]]:
    violations: list[dict[str, Any]] = []
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("decision_group_id") or "")].append(row)
        decision_ns = integer(row.get("decision_ns"))
        for field_name in FEATURE_TIMESTAMP_FIELDS:
            timestamp = row.get(field_name)
            if timestamp is not None and integer(timestamp) > decision_ns:
                violations.append(
                    {
                        "code": "FUTURE_FEATURE_TIMESTAMP",
                        "decision_group_id": row.get("decision_group_id"),
                        "candidate_mint": row.get("candidate_mint"),
                        "field": field_name,
                    }
                )
    for group_id, group in grouped.items():
        mints = [str(row.get("candidate_mint") or "") for row in group]
        chosen = [row for row in group if bool(row.get("selected_by_e4"))]
        if len(mints) != len(set(mints)):
            violations.append({"code": "DUPLICATE_MINT_IN_GROUP", "decision_group_id": group_id})
        if len(chosen) != 1 or chosen[0].get("chosen_mint") not in mints:
            violations.append({"code": "CHOSEN_MINT_MISSING", "decision_group_id": group_id})
        if (
            chosen
            and chosen[0].get("label") == "SELECTED_FILL_REJECTED"
            and not bool(chosen[0].get("selected_by_e4"))
        ):
            violations.append({"code": "FAILED_ATTEMPT_NEGATIVE", "decision_group_id": group_id})
    expected_groups = {item.group_id for item in decisions}
    if expected_groups != set(grouped):
        violations.append({"code": "DECISION_GROUP_COVERAGE_MISMATCH"})
    if any(not str(row.get("reason_code") or "") for row in exclusions):
        violations.append({"code": "EXCLUSION_WITHOUT_REASON"})
    if integer(ranges["train"]["maximum_received_ns"]) >= integer(
        ranges["validation"]["minimum_received_ns"]
    ):
        violations.append({"code": "TRAIN_VALIDATION_TIME_OVERLAP"})
    if integer(ranges["validation"]["maximum_received_ns"]) >= integer(
        ranges["holdout"]["minimum_received_ns"]
    ):
        violations.append({"code": "VALIDATION_HOLDOUT_TIME_OVERLAP"})
    return violations


def write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(compact_json(row) + "\n")


def write_parquet(jsonl_path: Path, parquet_path: Path) -> None:
    try:
        import duckdb
    except ImportError as exc:
        raise RuntimeError("install the research extra to write Parquet") from exc
    parquet_path.parent.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect()
    try:
        relation = connection.read_json(str(jsonl_path), format="newline_delimited")
        relation.write_parquet(str(parquet_path), compression="zstd")
    finally:
        connection.close()


def report_markdown(coverage: Mapping[str, Any]) -> str:
    labels = coverage["launch_labels"]
    decisions = coverage["decision_groups"]
    successful = coverage["successful_buys"]
    failed = coverage["failed_attempts"]
    fields = coverage["field_coverage"]
    integrity = coverage["integrity"]
    gaps = coverage["remaining_evidence_gaps"]
    lines = [
        "# E4 V12 canonical decision choice sets",
        "",
        "## Result",
        "",
        f"Integrity gate: **{integrity['status']}**.",
        "No model was trained and no production code or live-money path was changed.",
        "",
        "## Clean causal sample",
        "",
        f"- Frozen capture windows: {coverage['sources']['capture_runs']}",
        f"- Captured launches: {coverage['sources']['captured_launches']}",
        f"- Decision groups: {decisions['total']}",
        f"- Landed selections: {decisions['selected_landed']}",
        f"- Failed-fill selections: {decisions['selected_fill_rejected']}",
        f"- Choice-set rows: {decisions['rows']}",
        f"- Total alternatives: {decisions['alternatives']}",
        f"- Median alternatives per group: {decisions['median_alternatives']}",
        f"- Candidate range per group: {decisions['minimum_candidates']}–{decisions['maximum_candidates']}",
        f"- True-ignore launches: {labels['true_ignore']}",
        f"- Captured-selection coverage: {coverage['coverage_percentages']['captured_selection_percent']:.2f}%",
        "",
        "## Selection audit",
        "",
        f"- Successful buys discovered in resolved wallet history: {successful['discovered_in_resolved_wallet_history']}",
        f"- Successful buys included: {successful['included']}",
        f"- Successful buys excluded: {successful['excluded']}",
        f"- Failed attempts discovered in resolved wallet history: {failed['discovered_in_resolved_wallet_history']}",
        f"- Failed attempts authoritatively mapped: {failed['authoritative_mapped']}",
        f"- Failed attempts included: {failed['included']}",
        f"- Failed attempts excluded: {failed['excluded']}",
        f"- Audited resolved-wallet selections: {coverage['coverage_percentages']['audited_resolved_wallet_selection_percent']:.2f}%",
        f"- Clean resolved-wallet modelling sample: {coverage['coverage_percentages']['resolved_wallet_selection_clean_sample_percent']:.2f}%",
        "",
        "### Exclusion reasons",
        "",
    ]
    for selection_kind, reason_counts in coverage["exclusion_reason_counts_by_selection"].items():
        for reason, count in reason_counts.items():
            lines.append(f"- `{selection_kind}` / `{reason}`: {count}")
    lines.extend(
        [
            "",
            "## Causal field coverage",
            "",
            f"- Rows with decision `received_ns`: {fields['decision_received_ns']}",
            f"- Decision groups missing transaction index: {fields['missing_decision_transaction_index_groups']}",
            f"- Rows missing candidate transaction index: {fields['missing_candidate_transaction_index_rows']}",
            f"- Rows with reserve state: {fields['reserve_state_rows']}",
            f"- Rows missing reserve state: {fields['missing_reserve_state_rows']}",
            f"- Rows with causal social evidence: {fields['causal_social_rows']}",
            f"- Rows with causal creator history: {fields['causal_creator_history_rows']}",
            f"- Rows with first-buyer history: {fields['first_buyer_history_rows']}",
            f"- Rows with causal funder history: {fields['funder_history_rows']}",
            "",
        ]
    )
    lines.extend(
        [
            "## Integrity checks",
            "",
            f"- Future-leakage violations: {integrity['future_leakage_violations']}",
            f"- Ambiguous labels: {integrity['ambiguous_labels']}",
            f"- Missing chosen rows: {integrity['missing_chosen_rows']}",
            f"- Duplicate source events: {coverage['quality']['duplicate_source_events']}",
            f"- Source parse errors: {coverage['quality']['source_parse_errors']}",
            f"- Reserve reproductions within 500 bps: {coverage['quality']['reserve_reproduction_within_500_bps']}",
            "",
            "## Chronology policy",
            "",
            "`received_ns` is the primary clock. Slot, transaction index, event index, and signature",
            "are used only for exact receipt-time ties. Landed buys use their observed receipt time.",
            "Failed fills have no event receipt in the JSONL, so their group uses the selected launch",
            "receipt as a documented lower bound and retains the failed transaction slot/index separately.",
            "Same-receipt alternatives without transaction-order proof are conservatively omitted.",
            "",
            "## Explicit evidence gaps",
            "",
        ]
    )
    lines.extend(f"- {value}" for value in gaps)
    lines.extend(
        [
            "",
            "## Reproduction",
            "",
            "The source manifest pins every run, artifact name, split, byte size, and SHA-256.",
            "Re-running the builder against those restored artifacts reproduces every stable",
            "`decision_group_id` and JSONL row order. The coverage JSON contains every exclusion",
            "with a machine-readable `reason_code`.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build the causal E4 V12 decision choice-set dataset"
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("research/e4-v12-canonical-choice-source-manifest.json"),
    )
    parser.add_argument("--capture-root", type=Path, default=Path(".tmp-choice-set-source/runs"))
    parser.add_argument(
        "--failed-attempts",
        type=Path,
        default=Path(".tmp-choice-set-source/failed-attempts/e4-v12-attempt-mint-forensics.json"),
    )
    parser.add_argument(
        "--bundle-metadata",
        type=Path,
        default=Path(
            ".tmp-choice-set-source/research/33876511129/e4-v12-bundle-metadata-forensics.json"
        ),
    )
    parser.add_argument(
        "--wallet-history",
        type=Path,
        default=Path(".tmp-choice-set-source/research/33876680669/e4-v12-full-wallet-history.json"),
    )
    parser.add_argument(
        "--social-choice",
        type=Path,
        default=Path(
            ".tmp-choice-set-source/research/33877632753/e4-v12-social-choice-forensics.json"
        ),
    )
    parser.add_argument(
        "--global-attempt",
        type=Path,
        default=Path(
            ".tmp-choice-set-source/research/33889403746/e4-v12-global-attempt-intent.json"
        ),
    )
    parser.add_argument(
        "--causal-prior-registry",
        type=Path,
        default=Path("docs/research/e4-v12-causal-prior-registry.json"),
    )
    parser.add_argument(
        "--jsonl-output",
        type=Path,
        default=Path("artifacts/e4-v12-canonical-choice-sets.jsonl"),
    )
    parser.add_argument(
        "--parquet-output",
        type=Path,
        default=Path("artifacts/e4-v12-canonical-choice-sets.parquet"),
    )
    parser.add_argument(
        "--coverage-output",
        type=Path,
        default=Path("artifacts/e4-v12-choice-set-coverage.json"),
    )
    parser.add_argument(
        "--report-output",
        type=Path,
        default=Path("artifacts/e4-v12-choice-set-report.md"),
    )
    args = parser.parse_args()

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    research = manifest.get("research_artifacts") or {}
    for name, path in (
        ("failed_attempts", args.failed_attempts),
        ("bundle_metadata", args.bundle_metadata),
        ("wallet_history", args.wallet_history),
        ("social_choice", args.social_choice),
        ("global_attempt", args.global_attempt),
        ("causal_prior_registry", args.causal_prior_registry),
    ):
        verify_artifact(path, research[name])

    specs = source_specs(manifest, args.capture_root)
    for spec in specs:
        verify_source(spec)
    scans = [scan_capture(spec) for spec in specs]
    if any(len(scan.launches) != 3_000 for scan in scans):
        raise ValueError("one or more event files do not contain 3,000 unique CREATE events")
    launch_by_mint = {mint: launch for scan in scans for mint, launch in scan.launches.items()}
    if len(launch_by_mint) != sum(len(scan.launches) for scan in scans):
        raise ValueError("a launch mint occurs in more than one capture")

    failed_artifact = json.loads(args.failed_attempts.read_text(encoding="utf-8"))
    bundle = json.loads(args.bundle_metadata.read_text(encoding="utf-8"))
    wallet = json.loads(args.wallet_history.read_text(encoding="utf-8"))
    social = json.loads(args.social_choice.read_text(encoding="utf-8"))
    global_attempt = json.loads(args.global_attempt.read_text(encoding="utf-8"))
    prior_registry = json.loads(args.causal_prior_registry.read_text(encoding="utf-8"))
    successful_labels, failed_labels, ignored_labels = assign_global_labels(
        scans, failed_artifact.get("captured_labels") or {}
    )

    wallet_by_signature = wallet_transactions(wallet)
    signature_indexes = pre_transaction_indexes(bundle)
    signature_indexes.update(
        {
            signature: integer(row.get("transactionIndex"), -1)
            for signature, row in wallet_by_signature.items()
            if integer(row.get("transactionIndex"), -1) >= 0
        }
    )
    annotate_launch_indexes(scans, signature_indexes)
    landed = successful_decisions(scans, wallet_by_signature)
    failed, mapped_exclusions = failed_decisions(
        (failed_artifact.get("failed_attempts") or {}).get("rows") or [],
        launch_by_mint,
        {scan.spec.run_id: scan for scan in scans},
        wallet_by_signature,
    )
    decisions = landed + failed
    if {item.chosen_mint for item in landed} != successful_labels:
        raise ValueError("landed event scan and authoritative success labels differ")
    if {item.chosen_mint for item in failed} != failed_labels:
        raise ValueError("failed decision scan and authoritative failed labels differ")

    rows, chronology = build_rows(
        scans,
        decisions,
        signature_indexes,
        social_indexes(social, global_attempt),
        prior_registry,
    )
    methodology = failed_artifact.get("methodology") or {}
    wallet_exclusions, wallet_counts = decision_exclusions(
        wallet,
        decisions,
        {
            str(row.get("signature") or "")
            for row in (failed_artifact.get("failed_attempts") or {}).get("rows") or []
        },
        integer(methodology.get("capture_start_ns")),
        integer(methodology.get("capture_end_ns")),
    )
    exclusion_keys = {
        (str(row.get("selection_kind")), str(row.get("signature"))) for row in wallet_exclusions
    }
    exclusions = wallet_exclusions + [
        row
        for row in mapped_exclusions
        if (str(row.get("selection_kind")), str(row.get("signature"))) not in exclusion_keys
    ]
    exclusions.sort(
        key=lambda row: (
            str(row.get("selection_kind")),
            str(row.get("reason_code")),
            str(row.get("signature")),
        )
    )
    ranges = split_ranges(scans)
    violations = integrity_violations(rows, decisions, exclusions, ranges)
    reserve_errors = [
        finite(row.get("reserve_reproduction_error_bps"), float("nan"))
        for row in rows
        if bool(row.get("selected_by_e4"))
        and row.get("label") == "SELECTED_LANDED"
        and row.get("reserve_reproduction_error_bps") is not None
    ]
    alternatives = [
        integer(group[0].get("decision_group_alternative_count"))
        for group in (
            list(values)
            for values in (
                [row for row in rows if row["decision_group_id"] == group_id]
                for group_id in sorted({row["decision_group_id"] for row in rows})
            )
        )
        if group
    ]
    discovered_success = sum(
        bool(row.get("detail_ok")) and is_buy_instruction(row) and not bool(row.get("error"))
        for row in wallet.get("transactions") or []
    )
    discovered_failed = sum(
        bool(row.get("detail_ok")) and is_buy_instruction(row) and bool(row.get("error"))
        for row in wallet.get("transactions") or []
    )
    ambiguity = successful_labels & failed_labels
    exclusion_reason_counts = Counter(str(row["reason_code"]) for row in exclusions)
    exclusion_reason_counts_by_selection = {
        selection_kind: dict(
            sorted(
                Counter(
                    str(row["reason_code"])
                    for row in exclusions
                    if row["selection_kind"] == selection_kind
                ).items()
            )
        )
        for selection_kind in ("SUCCESSFUL_BUY", "FAILED_ATTEMPT")
    }
    unresolved_wallet_details = sum(
        not bool(row.get("detail_ok")) for row in wallet.get("transactions") or []
    )
    coverage = {
        "version": "e4-v12-choice-set-coverage-v1",
        "source_manifest": str(args.manifest),
        "source_manifest_sha256": sha256_path(args.manifest),
        "sources": {
            "capture_runs": len(scans),
            "captured_launches": len(launch_by_mint),
            "captured_events": sum(scan.event_count for scan in scans),
            "capture_run_ids": [scan.spec.run_id for scan in scans],
            "research_artifact_run_ids": {
                name: value.get("run_id") for name, value in research.items() if value.get("run_id")
            },
        },
        "launch_labels": {
            "selected_landed": len(successful_labels),
            "selected_fill_rejected": len(failed_labels),
            "true_ignore": len(ignored_labels),
            "total": len(successful_labels | failed_labels | ignored_labels),
        },
        "successful_buys": {
            "discovered_in_resolved_wallet_history": discovered_success,
            "included": len(landed),
            "excluded": sum(row.get("selection_kind") == "SUCCESSFUL_BUY" for row in exclusions),
        },
        "failed_attempts": {
            "discovered_in_resolved_wallet_history": discovered_failed,
            "authoritative_mapped": len(
                (failed_artifact.get("failed_attempts") or {}).get("rows") or []
            ),
            "included": len(failed),
            "excluded": sum(row.get("selection_kind") == "FAILED_ATTEMPT" for row in exclusions),
        },
        "decision_groups": {
            "total": len(decisions),
            "selected_landed": len(landed),
            "selected_fill_rejected": len(failed),
            "rows": len(rows),
            "alternatives": len(rows) - len(decisions),
            "median_alternatives": statistics.median(alternatives) if alternatives else 0,
            "minimum_candidates": min(
                (row["decision_group_candidate_count"] for row in rows), default=0
            ),
            "maximum_candidates": max(
                (row["decision_group_candidate_count"] for row in rows), default=0
            ),
        },
        "coverage_percentages": {
            "captured_selection_percent": (
                len(decisions) / (len(successful_labels) + len(failed_labels)) * 100
            ),
            "resolved_wallet_selection_clean_sample_percent": (
                len(decisions) / (discovered_success + discovered_failed) * 100
            ),
            "audited_resolved_wallet_selection_percent": (
                (len(decisions) + len(exclusions)) / (discovered_success + discovered_failed) * 100
            ),
        },
        "field_coverage": {
            "reserve_state_rows": sum(row.get("reserve_state_ns") is not None for row in rows),
            "missing_reserve_state_rows": sum(row.get("reserve_state_ns") is None for row in rows),
            "decision_received_ns": len(rows),
            "missing_decision_transaction_index_groups": sum(
                item.source_transaction_index < 0 for item in decisions
            ),
            "missing_candidate_transaction_index_rows": sum(
                row.get("candidate_create_transaction_index") is None for row in rows
            ),
            "causal_social_rows": sum(
                bool(row.get("social_available_before_decision")) for row in rows
            ),
            "metadata_observation_rows": sum(
                bool(row.get("metadata_observation_available")) for row in rows
            ),
            "causal_creator_history_rows": sum(
                bool(row.get("creator_history_available")) for row in rows
            ),
            "first_buyer_history_rows": sum(
                bool(json.loads(str(row.get("first_buyer_identities_json") or "[]")))
                for row in rows
            ),
            "funder_history_rows": 0,
        },
        "quality": {
            "source_parse_errors": sum(scan.parse_errors for scan in scans),
            "duplicate_source_events": sum(scan.duplicate_events for scan in scans),
            "same_receipt_unordered_alternatives": chronology[
                "same_receipt_unordered_alternatives"
            ],
            "reserve_reproduction_count": len(reserve_errors),
            "reserve_reproduction_median_error_bps": (
                statistics.median(reserve_errors) if reserve_errors else None
            ),
            "reserve_reproduction_within_500_bps": sum(value <= 500 for value in reserve_errors),
        },
        "splits": ranges,
        "exclusions": exclusions,
        "exclusion_reason_counts": dict(sorted(exclusion_reason_counts.items())),
        "exclusion_reason_counts_by_selection": exclusion_reason_counts_by_selection,
        "wallet_history_counts": wallet_counts,
        "remaining_evidence_gaps": [
            f"{exclusion_reason_counts_by_selection['FAILED_ATTEMPT'].get('TARGET_LAUNCH_OUTSIDE_AUTHORITATIVE_CAPTURE', 0)} mapped failed attempts and {exclusion_reason_counts_by_selection['SUCCESSFUL_BUY'].get('TARGET_LAUNCH_OUTSIDE_AUTHORITATIVE_CAPTURE', 0)} in-interval landed buys target launches outside the frozen 36,000-launch corpus; they are explicitly excluded.",
            f"The full wallet ledger has {unresolved_wallet_details:,} unresolved transaction details; these cannot be silently asserted to be entry attempts.",
            "Capture JSONLs do not carry transaction indexes for most launch events; exact receipt-time ties without independent block order are conservatively omitted.",
            "No causal funding-wallet registry exists in the audited artifacts, so funder fields remain unavailable.",
            "Social and website fields are exposed only when the launch-time URI is content-addressed; mutable post-hoc metadata is not used as a causal feature.",
        ],
        "integrity": {
            "status": "PASS" if not violations and not ambiguity else "FAIL",
            "violations": violations,
            "future_leakage_violations": sum(
                row.get("code") == "FUTURE_FEATURE_TIMESTAMP" for row in violations
            ),
            "ambiguous_labels": len(ambiguity),
            "missing_chosen_rows": sum(
                row.get("code") == "CHOSEN_MINT_MISSING" for row in violations
            ),
            "every_exclusion_has_reason": all(bool(row.get("reason_code")) for row in exclusions),
            "deterministic_group_ids": True,
            "no_model_trained": True,
        },
    }
    if coverage["integrity"]["status"] != "PASS":
        raise ValueError(compact_json(coverage["integrity"]))

    write_jsonl(args.jsonl_output, rows)
    write_parquet(args.jsonl_output, args.parquet_output)
    coverage["outputs"] = {
        "jsonl": {
            "path": str(args.jsonl_output),
            "bytes": args.jsonl_output.stat().st_size,
            "sha256": sha256_path(args.jsonl_output),
        },
        "parquet": {
            "path": str(args.parquet_output),
            "bytes": args.parquet_output.stat().st_size,
            "sha256": sha256_path(args.parquet_output),
        },
    }
    args.coverage_output.parent.mkdir(parents=True, exist_ok=True)
    args.coverage_output.write_text(
        json.dumps(coverage, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    args.report_output.write_text(report_markdown(coverage), encoding="utf-8")
    print(
        json.dumps(
            {
                "decision_groups": coverage["decision_groups"],
                "launch_labels": coverage["launch_labels"],
                "coverage_percentages": coverage["coverage_percentages"],
                "integrity": coverage["integrity"],
                "outputs": coverage["outputs"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
