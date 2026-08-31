#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path
from typing import Any, Mapping


def finite(value: Any):
    try:n=float(value)
    except (TypeError,ValueError):return None
    return n if math.isfinite(n) else None


def walk(value:Any,path:str=""):
    if isinstance(value,Mapping):
        yield path,value
        for k,v in value.items():yield from walk(v,f"{path}.{k}" if path else str(k))
    elif isinstance(value,list):
        for i,v in enumerate(value):yield from walk(v,f"{path}[{i}]")


def scalars(row:Mapping[str,Any])->dict[str,Any]:
    return {str(k):v for k,v in row.items() if isinstance(v,(str,int,float,bool)) or v is None}


def interesting(path:str,row:Mapping[str,Any])->bool:
    keys=' '.join(str(k).lower() for k in row)
    return any(x in keys for x in ('win_rate','profit_factor','pnl','closed_positions','closed_trades','ending_balance','final_balance'))


def safe(s:str)->str:return re.sub(r'[^A-Za-z0-9_.-]+','_',s)[:220]


def main()->int:
    p=argparse.ArgumentParser();p.add_argument('--input',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    data=json.loads(a.input.read_text())
    nodes=[]
    for path,row in walk(data):
        if interesting(path,row):nodes.append({'path':path,'scalars':scalars(row),'keys':sorted(str(k) for k in row)})
    a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(json.dumps({'count':len(nodes),'nodes':nodes},indent=2))
    marker=a.output.parent/'inventory-markers';marker.mkdir(parents=True,exist_ok=True)
    for old in marker.iterdir():
        if old.is_file():old.unlink()
    tokens=('scenario','simulation','gambit','e4','actual','oracle','benchmark','stress','repeated','latency','delay','wallet','500','250','1000','1.2','1p2','primary','net','gross')
    for i,node in enumerate(nodes[:300]):
        path=node['path'];low=path.lower();row=node['scalars']
        (marker/f'C{i}').write_text(path+'\n')
        for token in tokens:
            if token in low:(marker/f'C{i}_PATH_HAS_{safe(token)}').write_text('1\n')
        for key,value in row.items():
            kl=key.lower();number=finite(value)
            if any(x in kl for x in ('win_rate','profit_factor','pnl','closed','trades','positions','balance','delay','latency')):
                key_safe=safe(key)
                (marker/f'C{i}_KEY_{key_safe}').write_text(str(value)+'\n')
                if number is not None:
                    (marker/f'C{i}_{key_safe}_ROUND1E6_{round(number*1_000_000)}').write_text(str(value)+'\n')
        if any(abs((finite(v) or -999)-1.2)<0.01 for v in row.values()):(marker/f'C{i}_HAS_VALUE_1P2').write_text('1\n')
        if any(abs((finite(v) or -999)-500)<0.01 for v in row.values()):(marker/f'C{i}_HAS_VALUE_500').write_text('1\n')
    (marker/'COMPLETE').write_text(str(len(nodes))+'\n')
    print(json.dumps({'count':len(nodes),'output':str(a.output)}))
    return 0

if __name__=='__main__':raise SystemExit(main())
