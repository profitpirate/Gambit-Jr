#!/usr/bin/env python3
"""Publish small monitoring JSON via gh; never touches trading code or an account."""
import argparse
import base64
import json
import os
import subprocess
import time
from pathlib import Path


def gh(args, data=None):
    p=subprocess.run(['gh','api',*args],input=json.dumps(data) if data is not None else None,
                     text=True,capture_output=True,timeout=40)
    if p.returncode:raise RuntimeError(p.stderr[:400])
    return json.loads(p.stdout or '{}')


def publish(local,remote,branch,repo):
    payload=json.loads(Path(local).read_text())
    # Only valid JSON snapshots; never manufacture metrics when the file is missing.
    content=json.dumps(payload,sort_keys=True)+'\n'
    endpoint=f'repos/{repo}/contents/{remote}'
    for _ in range(3):
        try:previous=gh([endpoint+'?ref='+branch]);sha=previous['sha']
        except Exception:sha=None
        body={'message':'data(horizon): publish evidence checkpoint [skip ci]', 'branch':branch,
              'content':base64.b64encode(content.encode()).decode()}
        if sha:body['sha']=sha
        try:return gh(['--method','PUT',endpoint,'--input','-'],body)
        except Exception:time.sleep(2)
    raise RuntimeError('checkpoint publication failed; local evidence remains intact')


def main():
    p=argparse.ArgumentParser();p.add_argument('--local',required=True);p.add_argument('--remote',required=True)
    p.add_argument('--watch',action='store_true');p.add_argument('--stop-file');args=p.parse_args()
    while True:
        if Path(args.local).is_file():
            try:publish(args.local,args.remote,os.environ['BRANCH'],os.environ['GITHUB_REPOSITORY'])
            except Exception as e:
                print(f'PUBLISH_ERROR: {e}',flush=True)
                if not args.watch:raise
        if not args.watch or (args.stop_file and Path(args.stop_file).exists()):break
        time.sleep(120)

if __name__=='__main__':main()
