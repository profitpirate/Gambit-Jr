"""One bounded forward-live window, with durable evidence and guarded continuation.

A successful time window below 100 per model is explicitly INCOMPLETE. It never
resets a prior account or resumes over an unclean/failed window. GitHub jobs,
not a chat process, host the live feed. No funded transaction API is present.
"""
from __future__ import annotations
import argparse,hashlib,json,os,shutil,subprocess,sys,time
from pathlib import Path
from golden_top4_engine import ARMS, write_json
from golden_top4_report import validate_state, report

PREFIX='docs/research/golden-top4-'

def prepare():
    contract=json.loads(Path('docs/research/golden-top4-experiment-contract.json').read_text())
    if contract['market_evidence']['axiom_market_visibility_required'] is not False:
        raise ValueError('direct-Solana source not authorized')
    if contract['models']!=list(ARMS) or contract['starting_sol_per_model']!=3:raise ValueError('changed model/balance requirements')
    cfg=json.loads(Path('config/golden-horizon.json').read_text())
    sources=list(Path('tools').glob('golden_top4_*.py'))+[Path('tools/golden_horizon_live.py'),Path('tools/golden_horizon_engine.py'),Path('tools/golden_buyer_reputation_core.py'),Path('tools/golden_management_v2.py')]
    cfg.update(campaign_name=contract['campaign_name'],models=list(ARMS),execution='PAPER_ONLY',
        target_per_model=100,market_source='DIRECT_SOLANA_NEW_CREATIONS',
        implementation_sha256={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(sources)},
        model_semantics='Three fixed controls and independent filtered FULL_3S_V2; at least 100 each, not forced matching',
        actual_axiom_execution=False, families=['FULL'],horizons_seconds=[3,4,7])
    cfg['completion'].update(same_mint_cohort_across_12_arms=False,minimum_verified_forward_per_model=100)
    cfg['limitations']=[x for x in cfg['limitations'] if 'twelve' not in x]
    dest=Path('config/golden-top4-live.json')
    rendered=json.dumps(cfg,sort_keys=True,indent=2)+'\n'
    if dest.exists() and dest.read_text()!=rendered:
        raise ValueError('registered manifest changed: start a separately authorized experiment')
    dest.write_text(rendered)
    return cfg


def command(args,log):
    with log.open('w') as f:
        return subprocess.call(args,stdout=f,stderr=subprocess.STDOUT)


def persist(paths):
    # Writes only explicit research evidence paths to the current campaign branch.
    keep=Path('artifacts/persist');keep.mkdir(parents=True,exist_ok=True)
    for name,src in paths.items():
        if src.exists():shutil.copyfile(src,keep/name)
    branch=os.environ['BRANCH']
    subprocess.run(['git','fetch','origin',branch],check=True)
    subprocess.run(['git','checkout','-B','top4-evidence','FETCH_HEAD'],check=True)
    written=[]
    for saved in keep.iterdir():
        dest=Path(PREFIX+saved.name);dest.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(saved,dest);written.append(str(dest))
    # The generated manifest is deterministic and frozen before the first entry.
    if Path('config/golden-top4-live.json').exists():written.append('config/golden-top4-live.json')
    subprocess.run(['git','config','user.name','gambit-top4-research'],check=True)
    subprocess.run(['git','config','user.email','actions@users.noreply.github.com'],check=True)
    subprocess.run(['git','add','--',*written],check=True)
    if subprocess.run(['git','diff','--cached','--quiet']).returncode:
        subprocess.run(['git','commit','-m','data(top4): preserve forward campaign evidence [skip ci]'],check=True)
        subprocess.run(['git','push','origin','HEAD:'+branch],check=True)


