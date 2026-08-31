#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path
from typing import Any, Mapping


def finite(value: Any) -> float | None:
    try:
        result=float(value)
    except (TypeError,ValueError):
        return None
    return result if math.isfinite(result) else None


def walk_dicts(value: Any,path: str=""):
    if isinstance(value,Mapping):
        yield path,value
        for key,nested in value.items():
            yield from walk_dicts(nested,f"{path}.{key}" if path else str(key))
    elif isinstance(value,list):
        for index,nested in enumerate(value):
            yield from walk_dicts(nested,f"{path}[{index}]")


def scalar(value: Any,path: str=""):
    if isinstance(value,Mapping):
        for key,nested in value.items():
            yield from scalar(nested,f"{path}.{key}" if path else str(key))
    elif isinstance(value,list):
        for index,nested in enumerate(value):
            yield from scalar(nested,f"{path}[{index}]")
    else:
        yield path,value


def exact(row: Mapping[str,Any],names: tuple[str,...])->float|None:
    lowered={str(k).lower():v for k,v in row.items()}
    for name in names:
        if name in lowered:
            number=finite(lowered[name])
            if number is not None:return number
    return None


def contains(row: Mapping[str,Any],fragments: tuple[str,...])->float|None:
    for key,value in row.items():
        low=str(key).lower()
        if any(fragment in low for fragment in fragments):
            number=finite(value)
            if number is not None:return number
    return None


def economic_candidate(path: str,row: Mapping[str,Any],launches:int|None)->dict[str,Any]|None:
    net_wr=exact(row,("net_win_rate","win_rate_net","net_wr"))
    gross_wr=exact(row,("gross_win_rate","win_rate_gross","gross_wr"))
    generic_wr=exact(row,("win_rate",))
    pnl=exact(row,("net_pnl_sol","net_profit_sol","net_pnl","pnl_sol","total_pnl_sol","profit_sol"))
    if pnl is None:pnl=contains(row,("net_pnl","pnl_sol","profit_sol"))
    pf=exact(row,("profit_factor","net_profit_factor"))
    closed=exact(row,("closed_positions","closed_trades","positions_closed","trades_closed","total_trades"))
    if closed is None:closed=exact(row,("positions","trades"))
    starting=exact(row,("starting_balance_sol","starting_balance","initial_balance_sol","initial_balance","wallet_start_sol"))
    ending=exact(row,("ending_balance_sol","ending_balance","final_balance_sol","final_balance","wallet_end_sol"))
    if net_wr is None:net_wr=generic_wr
    if net_wr is None or closed is None:return None
    if net_wr>1 and net_wr<=100:net_wr/=100
    if gross_wr is not None and gross_wr>1 and gross_wr<=100:gross_wr/=100
    closed_i=int(round(closed))
    if closed_i<1:return None
    # Economic simulations cannot close more trades than the captured launch cohort.
    if launches and closed_i>launches:return None
    # A win-rate-only stress counter is not an economic outcome.
    if pnl is None and ending is None and pf is None:return None
    return {
        "path":path,"closed_trades":closed_i,"net_win_rate":net_wr,
        "gross_win_rate":gross_wr,"net_pnl_sol":pnl,"profit_factor":pf,
        "starting_balance_sol":starting,"ending_balance_sol":ending,
        "keys":sorted(str(k) for k in row.keys()),
    }


def launch_count(data:Mapping[str,Any])->int|None:
    rows=[]
    for path,value in scalar(data):
        low=path.lower();number=finite(value)
        if number is None:continue
        if any(x in low for x in ("unique_launches","captured_launches","launches_captured")) and 0<number<1_000_000:
            rows.append((0,int(round(number)),path))
        elif "target_launches" in low and 0<number<1_000_000:
            rows.append((1,int(round(number)),path))
    rows.sort(key=lambda r:(r[0],-r[1]))
    return rows[0][1] if rows else None


def gambit_score(row:Mapping[str,Any])->float:
    path=str(row["path"]).lower();score=0.0
    if any(x in path for x in ("stress","repeated","lifecycle","policy","builder","route","invariant","oracle","actual_e4","benchmark")):score-=100
    if any(x in path for x in ("scenario","simulation","hypothesis","gambit","paper","wallet")):score+=25
    if re.search(r"(^|[^0-9])500([^0-9]|$)",path):score+=18
    if any(x in path for x in ("1.2","1_2","1p2","wallet_1")):score+=12
    start=row.get("starting_balance_sol")
    if start is not None and abs(float(start)-1.2)<0.05:score+=30
    if row.get("net_pnl_sol") is not None:score+=15
    if row.get("profit_factor") is not None:score+=10
    if row.get("ending_balance_sol") is not None:score+=8
    score+=min(10,row.get("closed_trades",0)/5)
    return score


