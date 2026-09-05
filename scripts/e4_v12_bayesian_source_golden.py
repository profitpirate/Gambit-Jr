#!/usr/bin/env python3
from __future__ import annotations

import argparse
import itertools
import json
import math
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from scipy.stats import beta as beta_distribution

from scripts import e4_v12_reactive_profit_clustered  # noqa: F401 - causal creator/funder clusters
from scripts import e4_v12_reactive_profit_model as source_model
from scripts import e4_v12_golden_thesis_search as golden
from scripts import e4_v12_independent_exit_replay as exits
from scripts import e4_v12_independent_exit_replay_v2  # noqa: F401 - scaled E4 output protection
from scripts import e4_v12_true_latency_replay as economics


def finite(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def posterior(wins: int, losses: int) -> tuple[float, float]:
    wins = max(0, int(wins))
    losses = max(0, int(losses))
    alpha = wins + 1.0
    beta = losses + 1.0
    mean = alpha / (alpha + beta)
    lower = float(beta_distribution.ppf(0.10, alpha, beta))
    return mean, lower


def annotate(row: Mapping[str, Any]) -> dict[str, Any]:
    item = dict(row)
    creator_wins = integer(item.get("prior_creator_wins"))
    creator_losses = integer(item.get("prior_creator_losses"))
    creator_mean, creator_lower = posterior(creator_wins, creator_losses)

    max_buyer_attempts = integer(item.get("max_prior_buyer_attempts"))
    max_buyer_wins = integer(item.get("max_prior_buyer_wins"))
    max_buyer_losses = max(0, max_buyer_attempts - max_buyer_wins)
    buyer_mean, buyer_lower = posterior(max_buyer_wins, max_buyer_losses)

    sum_buyer_attempts = integer(item.get("sum_prior_buyer_attempts"))
    sum_buyer_wins = integer(item.get("sum_prior_buyer_wins"))
    sum_buyer_losses = max(0, sum_buyer_attempts - sum_buyer_wins)
    buyer_pool_mean, buyer_pool_lower = posterior(sum_buyer_wins, sum_buyer_losses)

    item.update(
        {
            "creator_posterior_mean": creator_mean,
            "creator_posterior_lower10": creator_lower,
            "buyer_posterior_mean": buyer_mean,
            "buyer_posterior_lower10": buyer_lower,
            "buyer_pool_posterior_mean": buyer_pool_mean,
            "buyer_pool_posterior_lower10": buyer_pool_lower,
            "creator_observations": creator_wins + creator_losses,
            "buyer_observations": max_buyer_attempts,
            "buyer_pool_observations": sum_buyer_attempts,
        }
    )
    return item


@dataclass(frozen=True)
class Rule:
    identity_mode: str
    minimum_creator_observations: int
    minimum_creator_mean: float
    minimum_creator_lower: float
    minimum_buyer_observations: int
    minimum_buyer_mean: float
    minimum_buyer_lower: float
    maximum_source_impact_bps: float
    minimum_source_sol: float
    maximum_source_sol: float
    minimum_fdv_usd: float
    maximum_fdv_usd: float
    maximum_pre_buy_count: int
    require_clustered_creator: bool
    guard_bps: int

    def identity_accepts(self, row: Mapping[str, Any]) -> bool:
        creator_ok = bool(
            integer(row.get("creator_observations")) >= self.minimum_creator_observations
            and finite(row.get("creator_posterior_mean")) >= self.minimum_creator_mean
            and finite(row.get("creator_posterior_lower10")) >= self.minimum_creator_lower
        )
        buyer_ok = bool(
            integer(row.get("buyer_observations")) >= self.minimum_buyer_observations
            and finite(row.get("buyer_posterior_mean")) >= self.minimum_buyer_mean
            and finite(row.get("buyer_posterior_lower10")) >= self.minimum_buyer_lower
        )
        if self.identity_mode == "creator":
            return creator_ok
        if self.identity_mode == "buyer":
            return buyer_ok
        if self.identity_mode == "creator_or_buyer":
            return creator_ok or buyer_ok
        if self.identity_mode == "creator_and_buyer":
            return creator_ok and buyer_ok
        if self.identity_mode == "any_history":
            return creator_ok or buyer_ok or integer(row.get("max_creator_buyer_pair")) > 0
        raise ValueError(self.identity_mode)

    def accepts(self, row: Mapping[str, Any]) -> bool:
        source_sol = finite(row.get("source_sol"))
        fdv = finite(row.get("entry_fdv_usd"))
        return bool(
            self.identity_accepts(row)
            and finite(row.get("source_price_impact_bps")) <= self.maximum_source_impact_bps
            and self.minimum_source_sol <= source_sol <= self.maximum_source_sol
            and self.minimum_fdv_usd <= fdv <= self.maximum_fdv_usd
            and integer(row.get("pre_buy_count")) <= self.maximum_pre_buy_count
            and (
                not self.require_clustered_creator
                or bool(row.get("clustered_creator"))
            )
        )

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Rule":
        return cls(**{name: value[name] for name in cls.__dataclass_fields__})


def policy_candidates() -> list[exits.ExitPolicy]:
    return [
        exits.ExitPolicy(0.05, 0.10, 0.95, 0.15, 0.05, 0.00, 1_000.0, 0.0),
        exits.ExitPolicy(0.08, 0.15, 0.95, 0.25, 0.08, 0.00, 1_500.0, 0.0),
        exits.ExitPolicy(0.10, 0.20, 0.95, 0.40, 0.10, 0.00, 2_500.0, 0.0),
        exits.ExitPolicy(0.08, 0.15, 0.70, 0.35, 0.08, 0.00, 2_500.0, 0.0),
        exits.ExitPolicy(0.10, 0.20, 0.70, 0.50, 0.10, 0.02, 3_500.0, 25.0),
        exits.ExitPolicy(0.10, 0.20, 0.50, 0.60, 0.12, 0.00, 5_000.0, 25.0),
        exits.ExitPolicy(0.12, 0.25, 0.50, 0.75, 0.15, 0.02, 5_000.0, 50.0),
        exits.ExitPolicy(0.15, 0.30, 0.50, 1.00, 0.20, 0.02, 10_000.0, 50.0),
        exits.ExitPolicy(0.10, 0.20, 0.30, 0.60, 0.15, 0.00, 5_000.0, 0.0),
        exits.ExitPolicy(0.12, 0.25, 0.30, 0.75, 0.15, 0.00, 7_500.0, 25.0),
        exits.ExitPolicy(0.15, 0.30, 0.30, 1.00, 0.20, 0.00, 10_000.0, 50.0),
        exits.ExitPolicy(0.15, 0.40, 0.30, 1.50, 0.25, 0.00, 30_000.0, 100.0),
        exits.ExitPolicy(0.20, 0.50, 0.30, 2.00, 0.30, -0.02, 60_000.0, 100.0),
        exits.ExitPolicy(0.08, 0.10, 0.70, 0.30, 0.08, 0.00, 1_500.0, 0.0),
        exits.ExitPolicy(0.10, 0.15, 0.70, 0.40, 0.10, 0.02, 2_500.0, 0.0),
        exits.ExitPolicy(0.12, 0.20, 0.50, 0.50, 0.12, 0.02, 3_500.0, 25.0),
    ]


def rule_candidates() -> Sequence[Rule]:
    identity_settings = [
        ("creator", 1, 0.55, 0.20, 0, 0.0, 0.0),
        ("creator", 2, 0.60, 0.25, 0, 0.0, 0.0),
        ("creator", 3, 0.65, 0.30, 0, 0.0, 0.0),
        ("creator", 5, 0.70, 0.40, 0, 0.0, 0.0),
        ("creator", 8, 0.75, 0.50, 0, 0.0, 0.0),
        ("buyer", 0, 0.0, 0.0, 1, 0.55, 0.20),
        ("buyer", 0, 0.0, 0.0, 2, 0.60, 0.25),
        ("buyer", 0, 0.0, 0.0, 3, 0.65, 0.30),
        ("creator_or_buyer", 2, 0.60, 0.25, 2, 0.60, 0.25),
        ("creator_or_buyer", 3, 0.65, 0.30, 3, 0.65, 0.30),
        ("creator_and_buyer", 1, 0.55, 0.20, 1, 0.55, 0.20),
        ("creator_and_buyer", 2, 0.60, 0.25, 2, 0.60, 0.25),
        ("any_history", 2, 0.60, 0.25, 2, 0.60, 0.25),
        ("any_history", 3, 0.65, 0.30, 3, 0.65, 0.30),
    ]
    fdv_bands = (
        (2_500.0, 6_000.0),
        (2_500.0, 8_500.0),
        (2_500.0, 10_000.0),
        (3_000.0, 7_500.0),
        (3_500.0, 8_500.0),
    )
    output = []
    for identity, impact, source_band, fdv_band, max_buys, cluster, guard in itertools.product(
        identity_settings,
        (400.0, 600.0, 800.0, 1_000.0, 1_500.0, 2_500.0, 5_000.0),
        ((0.0, 1.5), (0.0, 2.5), (0.0, 5.0), (1.0, 5.0), (1.0, 8.0), (2.0, 8.0)),
        fdv_bands,
        (2, 3, 5, 8, 20),
        (False, True),
        (300, 500, 800, 1_000, 1_500),
    ):
        output.append(
            Rule(
                *identity,
                maximum_source_impact_bps=impact,
                minimum_source_sol=source_band[0],
                maximum_source_sol=source_band[1],
                minimum_fdv_usd=fdv_band[0],
                maximum_fdv_usd=fdv_band[1],
                maximum_pre_buy_count=max_buys,
                require_clustered_creator=cluster,
                guard_bps=guard,
            )
        )
    return output


def select(rows: Sequence[Mapping[str, Any]], rule: Rule) -> list[dict[str, Any]]:
    return [annotate(row) for row in rows if rule.accepts(annotate(row))]


def to_prediction(row: Mapping[str, Any]) -> economics.Prediction:
    return economics.Prediction(
        mint=str(row.get("mint") or ""),
        decision_ns=integer(row.get("decision_ns")),
        requested_fraction=0.0185,
        score=0.99,
        mode="v12_bayesian_source",
        metadata=dict(row),
    )


def aggregate(
    runs: Sequence[golden.RunData],
    rows: Sequence[Mapping[str, Any]],
    policy: exits.ExitPolicy,
    rule: Rule,
    latencies: Sequence[float],
    starting_balance_sol: float,
) -> dict[str, Any]:
    by_run: dict[int, list[economics.Prediction]] = {}
    for row in rows:
        by_run.setdefault(integer(row.get("run_index")), []).append(to_prediction(row))
    output: dict[str, Any] = {}
    for latency in latencies:
        positions=[]
        ending=[]
        rejected: dict[str, int] = {}
        for run in runs:
            result=exits.replay(
                by_run.get(run.run_index, ()),
                run.grouped,
                starting_balance_sol=starting_balance_sol,
                latency_ms=latency,
                reserve_sol=0.03,
                fee_bps=125,
                max_output_shortfall_bps=rule.guard_bps,
                policy=policy,
                max_concurrent=2,
            )
            positions.extend(result.get("positions") or [])
            ending.append(finite(result.get("ending_balance_sol"),starting_balance_sol))
            for key,value in (result.get("rejected") or {}).items():
                rejected[key]=rejected.get(key,0)+integer(value)
        block=golden.positions_metrics(positions)
        block["positions"]=positions
        block["mean_ending_balance_sol"]=statistics.fmean(ending) if ending else starting_balance_sol
        block["rejected"]=dict(sorted(rejected.items()))
        output[str(int(latency) if float(latency).is_integer() else latency)]=block
    return output


def all_pass(blocks: Mapping[str, Mapping[str, Any]], minimum_trades: int, minimum_wr: float, minimum_pf: float) -> bool:
    return bool(blocks) and all(golden.passes_economics(block,minimum_trades,minimum_wr,minimum_pf) for block in blocks.values())


def e4_quality(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    rows=list(rows)
    wins=sum(bool(row.get("e4_won")) for row in rows)
    return {"trades":len(rows),"wins":wins,"win_rate":wins/len(rows) if rows else 0.0,"net_pnl_sol":sum(finite(row.get("e4_pnl_sol")) for row in rows)}


def objective(blocks: Mapping[str, Mapping[str, Any]], quality: Mapping[str, Any], complexity: int) -> tuple[float,...]:
    return (
        min(finite(block.get("win_rate")) for block in blocks.values()),
        min(finite(block.get("wilson_low")) for block in blocks.values()),
        min(finite(block.get("profit_factor")) for block in blocks.values()),
        finite(quality.get("win_rate")),
        sum(finite(block.get("net_pnl_sol")) for block in blocks.values()),
        min(integer(block.get("trades")) for block in blocks.values()),
        -complexity,
    )


def search(args: argparse.Namespace) -> int:
    runs=golden.load_runs([golden.parse_pair(value) for value in args.pair])
    if len(runs)<8:
        raise SystemExit("at least eight chronological windows are required")
    rows=source_model.build_rows(runs)
    annotated=[annotate(row) for row in rows]
    holdout_start=len(runs)-2
    validation_start=max(4,holdout_start-2)
    train=[row for row in annotated if integer(row.get("run_index"))<validation_start]
    validation=[row for row in annotated if validation_start<=integer(row.get("run_index"))<holdout_start]
    holdout=[row for row in annotated if integer(row.get("run_index"))>=holdout_start]
    train_runs=runs[:validation_start]
    validation_runs=runs[validation_start:holdout_start]
    holdout_runs=runs[holdout_start:]
    latencies=economics.parse_latencies(args.latencies)

    raw_candidates=[]
    for rule in rule_candidates():
        train_selected=[row for row in train if rule.accepts(row)]
        validation_selected=[row for row in validation if rule.accepts(row)]
        if len(train_selected)<8 or len(validation_selected)<4:
            continue
        train_quality=e4_quality(train_selected)
        validation_quality=e4_quality(validation_selected)
        if train_quality["win_rate"]<0.58 or validation_quality["win_rate"]<0.60:
            continue
        score=(validation_quality["win_rate"],validation_quality["net_pnl_sol"],len(validation_selected),train_quality["win_rate"],-rule.guard_bps)
        raw_candidates.append((score,rule,train_selected,validation_selected,train_quality,validation_quality))
    raw_candidates.sort(key=lambda item:item[0],reverse=True)

    best=None
    for _,rule,train_selected,validation_selected,train_quality,validation_quality in raw_candidates[: args.maximum_rules]:
        for policy in policy_candidates():
            train_blocks=aggregate(train_runs,train_selected,policy,rule,latencies,args.starting_balance_sol)
            if not all_pass(train_blocks,8,args.minimum_win_rate,args.minimum_profit_factor):
                continue
            validation_blocks=aggregate(validation_runs,validation_selected,policy,rule,latencies,args.starting_balance_sol)
            if not all_pass(validation_blocks,4,args.minimum_win_rate,args.minimum_profit_factor):
                continue
            score=objective(validation_blocks,validation_quality,1)
            candidate=(score,rule,policy,train_blocks,validation_blocks,train_quality,validation_quality)
            if best is None or score>best[0]:
                best=candidate

    if best is None:
        report={"version":"e4-v12-bayesian-source-golden-v1","status":"NOT_CONCLUSIVE","reason":"no causal posterior/source guard and independent exit passed train plus validation at every latency","coverage":{"rows":len(rows),"train":len(train),"validation":len(validation),"holdout":len(holdout)},"quality_candidates":len(raw_candidates)}
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(report,indent=2,sort_keys=True),encoding="utf-8")
        print(json.dumps(report,indent=2,sort_keys=True))
        return 2

    _,rule,policy,train_blocks,validation_blocks,train_quality,validation_quality=best
    holdout_selected=[row for row in holdout if rule.accepts(row)]
    holdout_quality=e4_quality(holdout_selected)
    holdout_blocks=aggregate(holdout_runs,holdout_selected,policy,rule,latencies,args.starting_balance_sol)
    passed=len(holdout_selected)>=4 and holdout_quality["win_rate"]>=0.60 and all_pass(holdout_blocks,4,args.minimum_win_rate,args.minimum_profit_factor)
    status="HISTORICAL_HOLDOUT_CONFIRMED" if passed else "NOT_CONCLUSIVE"
    report={"version":"e4-v12-bayesian-source-golden-v1","status":status,"thesis":"After authenticated E4 source intent, trade only a causally proven creator/funder or first-buyer posterior-confidence cohort whose source price impact, stake, FDV and buy rank remain inside a frozen regime; enforce scaled BuyExactSolIn output and a frozen independent partial-stop-trailing exit.","rule":rule.as_dict(),"exit_policy":policy.as_dict(),"latencies_ms":latencies,"starting_balance_sol":args.starting_balance_sol,"train_runs":[run.run_id for run in train_runs],"validation_runs":[run.run_id for run in validation_runs],"holdout_runs":[run.run_id for run in holdout_runs],"train_quality":train_quality,"validation_quality":validation_quality,"holdout_quality":holdout_quality,"train":train_blocks,"validation":validation_blocks,"holdout":holdout_blocks,"holdout_predictions":len(holdout_selected)}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,indent=2,sort_keys=True),encoding="utf-8")
    args.model_output.write_text(json.dumps({"version":"e4-v12-bayesian-source-model-v1","status":status,"rule":rule.as_dict(),"exit_policy":policy.as_dict(),"history_run_ids":[run.run_id for run in runs]},indent=2,sort_keys=True),encoding="utf-8")
    args.predictions_output.write_text(json.dumps({"predictions":holdout_selected},indent=2,sort_keys=True),encoding="utf-8")
    print(json.dumps({"status":status,"holdout_predictions":len(holdout_selected),"holdout_quality":holdout_quality,"rule":rule.as_dict()},indent=2,sort_keys=True))
    return 0 if passed else 3


