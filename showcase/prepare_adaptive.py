#!/usr/bin/env python3
"""Freeze verified engineering-E request bytes for a prospective operational study."""
import argparse, shutil
from pathlib import Path
import analyze_campaign
from adaptive_core import PLAN,digest,schedule,runtime_hashes
from mhforge import controls,util,manifest as mm,qualification
from mhforge.paths import StatePaths,resolve_state
from mhforge.telemetry import join

def prepare(source,template,output,campaign,smoke=False,offline=False):
    source=StatePaths(Path(source).resolve()); output=Path(output).resolve()
    m=util.read_json(template); m['campaign']=campaign;m['state_dir']='state/'+campaign
    m['notes']='Prospective fixed-policy operational comparison on previously inspected tasks. No proposer. Reviewed host orchestration; no cloud or worker isolation claim. See design/adaptive-study.md.'
    m['gateway']['max_in_flight']=PLAN['concurrent_calls']
    m['prices']['checked_utc']='2026-11-11T00:00:00.000000Z'
    m['budgets']['solver_usd']={'total':'50.00','phases':{'qualification':'5.00','calibration':'1.00','baseline':'1.00','search':'1.00','test':'1.00','controls':'35.00','recovery':'5.00'}}
    m['budgets']['lens_ingest_split_mb']={'main':5,'preflight':150,'recovery':20}
    if offline:m['provider']['kind']='stub';m['provider']['base_url']='http://127.0.0.1:1/v1'
    mm.require_valid(m,output)
    paths=resolve_state(m,output)
    if paths.root.exists():raise ValueError('Refusing to replace an existing campaign')
    src_manifest=util.read_json(source.manifest); pristine=util.read_json(source.pristine)
    grader=controls.load_grader(source);tasks={};identities={}
    for ds in m['upstream']['expect']['datasets']:
        key=f'test.context_all.{ds}.s1';job=util.read_json(source.job_dir(key)/'job.json')
        attempt=next(a for a in reversed(job['attempts']) if a['outcome']=='ok')
        side=source.attempt_dir(key,attempt['attempt'])/'out/sidecar.jsonl'
        if util.sha256_file(side)!=attempt['outputs']['sidecar.jsonl']:raise ValueError('Source sidecar changed')
        raw=util.read_jsonl(side);final=controls.final_evals(raw);identities[ds]=final
        calls={c['call_id']:c for c in map(util.read_json,source.calls_dir(attempt['job_id']).glob('*.json'))}
        joined={r['index']:r for r in join.join_examples(join.parse_sidecar(raw),list(calls.values()),dataset=ds,split='test')}
        tasks[ds]={}
        for i,r in final.items():
            j=joined[i]
            if not j['matched'] or j['ambiguous'] or j['n_calls']!=1:raise ValueError('Ambiguous source call')
            c=calls[j['calls'][0]['call_id']]
            if digest(c['request'])!=c['request_sha256'] or c['request']['temperature']!=0:raise ValueError('Request differs')
            if util.sha256_bytes(r['target'].encode())!=r['target_sha256']:raise ValueError('Target changed')
            score=grader.get_evaluator(ds)(r['prediction'],r['target'])
            if bool(score['was_correct'] if isinstance(score,dict) else score)!=r['was_correct']:raise ValueError('Source regrade differs')
            tasks[ds][str(i)]={k:r[k] for k in ('target','target_sha256','example_sha256')}
            tasks[ds][str(i)].update(request=c['request'],request_sha256=c['request_sha256'],source_call_id=c['call_id'],source_job_id=c['job']['job_id'],source_sidecar_sha256=util.sha256_file(side))
    analyze_campaign.verify_source_identities(source.root,src_manifest,identities)
    if smoke:tasks={ds:{'0':rows['0']} for ds,rows in tasks.items()}
    paths.ws.mkdir(parents=True)
    for rel,sha in pristine['files'].items():
        p=source.ws/rel
        if util.sha256_file(p)!=sha:raise ValueError('Pinned source changed: '+rel)
        dest=paths.ws/rel;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,dest)
    util.write_json(paths.pristine,pristine)
    util.write_json(output,m)
    mm.freeze(m,paths,qualification.engine_snapshot(Path(__file__).parent/'mhforge'))
    packed=paths.root/'adaptive/inputs-private.json'
    util.write_json(packed,{'source_campaign':src_manifest['campaign'],'source_manifest_sha256':mm.manifest_sha256(src_manifest),'tasks':tasks},fsync=True)
    contract={'plan':PLAN,'cohort':'smoke' if smoke else 'measured','inputs_sha256':util.sha256_file(packed),'runtime_hashes':runtime_hashes(Path(__file__).parent),'schedule':schedule(tasks),'source_manifest_sha256':mm.manifest_sha256(src_manifest),'frozen_utc':util.utc_now()}
    util.write_json(paths.root/'adaptive/contract.json',contract,fsync=True)
    print({'campaign':campaign,'tasks':sum(map(len,tasks.values())),'decisions':sum(len(b['indices']) for b in contract['schedule']),'contract_sha256':digest(contract)})

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--source',required=True);p.add_argument('--template',required=True);p.add_argument('--output',required=True);p.add_argument('--campaign',required=True);p.add_argument('--smoke',action='store_true');p.add_argument('--offline',action='store_true');a=p.parse_args();prepare(**vars(a))
