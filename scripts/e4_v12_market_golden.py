#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import json
import math
import random
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Mapping, Sequence

import joblib
import numpy as np
from sklearn.ensemble import ExtraTreesClassifier, HistGradientBoostingClassifier, RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from scripts import e4_v12_clustered_preimpact  # noqa: F401 - causal creator/funder aliases
from scripts import e4_v12_golden_thesis_search as golden
from scripts import e4_v12_independent_exit_replay as exit_replay
from scripts import e4_v12_independent_exit_replay_v2  # noqa: F401
from scripts import e4_v12_true_latency_replay as economics
from scripts import e4_v12_true_latency_replay_v3  # noqa: F401 - dynamic captured fees

FEATURES = list(golden.FEATURES) + [
    "stage_creator_seed",
    "stage_first_flow",
    "stage_second_flow",
    "stage_bundle",
    "stage_two_buyers",
]
STAGES = ("creator_seed", "first_flow", "second_flow", "bundle", "two_buyers")


def finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def row_stage(row: Mapping[str, Any]) -> str | None:
    if finite(row.get("trigger_is_creator")) >= 0.5:
        return "creator_seed"
    if integer(row.get("create_signature_buys")) >= 2 or integer(row.get("max_buys_one_signature")) >= 2:
        return "bundle"
    if integer(row.get("unique_buyers")) >= 2:
        return "two_buyers"
    buy_count = integer(row.get("buy_count"))
    if buy_count <= 1:
        return "first_flow"
    if buy_count == 2:
        return "second_flow"
    return None


def candidates(dataset: Sequence[Mapping[str, Any]], stage: str) -> list[dict[str, Any]]:
    chosen: dict[str, dict[str, Any]] = {}
    for source in sorted(dataset, key=lambda row: (integer(row.get("decision_ns")), str(row.get("mint") or ""))):
        mint = str(source.get("mint") or "")
        if not mint or mint in chosen or row_stage(source) != stage:
            continue
        if integer(source.get("sell_count")) > 0:
            continue
        age = finite(source.get("age_ms"))
        fdv = finite(source.get("fdv_usd"))
        seed = finite(source.get("creator_seed_sol"))
        outside = finite(source.get("outside_sol"))
        if not (0.0 <= age <= 1_500.0 and 2_500.0 <= fdv <= 12_000.0):
            continue
        if seed < 0.10 and outside < 0.10:
            continue
        row = dict(source)
        row["stage"] = stage
        row["requested_fraction"] = 0.0185
        row["score"] = 0.99
        row["mode"] = "v12_market_golden"
        chosen[mint] = row
    return list(chosen.values())


def feature_values(row: Mapping[str, Any]) -> dict[str, float]:
    values = dict(golden.feature_values(row))
    stage = str(row.get("stage") or "")
    for name in STAGES:
        values[f"stage_{name}"] = float(stage == name)
    return {feature: finite(values.get(feature)) for feature in FEATURES}


def matrix(rows: Sequence[Mapping[str, Any]]) -> np.ndarray:
    return np.asarray([[feature_values(row)[feature] for feature in FEATURES] for row in rows], dtype=float)


def to_prediction(row: Mapping[str, Any]) -> economics.Prediction:
    return economics.Prediction(
        mint=str(row.get("mint") or ""),
        decision_ns=integer(row.get("decision_ns")),
        requested_fraction=finite(row.get("requested_fraction"), 0.0185),
        score=finite(row.get("score"), 0.99),
        mode="v12_market_golden",
        metadata=dict(row),
    )


