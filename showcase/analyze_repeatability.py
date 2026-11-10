#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Reconcile repeated validation outcomes, request identities and released graders."""
import argparse
import random
from pathlib import Path

import analyze_campaign as analysis
from mhforge import controls, util
from mhforge.paths import StatePaths
from mhforge.telemetry import join, plan_study


def inspect(root):
    paths=StatePaths(root);m=util.read_json(paths.manifest)
    def url(job):
        rec=util.read_json(paths.receipt_file('job:'+job,'agent_lens'))
        return rec['urls'][0] if rec['status']=='verified' else None
    _,_,evidence,_=plan_study.noise(paths,m,url)
    grader=controls.load_grader(paths);pins=util.read_json(paths.pristine)['files']
    groups={};regraded=0
    for ev in evidence:
        job=util.read_json(paths.job_dir(ev['job_key'])/'job.json');ds=job['dataset'];name=job['candidate']
        repeat=job.get('repeat') or 1;jid=f"{ev['job_key']}.a{ev['attempt']}"
        argv=job['inner_argv']
        if float(argv[argv.index('--temperature')+1])!=m['upstream']['expect']['temperature']:
            raise ValueError('repeat temperature differs')
        rel='data/'+m['upstream']['expect']['dataset_dirs'][ds]+'/val.jsonl'
        if util.sha256_file(paths.ws/rel)!=pins[rel]:raise ValueError('validation source changed')
        source=util.read_jsonl(paths.ws/rel);random.Random(m['upstream']['expect']['seed']).shuffle(source)
        raw=util.read_jsonl(paths.attempt_dir(ev['job_key'],ev['attempt'])/'out/sidecar.jsonl')
        final=controls.final_evals(raw,split='val')
        calls={c['call_id']:c for c in (util.read_json(p) for p in paths.calls_dir(jid).glob('*.json'))}
        joined={r['index']:r for r in join.join_examples(join.parse_sidecar(raw),list(calls.values()),dataset=ds,split='val')}
        for i,row in final.items():
            if row['example_sha256']!=analysis.source_identity(ds,source[i]):raise ValueError('repeat task identity differs')
            judge=grader.get_evaluator(ds)(row['prediction'],row['target'])
            correct=judge['was_correct'] if isinstance(judge,dict) else judge
            if bool(correct)!=row['was_correct']:raise ValueError('host repeat grade differs')
            regraded+=1;j=joined[i]
            if not j['matched'] or j['ambiguous'] or j['n_calls']!=1:
                raise ValueError('baseline repeat lacks an unambiguous single-call join')
            call=calls[j['calls'][0]['call_id']]
            request_sha=call.get('request_sha256')
            if not isinstance(request_sha,str) or len(request_sha)!=64:raise ValueError('request digest missing')
            request=call['request']
            if util.sha256_bytes(util.canonical_json(request))!=request_sha:raise ValueError('request body digest differs')
            if request.get('temperature')!=m['upstream']['expect']['temperature'] or 'seed' in request:
                raise ValueError('provider temperature or seed protocol differs')
            label=controls.vote_label(ds,row['prediction'],grader.extract_final_answer)
            groups.setdefault((name,ds,i),[]).append({'repeat':repeat,'was_correct':row['was_correct'],
                'example_sha256':row['example_sha256'],'request_sha256':request_sha,
                'prediction_sha256':row['prediction_sha256'],'normalized_label_sha256':util.sha256_bytes(util.canonical_json(label)),
                'call_id':call['call_id'],'outcome':call['outcome'],'trace_url':url(jid)})
    tasks=[]
    repeats=m['calibration']['noise_repeats']
    for (name,ds,i),rows in sorted(groups.items()):
        if sorted(r['repeat'] for r in rows)!=list(range(1,repeats+1)):raise ValueError('repeat membership differs')
        tasks.append({'system':name,'dataset':ds,'index':i,'same_request_all_repeats':len({r['request_sha256'] for r in rows})==1,
            'correctness_changed':len({r['was_correct'] for r in rows})>1,
            'normalized_label_changed':len({r['normalized_label_sha256'] for r in rows})>1,'repeats':rows})
    expected=sum(sizes[1] for sizes in m['upstream']['expect']['sizes'].values())*len(m['upstream']['expect']['baselines'])
    if len(tasks)!=expected or regraded!=expected*repeats:raise ValueError('repeat study is incomplete')
    noise=util.read_json(paths.noise)
    summary={name:{'macro_min':min(values),'macro_max':max(values),'macro_std':noise['macro_std'][name]}
             for name,values in noise['macro_validation_accuracy'].items()}
    return {'schema':'mhforge.repeatability-analysis/1','campaign':m['campaign'],'temperature':m['upstream']['expect']['temperature'],
        'repeats':repeats,'task_system_groups':len(tasks),'task_outcomes_regraded':regraded,
        'groups_with_identical_requests':sum(t['same_request_all_repeats'] for t in tasks),
        'groups_with_correctness_changes':sum(t['correctness_changed'] for t in tasks),
        'groups_with_normalized_label_changes':sum(t['normalized_label_changed'] for t in tasks),
        'macro_validation_accuracy':summary,'evidence':evidence,'tasks':tasks,
        'limitation':'Five repetitions describe this endpoint and these tasks; they do not estimate universal model variance. Seed controls loader shuffle, not a provider API seed.',
        'script_sha256':util.sha256_file(Path(__file__))}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--state',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);a=p.parse_args();doc=inspect(a.state)
    util.write_json(a.output,doc,fsync=True)
    print({k:v for k,v in doc.items() if k not in ('tasks','evidence')})