def apply(args: argparse.Namespace) -> int:
    payload=json.loads(args.model_input.read_text(encoding="utf-8"))
    rule=Rule.from_dict(payload["rule"])
    runs=golden.load_runs([golden.parse_pair(value) for value in args.pair])
    rows=[annotate(row) for row in source_model.build_rows(runs)]
    live_index=len(runs)-1
    selected=[row for row in rows if integer(row.get("run_index"))==live_index and rule.accepts(row)]
    args.predictions_output.parent.mkdir(parents=True,exist_ok=True)
    args.predictions_output.write_text(json.dumps({"version":"e4-v12-bayesian-source-live-v1","live_run_id":runs[-1].run_id,"rule":rule.as_dict(),"predictions":selected},indent=2,sort_keys=True),encoding="utf-8")
    args.policy_output.write_text(json.dumps(payload["exit_policy"],indent=2,sort_keys=True),encoding="utf-8")
    args.guard_output.write_text(str(rule.guard_bps)+"\n",encoding="utf-8")
    print(json.dumps({"live_run_id":runs[-1].run_id,"predictions":len(selected)},indent=2))
    return 0


def main() -> int:
    parser=argparse.ArgumentParser(description="Find a causal Bayesian E4 source-confidence golden thesis")
    parser.add_argument("--mode",choices=("search","apply"),default="search")
    parser.add_argument("--pair",action="append",default=[])
    parser.add_argument("--latencies",default="0,1,2,5,10")
    parser.add_argument("--starting-balance-sol",type=float,default=3.0)
    parser.add_argument("--minimum-win-rate",type=float,default=0.65)
    parser.add_argument("--minimum-profit-factor",type=float,default=1.25)
    parser.add_argument("--maximum-rules",type=int,default=120)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--model-output",type=Path,required=True)
    parser.add_argument("--model-input",type=Path)
    parser.add_argument("--predictions-output",type=Path,required=True)
    parser.add_argument("--policy-output",type=Path,default=Path("artifacts/bayesian-policy.json"))
    parser.add_argument("--guard-output",type=Path,default=Path("artifacts/bayesian-guard.txt"))
    args=parser.parse_args()
    if args.mode=="apply":
        if args.model_input is None:
            parser.error("--model-input is required in apply mode")
        return apply(args)
    return search(args)


if __name__=="__main__":
    raise SystemExit(main())