def policy_candidates() -> list[exit_replay.ExitPolicy]:
    rows = [
        exit_replay.ExitPolicy(0.05, 0.10, 0.95, 0.12, 0.05, 0.00, 1_000.0, 0.0),
        exit_replay.ExitPolicy(0.08, 0.15, 0.95, 0.18, 0.08, 0.00, 1_500.0, 0.0),
        exit_replay.ExitPolicy(0.10, 0.20, 0.95, 0.25, 0.10, 0.00, 2_500.0, 0.0),
        exit_replay.ExitPolicy(0.08, 0.15, 0.50, 0.40, 0.10, 0.00, 2_500.0, 0.0),
        exit_replay.ExitPolicy(0.10, 0.20, 0.30, 0.60, 0.15, 0.00, 5_000.0, 0.0),
        exit_replay.ExitPolicy(0.12, 0.25, 0.30, 0.75, 0.15, 0.00, 5_000.0, 25.0),
        exit_replay.ExitPolicy(0.15, 0.30, 0.30, 1.00, 0.20, 0.00, 10_000.0, 50.0),
        exit_replay.ExitPolicy(0.15, 0.40, 0.30, 1.50, 0.25, 0.00, 30_000.0, 100.0),
        exit_replay.ExitPolicy(0.20, 0.50, 0.30, 2.00, 0.30, -0.02, 60_000.0, 100.0),
        exit_replay.ExitPolicy(0.08, 0.10, 0.70, 0.30, 0.08, 0.00, 1_500.0, 0.0),
        exit_replay.ExitPolicy(0.10, 0.15, 0.70, 0.40, 0.10, 0.02, 2_500.0, 0.0),
        exit_replay.ExitPolicy(0.12, 0.20, 0.50, 0.50, 0.12, 0.02, 3_500.0, 25.0),
        exit_replay.ExitPolicy(0.15, 0.25, 0.50, 0.75, 0.15, 0.02, 5_000.0, 50.0),
        exit_replay.ExitPolicy(0.20, 0.30, 0.50, 1.00, 0.20, 0.05, 10_000.0, 100.0),
    ]
    rng = random.Random(812)
    for _ in range(50):
        tp1 = rng.choice((0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50))
        final = rng.choice([value for value in (0.30,0.50,0.75,1.0,1.5,2.0) if value > tp1])
        rows.append(
            exit_replay.ExitPolicy(
                rng.choice((0.05,0.08,0.10,0.12,0.15,0.20)),
                tp1,
                rng.choice((0.30,0.50,0.70,0.95)),
                final,
                rng.choice((0.05,0.08,0.10,0.15,0.20,0.25)),
                rng.choice((-0.05,-0.02,0.0,0.02,0.05)),
                rng.choice((750.0,1_000.0,1_500.0,2_500.0,5_000.0,10_000.0,30_000.0,60_000.0)),
                rng.choice((0.0,25.0,50.0,100.0)),
            )
        )
    unique = {tuple(policy.as_dict().values()): policy for policy in rows}
    return list(unique.values())


def model_specs() -> list[tuple[str, Any]]:
    return [
        ("logit", Pipeline([("scale", StandardScaler()), ("model", LogisticRegression(C=0.15, class_weight="balanced", max_iter=5_000, solver="liblinear", random_state=812))])),
        ("extra4", ExtraTreesClassifier(n_estimators=280, max_depth=4, min_samples_leaf=6, max_features="sqrt", class_weight="balanced_subsample", random_state=812, n_jobs=-1)),
        ("extra6", ExtraTreesClassifier(n_estimators=340, max_depth=6, min_samples_leaf=8, max_features="sqrt", class_weight="balanced_subsample", random_state=812, n_jobs=-1)),
        ("forest5", RandomForestClassifier(n_estimators=320, max_depth=5, min_samples_leaf=6, max_features="sqrt", class_weight="balanced_subsample", random_state=812, n_jobs=-1)),
        ("hist", HistGradientBoostingClassifier(max_iter=180, learning_rate=0.04, max_depth=4, min_samples_leaf=12, l2_regularization=4.0, random_state=812)),
    ]


def simulate_one(run: golden.RunData, row: Mapping[str, Any], policy: exit_replay.ExitPolicy, guard: int, latency: float, balance: float) -> Mapping[str, Any] | None:
    position, _ = exit_replay.simulate_independent(
        to_prediction(row),
        run.grouped.get(str(row.get("mint") or ""), ()),
        liquid_sol=balance,
        latency_ms=latency,
        reserve_sol=0.03,
        fee_bps=125,
        max_output_shortfall_bps=guard,
        policy=policy,
    )
    return position.as_dict() if position is not None else None


