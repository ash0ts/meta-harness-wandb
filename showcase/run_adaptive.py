#!/usr/bin/env python3
"""Execute frozen concurrent-five and conditional-three-then-two policies."""
import argparse, json, os, subprocess, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from adaptive_core import PLAN,RecordingIds,Heartbeat,verify_inputs,load_extract,request_for,execute_policy,digest
from mhforge import controls,util,wire,manifest as mm,cli
from mhforge.circuit import Circuit
from mhforge.ledger import Ledger
from mhforge.replay import ReplayStore
from mhforge.gateway import Gateway
from mhforge.provider import ForgeProviderClient,ProviderResult
from mhforge.state import StateLock

class OfflineProvider:
    def complete(self,body,**kwargs):
        time.sleep(.001)
        merged={'id':'offline-'+os.urandom(8).hex(),'object':'chat.completion','model':'openai/gpt-oss-120b','choices':[{'index':0,'message':{'role':'assistant','content':json.dumps({'final_answer':'offline controlled answer'})},'finish_reason':'stop'}],'usage':{'prompt_tokens':100,'completion_tokens':10,'total_tokens':110}}
        return ProviderResult(200,{},merged,True,None,.001,.001,len(json.dumps(merged)),None)

def record_path(paths,block,index):return paths.root/'adaptive/decisions'/block['block_id']/f'{index:04}.json'