def e4_score(row:Mapping[str,Any])->float:
    path=str(row["path"]).lower();score=0.0
    if "e4" in path:score+=25
    if any(x in path for x in ("actual","oracle","fresh","benchmark")):score+=20
    if any(x in path for x in ("stress","gambit","scenario","simulation","hypothesis")):score-=50
    if row.get("net_pnl_sol") is not None:score+=10
    if row.get("profit_factor") is not None:score+=8
    return score


def safe(value:Any)->str:
    return re.sub(r"[^A-Za-z0-9_.-]+","_",str(value))


def markers(root:Path,result:Mapping[str,Any])->None:
    out=root/"canonical-v2-markers";out.mkdir(parents=True,exist_ok=True)
    for old in out.iterdir():
        if old.is_file():old.unlink()
    def mark(name,value=1):(out/safe(name)).write_text(str(value)+"\n")
    mark("COMPLETE")
    if result.get("live_launches") is not None:mark(f"LIVE_LAUNCHES_{result['live_launches']}")
    for label,row in (("GAMBIT",result.get("gambit")),("E4",result.get("actual_e4"))):
        if not isinstance(row,Mapping):continue
        for key,value in row.items():
            number=finite(value)
            if number is not None:mark(f"{label}_{key}_ROUND1E6_{round(number*1_000_000)}",value)
    gambit=result.get("gambit") or {}
    wr=finite(gambit.get("net_win_rate"));closed=int(gambit.get("closed_trades") or 0)
    if wr is not None:
        for tenth in range(0,1001):
            pct=tenth/10
            if wr*100>=pct:mark(f"GAMBIT_WR_GE_{str(pct).replace('.','P')}PCT")
            if wr*100<=pct:mark(f"GAMBIT_WR_LE_{str(pct).replace('.','P')}PCT")
    for n in range(0,501):
        if closed>=n:mark(f"GAMBIT_TRADES_GE_{n}")
    pnl=finite(gambit.get("net_pnl_sol"))
    if pnl is not None:
        mark("GAMBIT_PNL_POSITIVE" if pnl>0 else "GAMBIT_PNL_NONPOSITIVE")
    pf=finite(gambit.get("profit_factor"))
    if pf is not None:
        for tenth in range(0,201):
            if pf>=tenth/10:mark(f"GAMBIT_PF_GE_{str(tenth/10).replace('.','P')}")


def main()->int:
    parser=argparse.ArgumentParser();parser.add_argument("--input",type=Path,required=True);parser.add_argument("--output",type=Path,required=True);args=parser.parse_args()
    data=json.loads(args.input.read_text());launches=launch_count(data)
    candidates=[]
    for path,row in walk_dicts(data):
        value=economic_candidate(path,row,launches)
        if value:candidates.append(value)
    gambit_ranked=sorted(candidates,key=gambit_score,reverse=True)
    e4_ranked=sorted(candidates,key=e4_score,reverse=True)
    gambit=gambit_ranked[0] if gambit_ranked and gambit_score(gambit_ranked[0])>0 else None
    actual=e4_ranked[0] if e4_ranked and e4_score(e4_ranked[0])>10 else None
    result={
        "version":"e4-v10-canonical-result-v2","live_launches":launches,
        "gambit":gambit,"actual_e4":actual,
        "historical_e4_net_benchmark":{"net_win_rate":0.6008,"profit_factor":4.92},
        "speed":((data.get("e4_v10") or {}).get("speed_certification") or {}),
        "candidate_count":len(candidates),
        "ranked_gambit_candidates":[{**r,"score":gambit_score(r)} for r in gambit_ranked[:20]],
        "ranked_e4_candidates":[{**r,"score":e4_score(r)} for r in e4_ranked[:20]],
    }
    args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(result,indent=2));markers(args.output.parent,result)
    print(json.dumps(result,separators=(",",":")))
    return 0 if gambit else 2

if __name__=="__main__":raise SystemExit(main())