def robust_labels(runs_by_index: Mapping[int, golden.RunData], rows: Sequence[Mapping[str, Any]], policy: exit_replay.ExitPolicy, guard: int, latencies: Sequence[float], balance: float) -> np.ndarray:
    labels = []
    for row in rows:
        run = runs_by_index[integer(row.get("run_index"))]
        outcomes = [simulate_one(run, row, policy, guard, latency, balance) for latency in latencies]
        labels.append(int(all(outcome is not None and finite(outcome.get("pnl_sol")) > 0 for outcome in outcomes)))
    return np.asarray(labels, dtype=int)


def fit(template: Any, rows: Sequence[Mapping[str, Any]], labels: np.ndarray) -> Any:
    model = copy.deepcopy(template)
    if isinstance(model, HistGradientBoostingClassifier):
        positives = max(1, int(labels.sum()))
        weights = np.where(labels == 1, max(1.0, (len(labels) - positives) / positives), 1.0)
        model.fit(matrix(rows), labels, sample_weight=weights)
    else:
        model.fit(matrix(rows), labels)
    return model


def probabilities(model: Any, rows: Sequence[Mapping[str, Any]]) -> np.ndarray:
    return model.predict_proba(matrix(rows))[:, 1] if rows else np.asarray([], dtype=float)


def select(rows: Sequence[Mapping[str, Any]], model: Any, threshold: float, minimum_margin: float, cooldown_ms: float) -> list[dict[str, Any]]:
    values = probabilities(model, rows)
    scored = [(dict(row), float(value)) for row, value in zip(rows, values)]
    scored.sort(key=lambda item: (integer(item[0].get("decision_ns")), str(item[0].get("mint"))))
    selected = []
    recent: list[tuple[int, str, float]] = []
    last_ns = -10**30
    cooldown_ns = int(cooldown_ms * 1_000_000)
    seen: set[str] = set()
    for row, probability in scored:
        now = integer(row.get("decision_ns")); mint = str(row.get("mint") or "")
        recent = [item for item in recent if item[0] >= now - 250_000_000]
        best_other = max((score for _, other_mint, score in recent if other_mint != mint), default=0.0)
        recent.append((now, mint, probability))
        if not mint or mint in seen or probability < threshold or probability - best_other < minimum_margin or now - last_ns < cooldown_ns:
            continue
        seen.add(mint); last_ns = now
        row["score"] = probability; row["requested_fraction"] = 0.0185; row["mode"] = "v12_market_golden"
        selected.append(row)
    return selected


def aggregate(runs: Sequence[golden.RunData], rows: Sequence[Mapping[str, Any]], policy: exit_replay.ExitPolicy, guard: int, latencies: Sequence[float], balance: float) -> dict[str, Any]:
    by_run: defaultdict[int, list[economics.Prediction]] = defaultdict(list)
    for row in rows: by_run[integer(row.get("run_index"))].append(to_prediction(row))
    output = {}
    for latency in latencies:
        positions=[]; ending=[]; rejected=Counter()
        for run in runs:
            result=exit_replay.replay(by_run.get(run.run_index,()),run.grouped,starting_balance_sol=balance,latency_ms=latency,reserve_sol=0.03,fee_bps=125,max_output_shortfall_bps=guard,policy=policy,max_concurrent=2)
            positions.extend(result.get("positions") or []); ending.append(finite(result.get("ending_balance_sol"),balance)); rejected.update(result.get("rejected") or {})
        block=golden.positions_metrics(positions); block["positions"]=positions; block["mean_ending_balance_sol"]=statistics.fmean(ending) if ending else balance; block["rejected"]=dict(sorted(rejected.items()))
        output[str(int(latency) if float(latency).is_integer() else latency)]=block
    return output


def all_pass(blocks: Mapping[str, Mapping[str, Any]], minimum_trades: int, wr: float, pf: float) -> bool:
    return bool(blocks) and all(golden.passes_economics(block,minimum_trades,wr,pf) for block in blocks.values())