def run(m,paths,contract,inputs,keys,*,offline=False,max_blocks=None):
    ids=RecordingIds();ledger=Ledger(paths.ledger,m)
    ledger.interrupt_pending();ledger.close_open_jobs()
    gateway=Gateway(m,paths,provider=OfflineProvider() if offline else ForgeProviderClient(m,environ=dict(keys)),ledger=ledger,replay=ReplayStore(paths.replay,m['routing']['reply_reuse_scope']),circuit=Circuit(paths.circuit,threshold=m['provider']['circuit']['failures'],cooldown_s=m['provider']['circuit']['cooldown_s']),ids=ids)
    extract=load_extract(paths);grader=controls.load_grader(paths)
    # An interrupted started policy is retained, never replayed into a latency sample.
    for p in (paths.root/'adaptive/decisions').glob('*/*.json'):
        r=util.read_json(p)
        if r['status']=='started':
            r.update(status='interrupted',correct=0,latency_s=None,ended_utc=util.utc_now(),end_epoch=time.time())
            util.write_json(p,r,fsync=True)
    with ThreadPoolExecutor(max_workers=PLAN['concurrent_calls']) as calls, ThreadPoolExecutor(max_workers=PLAN['concurrent_tasks']) as tasks, Heartbeat(paths.root/'adaptive/pauses.jsonl'):
        def task(block,index):
            task_input=inputs['tasks'][block['dataset']][str(index)];p=record_path(paths,block,index)
            if p.exists():return util.read_json(p)
            if ledger.halted():raise RuntimeError('Campaign halted; no new policy admission')
            start=time.time();tick=time.monotonic();ds=block['dataset']
            base={k:block[k] for k in ('block_id','pair_id','dataset','temperature','repeat','policy')}
            base.update(index=index,status='started',started_utc=util.utc_iso(start),start_epoch=start,example_sha256=task_input['example_sha256'],target_sha256=task_input['target_sha256'],request_sha256=digest(request_for(task_input,block['temperature'])),samples=[])
            util.write_json(p,base,fsync=True)
            jobs=[]
            for pos in range(1,6):
                j=wire.JobIdentity(m['campaign'],'test',f"{block['policy']}_t{block['temperature']}_i{index:04}",ds,1,'controls',None,repeat=None if block['repeat']==1 else block['repeat'],sample=pos)
                gateway.register_job(j);jobs.append(j)
            def sample(pos):
                j=jobs[pos-1];request=request_for(task_input,block['temperature'])
                reply=gateway.handle(j,wire.make_envelope('POST','/v1/chat/completions',util.canonical_json(request)))
                cid=ids.local.call_id;cpath=paths.calls_dir(j.job_id)/(cid+'.json');c=util.read_json(cpath)
                if c['request_sha256']!=base['request_sha256'] or c['job']['sample']!=pos:raise ValueError('Call identity changed')
                if c['outcome'] in ('replayed','coalesced'):raise ValueError('Unexpected response reuse')
                ok=c['outcome']=='completed' and reply.status==200
                content=(c['response']['choices'][0]['message']['content'] or '') if ok else ''
                prediction=extract(content,'final_answer') if ok else ''
                label=controls.vote_label(ds,prediction,grader.extract_final_answer)
                return {'position':pos,'ok':ok,'label':label,'prediction':prediction,'call_id':cid,'job_id':j.job_id,'call_sha256':util.sha256_file(cpath),'outcome':c['outcome']}
            def batch(positions):
                futures=[calls.submit(sample,pos) for pos in positions]
                return [f.result() for f in futures]
            samples,selected,status=execute_policy(block['policy'],batch)
            # End latency at selection, before grading, disk persistence or closing scopes.
            latency=time.monotonic()-tick;end=time.time()
            result=grader.get_evaluator(ds)(samples[selected]['prediction'],task_input['target']) if selected is not None else False
            correct=bool(result['was_correct'] if isinstance(result,dict) else result)
            base.update(samples=samples,status=status,chosen_position=None if selected is None else samples[selected]['position'],early_stop=status=='complete' and len(samples)==3,correct=int(correct),latency_s=latency,ended_utc=util.utc_iso(end),end_epoch=end)
            util.write_json(p,base,fsync=True)
            for j in jobs:
                if gateway.close_job(j.job_id,drain_timeout_s=1)['in_flight_at_close']:raise ValueError('Call scope not drained')
            return base
        completed_blocks=0
        for ordinal,block in enumerate(contract['schedule']):
            marker=paths.root/'adaptive/blocks'/(block['block_id']+'.json')
            if marker.exists():continue
            if ledger.halted():raise RuntimeError('Campaign halted; no new block admission')
            futures=[tasks.submit(task,block,i) for i in block['indices']]
            rows=[f.result() for f in futures]
            util.write_json(marker,{'block':block,'decisions':[str(record_path(paths,block,i).relative_to(paths.root)) for i in block['indices']]},fsync=True)
            print(json.dumps({'event':'block_complete','block':ordinal+1,'of':len(contract['schedule']),'decisions':len(rows),'policy':block['policy'],'dataset':block['dataset']}),flush=True)
            completed_blocks+=1
            if max_blocks is not None and completed_blocks>=max_blocks:break
    if all((paths.root/'adaptive/blocks'/(b['block_id']+'.json')).exists() for b in contract['schedule']):
        util.write_json(paths.root/'adaptive/completed.json',{'completed_utc':util.utc_now(),'contract_sha256':digest(contract)},fsync=True)
    return 0

def main(argv=None):
    p=argparse.ArgumentParser();p.add_argument('--manifest',type=Path,required=True);p.add_argument('--key-source');p.add_argument('--offline',action='store_true');p.add_argument('--max-blocks',type=int);p.add_argument('--export-only',action='store_true');a=p.parse_args(argv)
    m,paths=mm.load_frozen(a.manifest.resolve());contract,inputs=verify_inputs(paths,Path(__file__).parent)
    if a.offline!=(m['provider']['kind']=='stub'):raise ValueError('Provider mode mismatch')
    keys={} if a.offline else cli.read_keys(('fd',int(a.key_source.split(':',1)[1])),('provider','export'),m,{})
    with StateLock(paths.runner_lock,'adaptive'):
        if a.export_only:
            from adaptive_export import export_all
            return export_all(m,paths,keys,offline=a.offline)
        awake=None
        if not a.offline:awake=subprocess.Popen(['/usr/bin/caffeinate','-i','-s','-w',str(os.getpid())],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:return run(m,paths,contract,inputs,keys,offline=a.offline,max_blocks=a.max_blocks)
        finally:
            if awake is not None:awake.terminate();awake.wait()

if __name__=='__main__':raise SystemExit(main())
