#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Verify every saved operation and produce public-safe engineering study rows."""
import argparse
import ast
import csv
import hashlib
import json
import random
import statistics
from pathlib import Path

import analyze_campaign
import analyze_inference_cost
from engineering_stats import summarize
from mhforge import controls, engineering, util
from mhforge.paths import StatePaths
from mhforge.telemetry import join, plan_study


def prompt_examples(request, examples, limit):
    template = next(ast.literal_eval(n.value) for n in ast.parse(engineering.candidate_source(limit)).body
                    if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='PROMPT' for t in n.targets))
    prompt = next(m['content'] for m in reversed(request['messages']) if m['role']=='user')
    prefix, rest = template.split('{examples}'); middle, suffix = rest.split('{problem}')
    if not prompt.startswith(prefix) or not prompt.endswith(suffix): raise ValueError('prompt scaffold differs')
    body = prompt[len(prefix):-len(suffix)]
    examples_text, problem = body.rsplit(middle,1)
    ordered = list(examples)
    random.Random(int.from_bytes(hashlib.sha256(problem.encode()).digest()[:8],'big')).shuffle(ordered)
    parts=[]; chars=0
    for ex in ordered[:limit]:
        part='Q: '+ex.get('raw_question',ex['input'])+'\nA: '+ex['target']
        extra=len(part)+(2 if parts else 0)
        if chars+extra>30000: break
        parts.append(part);chars+=extra
    if '\n\n'.join(parts)!=examples_text: raise ValueError('example membership, order or cap differs')
    return len(parts),len(examples_text)