def launch(args):
    cfg=prepare();root=Path('artifacts/live');root.mkdir(parents=True,exist_ok=True)
    existing=Path(PREFIX+'state.json')
    resume=None;e4resume=None
    if existing.exists():
        previous=json.loads(existing.read_text());e=validate_state(previous,require_target=False)
        if previous['top4_campaign']!=cfg['campaign_name']:raise ValueError('different campaign checkpoint')
        if previous['manifest_hash']!=hashlib.sha256(Path('config/golden-top4-live.json').read_bytes()).hexdigest():
            raise ValueError('manifest mismatch; no account reset allowed')
        if min(e.forward_counts().values())>=100:
            print('ALREADY_AT_TARGET_NO_ADDITIONAL_RUN',flush=True);return 0
        resume=root/'resume.json';shutil.copyfile(existing,resume)
        prior=Path(PREFIX+'e4.json')
        if prior.exists():e4resume=root/'e4-resume.json';shutil.copyfile(prior,e4resume)
    readiness=json.loads(Path('artifacts/smoke/readiness.json').read_text())
    if readiness['status']!='SOLANA_AND_E4_PREFLIGHT_PASS':raise ValueError('read-only source preflight not passed')
    write_json(root/'status.json',{'status':'INITIALIZING_LIVE_WINDOW','checked_ns':time.time_ns(),
        'models':list(ARMS),'starting_sol_per_model':3,'resuming':bool(resume),
        'paper_only':True,'actual_profit_sol':None,'collector_active':False,
        'preflight':readiness,'run_id':os.environ.get('GITHUB_RUN_ID')})
    publog=(root/'publisher.log').open('w')
    publisher=subprocess.Popen([sys.executable,'tools/golden_top4_publish.py','--root',str(root)],stdout=publog,stderr=subprocess.STDOUT)
    deadline=time.monotonic()+90
    while time.monotonic()<deadline:
        hp=root/'publisher-health.json'
        if hp.exists() and json.loads(hp.read_text()).get('healthy'):break
        if publisher.poll() is not None:raise RuntimeError('reporter startup failed')
        time.sleep(1)
    else:
        publisher.terminate();raise RuntimeError('no acknowledged reporting channel')
    cmd=[sys.executable,'tools/golden_top4_solana.py','--production-root','production','--corpus',args.corpus,
         '--manifest','config/golden-top4-live.json','--output',str(root),'--duration',str(args.duration),
         '--window',os.environ.get('GITHUB_RUN_ID','local')]
    if resume:cmd+=['--resume',str(resume)]
    if e4resume:cmd+=['--e4-resume',str(e4resume)]
    rc=command(cmd,root/'live.log')
    (root/'stop-publisher').touch()
    try:pubrc=publisher.wait(timeout=120)
    except subprocess.TimeoutExpired:publisher.terminate();pubrc=2
    publog.close()
    paths={'status.json':root/'status.json','state.json':root/'state.json',
        'e4.json':root/'e4-status.json','activity.json':root/'activity.json',
        'publisher.json':root/'publisher-health.json','readiness.json':Path('artifacts/smoke/readiness.json')}
    error=None;finished=False
    try:
        if rc or pubrc:raise RuntimeError(f'collector={rc}, publisher={pubrc}; failed evidence is retained')
        s=json.loads((root/'state.json').read_text());e=validate_state(s,require_target=False)
        finished=min(e.forward_counts().values())>=100
        if finished:
            report(root/'state.json',Path('artifacts/final'),root/'e4-status.json',root/'publisher-health.json')
            paths.update({'final-results.json':Path('artifacts/final/results.json'),
                          'final-report.md':Path('artifacts/final/report.md'),
                          'complete.json':Path('artifacts/final/complete.json')})
        status=json.loads((root/'status.json').read_text())
        status.update(collector_active=False,completion_verified=finished,
            status='COMPLETE_100_PER_MODEL' if finished else 'CLEAN_WINDOW_END_INCOMPLETE',
            verified_forward_counts=e.forward_counts(),next_window_must_preserve_state=True)
        write_json(root/'status.json',status)
    except Exception as exc:
        error=str(exc)
        path=root/'status.json'
        status=json.loads(path.read_text()) if path.exists() else {}
        status.update(status='BLOCKED_PRESERVED_EVIDENCE',collector_active=False,
                      completion_verified=False,operational_blocker=error)
        write_json(path,status)
    persist(paths)
    if error:raise RuntimeError(error)
    print('CAMPAIGN_COMPLETE' if finished else 'CLEAN_WINDOW_END_NOT_COMPLETE',flush=True)
    return 0


def main():
    p=argparse.ArgumentParser();p.add_argument('--prepare',action='store_true');p.add_argument('--corpus')
    p.add_argument('--duration',type=int,default=14400);a=p.parse_args()
    if a.prepare:prepare();return 0
    if not a.corpus:p.error('--corpus is required')
    return launch(a)
if __name__=='__main__':raise SystemExit(main())