def objective(blocks: Mapping[str, Mapping[str, Any]], selected_count: int) -> tuple[float,...]:
    return (min(finite(b.get("win_rate")) for b in blocks.values()),min(finite(b.get("wilson_low")) for b in blocks.values()),min(finite(b.get("profit_factor")) for b in blocks.values()),sum(finite(b.get("net_pnl_sol")) for b in blocks.values()),min(integer(b.get("trades")) for b in blocks.values()),-selected_count)


def search(args: argparse.Namespace) -> int:
    runs=golden.load_runs([golden.parse_pair(value) for value in args.pair])
    if len(runs)<8: raise SystemExit("at least eight chronological windows required")
    dataset=golden.build_dataset(runs,args.horizon_ms)
    all_rows=[]
    for stage in STAGES: all_rows.extend(candidates(dataset,stage))
    holdout_start=len(runs)-2; validation_start=max(4,holdout_start-2)
    train=[r for r in all_rows if integer(r.get("run_index"))<validation_start]
    validation=[r for r in all_rows if validation_start<=integer(r.get("run_index"))<holdout_start]
    holdout=[r for r in all_rows if integer(r.get("run_index"))>=holdout_start]
    train_runs=runs[:validation_start]; validation_runs=runs[validation_start:holdout_start]; holdout_runs=runs[holdout_start:]
    runs_by_index={run.run_index:run for run in runs}; latencies=economics.parse_latencies(args.latencies)
    guards=(300,500,800,1000,1500)
    best=None; diagnostics=[]
    for policy in policy_candidates():
      for guard in guards:
        labels=robust_labels(runs_by_index,train,policy,guard,latencies,args.starting_balance_sol)
        positives=int(labels.sum())
        if positives<12 or positives>=len(labels): continue
        for model_name,template in model_specs():
          model=fit(template,train,labels)
          vp=probabilities(model,validation)
          thresholds=sorted(set(float(np.quantile(vp,q)) for q in (0.80,0.86,0.90,0.93,0.95,0.97,0.98,0.99,0.995))) if len(vp) else [1.0]
          for threshold in thresholds:
            for margin in (0.0,0.025,0.05,0.075,0.10,0.15):
              for cooldown in (0.0,50.0,100.0,250.0,500.0):
                selected_validation=select(validation,model,threshold,margin,cooldown)
                if len(selected_validation)<5: continue
                blocks=aggregate(validation_runs,selected_validation,policy,guard,latencies,args.starting_balance_sol)
                if not all_pass(blocks,5,args.minimum_win_rate,args.minimum_profit_factor): continue
                score=objective(blocks,len(selected_validation))
                item=(score,stage if False else "multi_stage",policy,guard,model_name,model,threshold,margin,cooldown,blocks)
                if best is None or score>best[0]: best=item
          diagnostics.append({"model":model_name,"guard":guard,"robust_train_wins":positives,"policy":policy.as_dict()})
    if best is None:
      report={"version":"e4-v12-market-golden-v1","status":"NOT_CONCLUSIVE","reason":"no direct market entry/exit model passed chronological validation at every latency","coverage":{"dataset":len(dataset),"candidates":len(all_rows),"train":len(train),"validation":len(validation),"holdout":len(holdout)},"diagnostics":len(diagnostics)}
      args.output.parent.mkdir(parents=True,exist_ok=True); args.output.write_text(json.dumps(report,indent=2,sort_keys=True)); print(json.dumps(report,indent=2,sort_keys=True)); return 2
    score,stage_mode,policy,guard,model_name,model,threshold,margin,cooldown,validation_blocks=best
    selected_holdout=select(holdout,model,threshold,margin,cooldown)
    holdout_blocks=aggregate(holdout_runs,selected_holdout,policy,guard,latencies,args.starting_balance_sol)
    passed=len(selected_holdout)>=5 and all_pass(holdout_blocks,5,args.minimum_win_rate,args.minimum_profit_factor)
    status="HISTORICAL_HOLDOUT_CONFIRMED" if passed else "NOT_CONCLUSIVE"
    report={"version":"e4-v12-market-golden-v1","status":status,"thesis":"Trade only the frozen high-confidence causal creator/funder, first-slot topology, velocity and relative-competition regime; protect the decision-time token output and use a frozen independent partial-stop-trailing exit.","entry_model":{"name":model_name,"threshold":threshold,"minimum_margin":margin,"cooldown_ms":cooldown},"exit_policy":policy.as_dict(),"guard_bps":guard,"features":FEATURES,"horizon_ms":args.horizon_ms,"latencies_ms":latencies,"starting_balance_sol":args.starting_balance_sol,"train_runs":[r.run_id for r in train_runs],"validation_runs":[r.run_id for r in validation_runs],"holdout_runs":[r.run_id for r in holdout_runs],"validation":validation_blocks,"holdout":holdout_blocks,"holdout_predictions":len(selected_holdout)}
    args.output.parent.mkdir(parents=True,exist_ok=True); args.output.write_text(json.dumps(report,indent=2,sort_keys=True)); args.predictions_output.write_text(json.dumps({"predictions":selected_holdout},indent=2,sort_keys=True)); joblib.dump({"version":"e4-v12-market-golden-model-v1","status":status,"model":model,"model_name":model_name,"threshold":threshold,"minimum_margin":margin,"cooldown_ms":cooldown,"exit_policy":policy.as_dict(),"guard_bps":guard,"features":FEATURES,"horizon_ms":args.horizon_ms,"history_run_ids":[r.run_id for r in runs]},args.model_output); print(json.dumps({"status":status,"holdout_predictions":len(selected_holdout)},indent=2)); return 0 if passed else 3