def inspect(root, output, *, offline=False):
    paths=StatePaths(root); manifest=util.read_json(paths.manifest)
    if manifest['routing']['reply_reuse_scope']!='job': raise ValueError('independent arms require job-scoped replay')
    if offline and manifest['provider']['kind']!='stub': raise ValueError('offline analysis requires a stub manifest')
    def url(jid):
        if offline: return 'offline:unverified:'+jid
        receipt=util.read_json(paths.receipt_file('job:'+jid,'agent_lens'))
        return receipt['urls'][0] if receipt['status']=='verified' else None
    _,_,evidence,native=plan_study.engineering(paths,manifest,url)
    grader=controls.load_grader(paths)
    groups={}; identities={}; private=[]; timings=[]
    for ev in evidence:
        key=ev['job_key']; job=util.read_json(paths.job_dir(key)/'job.json')
        ds,system,sample=job['dataset'],job['candidate'],job['sample']
        jid=f"{key}.a{ev['attempt']}"
        raw=util.read_jsonl(paths.attempt_dir(key,ev['attempt'])/'out/sidecar.jsonl')
        final=controls.final_evals(raw)
        calls={c['call_id']:c for c in (util.read_json(p) for p in paths.calls_dir(jid).glob('*.json'))}
        joined={r['index']:r for r in join.join_examples(join.parse_sidecar(raw),list(calls.values()),dataset=ds,split='test')}
        valjob=util.read_json(paths.job_dir(f'val.{system}.{ds}')/'job.json')
        va=next(a for a in reversed(valjob['attempts']) if a['outcome']=='ok')
        memory_path=paths.attempt_dir(valjob['job_key'],va['attempt'])/'out/memory.json'
        digest=util.sha256_file(memory_path)
        attempt=next(a for a in job['attempts'] if a['attempt']==ev['attempt'])
        if digest!=va['outputs']['memory.json'] or digest!=attempt['inputs']['memory.json']:
            raise ValueError('training memory source differs from the executed test input')
        if attempt['candidate_sha256']!=util.sha256_bytes(engineering.candidate_source(engineering.ARMS[system][0]).encode()):
            raise ValueError('executed candidate differs from the frozen treatment')
        memory=util.read_json(memory_path)
        limit,temp=engineering.ARMS[system]
        for i,r in final.items():
            j=joined[i]
            if not j['matched'] or j['ambiguous'] or j['n_calls']!=1: raise ValueError('ambiguous operation join')
            call=calls[j['calls'][0]['call_id']]; request=call['request']
            if util.sha256_bytes(util.canonical_json(request))!=call['request_sha256']: raise ValueError('request digest differs')
            if request.get('temperature')!=temp or 'seed' in request: raise ValueError('solver parameters differ')
            if call['reuse_salt']!='s'+str(sample) or call['job']['sample']!=sample: raise ValueError('sample namespace differs')
            count,chars=prompt_examples(request,memory['examples'],limit)
            content=call['response']['choices'][0]['message']['content'] or ''
            try:
                parsed=json.loads(content);valid=isinstance(parsed,dict) and isinstance(parsed.get('final_answer'),str)
            except (ValueError,TypeError):valid=False
            label=controls.vote_label(ds,r['prediction'],grader.extract_final_answer)
            label_hash=util.sha256_bytes(util.canonical_json(label))
            row={'system':system,'dataset':ds,'index':i,'sample':sample,'correct':int(r['was_correct']),
                 'empty':int(not r['prediction_chars']),'strict_json_valid':int(valid),'examples':count,'example_chars':chars,
                 '_label':label_hash,'_request':call['request_sha256']}
            groups.setdefault((system,ds,i),[]).append(row)
            old=identities.setdefault(system,{}).setdefault(ds,{}).setdefault(i,r)
            if old['example_sha256']!=r['example_sha256']: raise ValueError('draw task identity differs')
            private.append({'system':system,'dataset':ds,'index':i,'sample':sample,'call_id':call['call_id'],
                            'job_id':jid,'sidecar_seq':r['seq'],'trace_url':url(jid),'request_sha256':call['request_sha256']})
            timings.append({'system':system,'dataset':ds,'sample':sample,'queue_s':call['queue_s'],
                            'received_utc':call['received_utc'],'ended_utc':call['ended_utc'],'outcome':call['outcome']})
    rows=[]
    for key,group in sorted(groups.items()):
        if len({r['_request'] for r in group})!=1: raise ValueError('requests differ across five draws')
        labels={}
        for r in sorted(group,key=lambda r:r['sample']):
            label=r.pop('_label');r.pop('_request')
            r['label_class']=labels.setdefault(label,len(labels));rows.append(r)
    for system,arm in identities.items():analyze_campaign.verify_source_identities(root,manifest,arm)
    for arm in identities.values():
        for ds,group in arm.items():
            if any(r['example_sha256']!=identities['context_all'][ds][i]['example_sha256'] for i,r in group.items()):raise ValueError('cross-arm task identity differs')
    costs={}
    for system in engineering.ARMS:
        report=analyze_inference_cost.inspect(root,system,{'controls'})
        costs[system]={'all_jobs':report['totals'],'policies':{}}
        for k in (1,3,5):
            jobs=[j for j in report['jobs'] if j['job_key'].startswith('test.') and j['sample']<=k]
            costs[system]['policies'][str(k)]={key:sum(j[key] for j in jobs) for key in ('usage_priced_micro_usd','unknown_cost_upper_micro_usd')}
        util.write_json(output/(system+'-cost-private.json'),report)
    summary=summarize(rows)
    for system,result in summary['systems'].items():
        if abs(result['mean_accuracy_percent']/100-native[f'engineering/{system}/macro_mean_accuracy'])>1e-12:raise ValueError('native table mean differs')
        for k,v in result['vote_accuracy_percent'].items():
            if abs(v/100-native[f'engineering/{system}/macro_vote{k}_accuracy'])>1e-12:raise ValueError('native table vote differs')
    summary.update(trace_readback_verified=not offline,provider=manifest['provider']['kind'],costs=costs,plan=util.read_json(paths.root/'engineering/plan.json'),
                   sensitivity=summarize(rows,exclude_overlap=True),all_outputs_regraded=len(rows),
                   identical_request_groups=len(groups),cost_scope='Observed frozen-price inference usage; all attempts retained; prefix costs are attributed estimates, all five samples were executed and paid for.',
                   latency_scope='Request duration and queue delay only; no end-to-end deployed policy latency estimate.')
    util.write_json(output/'engineering-summary.json',summary,fsync=True)
    util.write_json(output/'engineering-evidence-private.json',{'sources':evidence,'operations':private,'request_timings':timings},fsync=True)
    with (output/'engineering-scores.csv').open('w',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]),lineterminator='\n');w.writeheader();w.writerows(rows)
    return summary


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--state',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--offline-smoke',action='store_true')
    a=p.parse_args();a.output.mkdir(parents=True,exist_ok=True)
    result=inspect(a.state,a.output,offline=a.offline_smoke)
    print(json.dumps({k:result[k] for k in ('systems','paired','costs','all_outputs_regraded')},indent=2))
