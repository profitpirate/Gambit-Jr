"""Out-of-process acknowledged GitHub reporting. No trading or signing calls.

Each model event is retained; publication can batch events within a polling cycle.
A delivery failure closes new admissions, never existing-position management.
"""
from __future__ import annotations
import argparse,base64,hashlib,json,os,subprocess,time
from pathlib import Path
from urllib.parse import quote
from golden_top4_engine import write_json

PREFIX='docs/research/golden-top4-'

def api(method,path,data=None,allow404=False):
    cmd=['gh','api','--method',method,path]
    if data is not None:cmd+=['--input','-']
    r=subprocess.run(cmd,input=json.dumps(data) if data is not None else None,text=True,capture_output=True,timeout=35)
    if r.returncode:
        if allow404 and 'HTTP 404' in r.stderr:return None
        raise RuntimeError('GITHUB_PUBLISH_REQUEST_FAILED:'+r.stderr[:180])
    return json.loads(r.stdout or '{}')


def put(path,obj,repo,branch):
    content=json.dumps(obj,sort_keys=True,allow_nan=False)+'\n'
    endpoint=f'repos/{repo}/contents/{path}'
    for retry in range(3):
        old=api('GET',endpoint+'?ref='+quote(branch,safe=''),allow404=True)
        data={'branch':branch,'message':'data(top4): publish live evidence [skip ci]',
              'content':base64.b64encode(content.encode()).decode()}
        if old:data['sha']=old['sha']
        try:
            result=api('PUT',endpoint,data)
            sha=hashlib.sha1(b'blob '+str(len(content.encode())).encode()+b'\0'+content.encode()).hexdigest()
            if result['content']['sha']!=sha:raise RuntimeError('PUBLICATION_HASH_MISMATCH')
            return {'commit':result['commit']['sha'],'content_sha':sha,'acknowledged_ns':time.time_ns()}
        except Exception:
            if retry==2:raise
            time.sleep(1+retry)


def cycle(root,cache,repo,branch,*,force=False):
    ack={};now=time.monotonic()
    # Publish all new per-action records before reporting a healthy delivery state.
    files=[('activity.json','activity.json',0),('e4-status.json','e4.json',15),('status.json','status.json',30)]
    for name,remote,min_interval in files:
        path=root/name
        if not path.exists():continue
        raw=path.read_bytes();digest=hashlib.sha256(raw).hexdigest()
        old=cache.get(name,{})
        if digest==old.get('digest') or not force and now-old.get('time',0)<min_interval:continue
        obj=json.loads(raw);ack[remote]=put(PREFIX+remote,obj,repo,branch)
        cache[name]={'digest':digest,'time':now}
        if name=='activity.json':cache['last_event']=obj['count']
    health={'healthy':True,'checked_ns':time.time_ns(),'last_acknowledged_event_id':cache.get('last_event',0),
            'destination':'GITHUB_REPOSITORY_EVIDENCE_FEED','delivery_acknowledgements':ack,
            'delivery_is_async_not_subsecond_chat':True}
    write_json(root/'publisher-health.json',health)
    return health


def main():
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);a=p.parse_args()
    repo=os.environ['GITHUB_REPOSITORY'];branch=os.environ['BRANCH'];root=a.root
    cache={};last_control=0.0;last_remote=0.0
    try:
        put(PREFIX+'publisher.json',{'status':'DELIVERY_CHANNEL_READY','checked_ns':time.time_ns(),
            'run_id':os.environ.get('GITHUB_RUN_ID'),'new_trades_claimed':False},repo,branch)
    except Exception as exc:
        write_json(root/'publisher-health.json',{'healthy':False,'checked_ns':time.time_ns(),'error':str(exc)})
        raise
    while True:
        finishing=(root/'stop-publisher').exists()
        try:
            cycle(root,cache,repo,branch,force=finishing)
            if time.monotonic()-last_control>=30:
                control=api('GET',f'repos/{repo}/contents/{PREFIX}control.json?ref='+quote(branch,safe=''),allow404=True)
                if control:
                    payload=json.loads(base64.b64decode(control['content']))
                    if payload.get('stop_requested') is True:(root/'stop-request').touch()
                last_control=time.monotonic()
        except Exception as exc:
            write_json(root/'publisher-health.json',{'healthy':False,'checked_ns':time.time_ns(),
                'last_acknowledged_event_id':cache.get('last_event',0),'error':str(exc)})
            print('PUBLISH_FAILURE '+str(exc),flush=True)
            if finishing:raise
        if finishing:break
        time.sleep(2)

if __name__=='__main__':main()