def apply(args: argparse.Namespace) -> int:
    bundle=joblib.load(args.model_input); runs=golden.load_runs([golden.parse_pair(value) for value in args.pair]); dataset=golden.build_dataset(runs,finite(bundle.get("horizon_ms"),args.horizon_ms)); rows=[]
    for stage in STAGES: rows.extend(candidates(dataset,stage))
    live_index=len(runs)-1; live=[r for r in rows if integer(r.get("run_index"))==live_index]
    selected=select(live,bundle["model"],finite(bundle["threshold"]),finite(bundle["minimum_margin"]),finite(bundle["cooldown_ms"])); args.predictions_output.parent.mkdir(parents=True,exist_ok=True); args.predictions_output.write_text(json.dumps({"version":"e4-v12-market-golden-live-v1","live_run_id":runs[-1].run_id,"predictions":selected},indent=2,sort_keys=True)); args.policy_output.write_text(json.dumps(bundle["exit_policy"],indent=2,sort_keys=True)); args.guard_output.write_text(str(integer(bundle["guard_bps"],800))+"\n"); print(json.dumps({"live_run_id":runs[-1].run_id,"predictions":len(selected)},indent=2)); return 0


def main() -> int:
    parser=argparse.ArgumentParser(description="Find a direct causal high-WR live-launch V12 thesis")
    parser.add_argument("--mode",choices=("search","apply"),default="search"); parser.add_argument("--pair",action="append",default=[]); parser.add_argument("--horizon-ms",type=float,default=750.0); parser.add_argument("--latencies",default="0,1,2,5,10"); parser.add_argument("--starting-balance-sol",type=float,default=3.0); parser.add_argument("--minimum-win-rate",type=float,default=0.65); parser.add_argument("--minimum-profit-factor",type=float,default=1.25); parser.add_argument("--output",type=Path,required=True); parser.add_argument("--model-output",type=Path,required=True); parser.add_argument("--model-input",type=Path); parser.add_argument("--predictions-output",type=Path,required=True); parser.add_argument("--policy-output",type=Path,default=Path("artifacts/market-policy.json")); parser.add_argument("--guard-output",type=Path,default=Path("artifacts/market-guard.txt")); args=parser.parse_args()
    if args.mode=="apply":
      if args.model_input is None: parser.error("--model-input required in apply mode")
      return apply(args)
    return search(args)


if __name__ == "__main__": raise SystemExit(main())
