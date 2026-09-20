"""Shadow-only unknown-creator apprenticeship for frozen V12 Pre-Armed."""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import math
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import e4_v12_axiom_paper_live as paper
import e4_v12_true_latency_replay as replay

SCHEMA_VERSION = "v12-creator-apprentice-v1"
PROMOTION_VERSION = "v12-creator-promotion-candidates-v1"
HORIZONS_SECONDS = (2, 10, 60)
PROMOTION_MIN_FILLS = 3
PROMOTION_MIN_WIN_RATE = 2.0 / 3.0
PROMOTION_MIN_PROFIT_FACTOR = 1.5
PROMOTION_MIN_NET_PNL_SOL = 0.01


def finite(value: Any, default: float = 0.0) -> float:
    return paper.finite(value, default)


def integer(value: Any, default: int = 0) -> int:
    return paper.integer(value, default)


def stable_hash(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return copy.deepcopy(default)
    return json.loads(path.read_text(encoding="utf-8"))


def empty_state(model: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "model_sha256": stable_hash(model),
        "shadow_only": True,
        "real_money_execution": False,
        "production_authorised": False,
        "production_paths_changed": 0,
        "frozen_selector_modified": False,
        "processed_runs": [],
        "observations": [],
        "creators": {},
        "promotion_candidates": [],
        "counts": {},
    }


def creator_from_create(create: Mapping[str, Any]) -> str:
    raw = create.get("raw") if isinstance(create.get("raw"), Mapping) else {}
    return str(create.get("creator") or raw.get("creator") or create.get("trader") or "")


def uri_from_create(create: Mapping[str, Any]) -> str:
    raw = create.get("raw") if isinstance(create.get("raw"), Mapping) else {}
    return str(raw.get("uri") or create.get("uri") or "")


def is_mayhem(create: Mapping[str, Any]) -> bool:
    raw = create.get("raw") if isinstance(create.get("raw"), Mapping) else {}
    return bool(raw.get("is_mayhem_mode"))


def runner_profile(run: replay.RunData, mint: str, create_ns: int) -> dict[str, Any]:
    states = list(run.reserves_by_mint.get(mint, []))
    create_state = replay.state_at_or_before(states, create_ns)
    if create_state is None:
        return {"status": "NO_CAUSAL_RESERVE_STATE", "runner_2x_60s": False}
    create_price = create_state.price_sol or create_state.virtual_sol / max(
        create_state.virtual_tokens, 1e-18
    )
    if create_price <= 0:
        return {"status": "NO_CREATE_PRICE", "runner_2x_60s": False}
    output: dict[str, Any] = {"status": "OBSERVED"}
    for horizon in HORIZONS_SECONDS:
        deadline = create_ns + horizon * 1_000_000_000
        multiples: list[float] = []
        for state in states:
            if not (create_ns <= state.received_ns <= deadline):
                continue
            price = state.price_sol or state.virtual_sol / max(
                state.virtual_tokens, 1e-18
            )
            if price > 0:
                multiples.append(price / create_price)
        output[f"max_multiple_{horizon}s"] = max(multiples, default=1.0)
        output[f"last_multiple_{horizon}s"] = multiples[-1] if multiples else 1.0
    output["runner_2x_60s"] = output.get("max_multiple_60s", 1.0) >= 2.0
    output["runner_3x_60s"] = output.get("max_multiple_60s", 1.0) >= 3.0
    return output


def hypothetical_trade(
    run: replay.RunData,
    trace: Any,
    model: Mapping[str, Any],
    *,
    mint: str,
    creator: str,
    create_ns: int,
    decision_ns: int,
    decision_sequence: int,
    creator_seed_sol: float,
    profile: str,
) -> dict[str, Any]:
    candidate = paper.Candidate(
        run_id=run.run_id,
        mint=mint,
        creator=creator,
        handle="",
        status_id="",
        tweet_age_seconds=0.0,
        create_ns=create_ns,
        decision_ns=decision_ns,
        decision_sequence=decision_sequence,
        creator_seed_sol=creator_seed_sol,
    )
    budget = finite(model["position_sizing"]["starting_bankroll_sol"]) * finite(
        model["position_sizing"]["fraction_of_available_cash"]
    )
    trade, rejection = paper.simulate_trade(
        run,
        trace,
        candidate,
        budget,
        model,
        paper.load_costs(model),
        paper.load_policy(model),
    )
    if trade is None:
        return {
            "profile": profile,
            "status": "REJECTED",
            "win": False,
            "rejection_reason": rejection.get("reason") if rejection else None,
            "fee_sol": finite(rejection.get("fee_sol")) if rejection else 0.0,
        }
    pnl = finite(trade.get("pnl_sol"))
    return {
        "profile": profile,
        "status": "FILLED",
        "pnl_sol": pnl,
        "win": pnl > 0,
        "entry_cost_sol": finite(trade.get("entry_cost_sol")),
        "return_on_entry": pnl / max(finite(trade.get("entry_cost_sol")), 1e-12),
        "output_ratio": finite(trade.get("output_ratio")),
        "create_to_fill_price_multiple": finite(
            trade.get("create_to_fill_price_multiple")
        ),
        "exit_reason": trade.get("exit_reason"),
        "fill_ns": integer(trade.get("fill_ns")),
        "exit_ns": integer(trade.get("exit_ns")),
    }


def profit_factor(pnls: Sequence[float]) -> float:
    gains = sum(value for value in pnls if value > 0)
    losses = abs(sum(value for value in pnls if value <= 0))
    if losses <= 1e-12:
        return float("inf") if gains > 0 else 0.0
    return gains / losses


def creator_summary(
    creator: str, observations: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    rows = [row for row in observations if row.get("creator") == creator]
    rows.sort(key=lambda row: (integer(row.get("create_ns")), str(row.get("mint"))))

    scout_fills = [
        row for row in rows if row.get("scout", {}).get("status") == "FILLED"
    ]
    scout_pnls = [finite(row["scout"].get("pnl_sol")) for row in scout_fills]
    scout_winners = [
        row for row in scout_fills if finite(row["scout"].get("pnl_sol")) > 0
    ]
    scout_winning_mints = list(
        dict.fromkeys(str(row.get("mint")) for row in scout_winners)
    )

    compatible = [
        row
        for row in rows
        if row.get("v12_compatible", {}).get("status") == "FILLED"
    ]
    pnls = [finite(row["v12_compatible"].get("pnl_sol")) for row in compatible]
    winners = [
        row for row in compatible if finite(row["v12_compatible"].get("pnl_sol")) > 0
    ]
    winning_mints = list(dict.fromkeys(str(row.get("mint")) for row in winners))
    fills = len(compatible)
    wins = len(winners)
    losses = fills - wins
    win_rate = wins / fills if fills else 0.0
    net = sum(pnls)
    pf = profit_factor(pnls)

    status = "DISCOVERED"
    if any(row.get("runner", {}).get("runner_2x_60s") for row in rows):
        status = "RUNNER_OBSERVED"
    if len(scout_winning_mints) >= 1:
        status = "SHORTLISTED"
    if len(scout_winning_mints) >= 2:
        status = "CONFIRMED_REPEAT_WINNER"

    promotion_ready = bool(
        len(scout_winning_mints) >= 2
        and len(winning_mints) >= 2
        and fills >= PROMOTION_MIN_FILLS
        and win_rate >= PROMOTION_MIN_WIN_RATE
        and net >= PROMOTION_MIN_NET_PNL_SOL
        and pf >= PROMOTION_MIN_PROFIT_FACTOR
    )
    if promotion_ready:
        status = "PROMOTION_CANDIDATE"

    scout_wins = len(scout_winners)
    scout_losses = len(scout_fills) - scout_wins
    return {
        "creator": creator,
        "status": status,
        "promotion_ready": promotion_ready,
        "launches_seen": len(rows),
        "runner_2x_count": sum(
            bool(row.get("runner", {}).get("runner_2x_60s")) for row in rows
        ),
        "runner_3x_count": sum(
            bool(row.get("runner", {}).get("runner_3x_60s")) for row in rows
        ),
        "scout_fills": len(scout_fills),
        "scout_wins": scout_wins,
        "scout_losses": scout_losses,
        "scout_win_rate": scout_wins / len(scout_fills) if scout_fills else 0.0,
        "scout_net_pnl_sol": sum(scout_pnls),
        "scout_profit_factor": profit_factor(scout_pnls),
        "distinct_scout_winning_mints": scout_winning_mints,
        "v12_compatible_fills": fills,
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "net_pnl_sol": net,
        "expectancy_sol": net / fills if fills else 0.0,
        "profit_factor": pf,
        "distinct_winning_mints": winning_mints,
        "first_win_mint": scout_winning_mints[0] if scout_winning_mints else None,
        "second_confirmation_mint": (
            scout_winning_mints[1] if len(scout_winning_mints) > 1 else None
        ),
        "first_seen_ns": integer(rows[0].get("create_ns")) if rows else 0,
        "last_seen_ns": integer(rows[-1].get("create_ns")) if rows else 0,
        "automatic_whitelist_mutation": False,
    }

def rebuild_creator_index(state: dict[str, Any]) -> None:
    creators = sorted(
        {
            str(row.get("creator") or "")
            for row in state.get("observations", [])
            if str(row.get("creator") or "")
        }
    )
    state["creators"] = {
        creator: creator_summary(creator, state.get("observations", []))
        for creator in creators
    }
    state["promotion_candidates"] = [
        value
        for value in state["creators"].values()
        if value.get("promotion_ready")
    ]


def state_counts(state: Mapping[str, Any]) -> dict[str, Any]:
    creators = list(state.get("creators", {}).values())
    observations = list(state.get("observations", []))
    return {
        "processed_runs": len(state.get("processed_runs", [])),
        "unknown_launch_observations": len(observations),
        "unique_unknown_creators": len(creators),
        "runners_2x_60s": sum(
            bool(row.get("runner", {}).get("runner_2x_60s"))
            for row in observations
        ),
        "shortlisted_creators": sum(
            row.get("status")
            in {"SHORTLISTED", "CONFIRMED_REPEAT_WINNER", "PROMOTION_CANDIDATE"}
            for row in creators
        ),
        "confirmed_repeat_winners": sum(
            row.get("status") in {"CONFIRMED_REPEAT_WINNER", "PROMOTION_CANDIDATE"}
            for row in creators
        ),
        "promotion_candidates": sum(
            bool(row.get("promotion_ready")) for row in creators
        ),
    }


async def process_run(
    *,
    run_id: str,
    batch_path: Path,
    events_path: Path,
    model: Mapping[str, Any],
    state: dict[str, Any],
) -> dict[str, Any]:
    if state.get("model_sha256") != stable_hash(model):
        raise ValueError("creator apprentice model fingerprint changed")
    if run_id in {str(value) for value in state.get("processed_runs", [])}:
        return state

    run = replay.load_run(run_id, batch_path, events_path)
    traces = paper.trace_map(run_id, events_path)
    selector = model["selector"]
    prior_attempts = {
        str(key): integer(value)
        for key, value in model["creator_prior_e4_attempts"].items()
    }
    known_min = integer(selector["minimum_prior_e4_attempts"])
    counts: Counter[str] = Counter()
    new_rows: list[dict[str, Any]] = []

    for mint, rows in sorted(run.events_by_mint.items()):
        create = paper.creation(rows)
        if create is None:
            continue
        create_ns = integer(create.get("received_ns"))
        if create_ns <= integer(model["maximum_evidence_ns"]):
            raise ValueError("broad discovery row predates frozen evidence epoch")
        creator = creator_from_create(create)
        if not creator:
            counts["missing_creator"] += 1
            continue
        if prior_attempts.get(creator, 0) >= known_min:
            counts["known_creator_skipped"] += 1
            continue

        counts["unknown_creator_launches"] += 1
        seed, seed_decision_ns, seed_sequence = paper.creator_seed(rows, create, creator)
        trace = traces.get(mint)
        runner = runner_profile(run, mint, create_ns)
        if trace is None:
            scout = {"profile": "ALL_UNKNOWN_LAUNCHES", "status": "MISSING_TRACE", "win": False}
        else:
            scout = hypothetical_trade(
                run,
                trace,
                model,
                mint=mint,
                creator=creator,
                create_ns=create_ns,
                decision_ns=create_ns,
                decision_sequence=integer(create.get("__sequence"), -1),
                creator_seed_sol=seed,
                profile="ALL_UNKNOWN_LAUNCHES",
            )

        if is_mayhem(create):
            compatible = {"profile": "V12_COMPATIBLE", "status": "INELIGIBLE_MAYHEM", "win": False}
        elif seed < finite(selector["minimum_creator_seed_sol"]):
            compatible = {
                "profile": "V12_COMPATIBLE",
                "status": "INELIGIBLE_CREATOR_SEED",
                "win": False,
            }
        elif trace is None:
            compatible = {"profile": "V12_COMPATIBLE", "status": "MISSING_TRACE", "win": False}
        else:
            compatible = hypothetical_trade(
                run,
                trace,
                model,
                mint=mint,
                creator=creator,
                create_ns=create_ns,
                decision_ns=seed_decision_ns,
                decision_sequence=seed_sequence,
                creator_seed_sol=seed,
                profile="V12_COMPATIBLE",
            )
        new_rows.append(
            {
                "run_id": run_id,
                "mint": mint,
                "creator": creator,
                "create_ns": create_ns,
                "creator_seed_sol": seed,
                "is_mayhem_mode": is_mayhem(create),
                "uri": uri_from_create(create) or None,
                "runner": runner,
                "scout": scout,
                "v12_compatible": compatible,
                "shadow_only": True,
                "eligible_for_live_entry": False,
                "automatic_whitelist_mutation": False,
            }
        )

    existing = {
        (str(row.get("run_id")), str(row.get("mint")))
        for row in state.get("observations", [])
    }
    for row in new_rows:
        key = (str(row.get("run_id")), str(row.get("mint")))
        if key not in existing:
            state.setdefault("observations", []).append(row)
            existing.add(key)

    state.setdefault("processed_runs", []).append(run_id)
    rebuild_creator_index(state)
    state["counts"] = state_counts(state)
    state["last_run_id"] = run_id
    state["last_run_counts"] = dict(counts)
    state["shadow_only"] = True
    state["real_money_execution"] = False
    state["production_authorised"] = False
    state["production_paths_changed"] = 0
    state["frozen_selector_modified"] = False
    return state


def render_report(state: Mapping[str, Any]) -> str:
    counts = state.get("counts", {})
    leaders = sorted(
        state.get("creators", {}).values(),
        key=lambda row: (
            not bool(row.get("promotion_ready")),
            -integer(row.get("wins")),
            -finite(row.get("net_pnl_sol")),
        ),
    )[:20]
    lines = [
        "# V12 creator apprenticeship",
        "",
        "**Mode:** shadow-only. No creator is automatically admitted to V12.",
        "",
        "## Coverage",
        "",
        f"- Unknown launch observations: {counts.get('unknown_launch_observations', 0)}",
        f"- Unique unknown creators: {counts.get('unique_unknown_creators', 0)}",
        f"- 2x runners within 60s: {counts.get('runners_2x_60s', 0)}",
        f"- Shortlisted creators: {counts.get('shortlisted_creators', 0)}",
        f"- Confirmed repeat winners: {counts.get('confirmed_repeat_winners', 0)}",
        f"- Promotion candidates: {counts.get('promotion_candidates', 0)}",
        "",
        "## Promotion policy",
        "",
        "- First profitable V12-compatible hypothetical fill -> SHORTLISTED.",
        "- Second profitable launch on a different mint -> CONFIRMED_REPEAT_WINNER.",
        (
            f"- Promotion additionally requires >= {PROMOTION_MIN_FILLS} fills, "
            f">= {PROMOTION_MIN_WIN_RATE:.2%} WR, PF >= {PROMOTION_MIN_PROFIT_FACTOR:.2f}, "
            f"and net P&L >= {PROMOTION_MIN_NET_PNL_SOL:.3f} SOL."
        ),
        "- Promotion candidates are evidence only; frozen creator lists are never mutated.",
        "",
        "## Leading creators",
        "",
    ]
    for row in leaders:
        pf = row.get("profit_factor")
        pf_text = "inf" if isinstance(pf, float) and math.isinf(pf) else f"{finite(pf):.2f}"
        lines.append(
            f"- {row.get('creator')}: {row.get('status')} | "
            f"{row.get('wins')}W/{row.get('losses')}L | "
            f"net {finite(row.get('net_pnl_sol')):.4f} SOL | PF {pf_text}"
        )
    lines += [
        "",
        "## Integrity",
        "",
        f"- Frozen model SHA256: {state.get('model_sha256')}",
        "- Frozen selector modified: false",
        "- Production paths changed: 0",
        "- Real-money execution: false",
        "",
    ]
    return "\n".join(lines)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--run-id", required=True)
    value.add_argument("--batch", type=Path, required=True)
    value.add_argument("--events", type=Path, required=True)
    value.add_argument("--model", type=Path, required=True)
    value.add_argument("--state", type=Path, required=True)
    value.add_argument("--report", type=Path, required=True)
    value.add_argument("--promotion-candidates", type=Path, required=True)
    return value


async def run(args: argparse.Namespace) -> dict[str, Any]:
    model = read_json(args.model, {})
    state = read_json(args.state, None) or empty_state(model)
    state = await process_run(
        run_id=args.run_id,
        batch_path=args.batch,
        events_path=args.events,
        model=model,
        state=state,
    )
    paper.atomic_json(args.state, state)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(render_report(state), encoding="utf-8")
    paper.atomic_json(
        args.promotion_candidates,
        {
            "schema_version": PROMOTION_VERSION,
            "shadow_only": True,
            "automatic_whitelist_mutation": False,
            "model_sha256": state["model_sha256"],
            "candidates": state.get("promotion_candidates", []),
        },
    )
    return state


def main() -> int:
    args = parser().parse_args()
    result = asyncio.run(run(args))
    print(
        json.dumps(
            {
                "schema_version": result["schema_version"],
                "last_run_id": result.get("last_run_id"),
                "counts": result.get("counts", {}),
                "promotion_candidates": len(result.get("promotion_candidates", [])),
                "shadow_only": True,
                "frozen_selector_modified": False,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
