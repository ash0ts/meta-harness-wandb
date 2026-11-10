#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Audit all five samples and diagnose agreement, ties, and aggregation losses.

The treatment is the frozen five-sample rule. Comparison with sample one and
the oracle ceiling are secondary diagnostics, not new selection policies.
"""
import argparse
from collections import Counter
from pathlib import Path

import analyze_campaign as analysis
from mhforge import controls, util
from mhforge.paths import StatePaths
from mhforge.telemetry import join, plan_study


def inspect(root):
    paths=StatePaths(root); manifest=util.read_json(paths.manifest)
    expected={ds:sizes[2] for ds,sizes in manifest['upstream']['expect']['sizes'].items()}
    def url(jid):
        receipt=util.read_json(paths.receipt_file('job:'+jid,'agent_lens'))
        return receipt['urls'][0] if receipt['status']=='verified' else None
    _,_,evidence,_=plan_study.voting(paths,manifest,url)
    arm,_=analysis.read_arm(root,'fewshot_vote5',expected)
    analysis.verify_source_identities(root,manifest,arm)
    grader=controls.load_grader(paths)
    samples={}; tasks=[]; baseline={}; dataset_stats={}
    for ev in evidence:
        key=ev['job_key']; job=util.read_json(paths.job_dir(key)/'job.json'); ds=job['dataset']
        sample=job['sample']; jid=f"{key}.a{ev['attempt']}"
        raw=util.read_jsonl(paths.attempt_dir(key,ev['attempt'])/'out/sidecar.jsonl')
        final=controls.final_evals(raw)
        calls={c['call_id']:c for c in (util.read_json(p) for p in paths.calls_dir(jid).glob('*.json'))}
        joined={r['index']:r for r in join.join_examples(join.parse_sidecar(raw),list(calls.values()),dataset=ds,split='test')}
        rows={}
        for i,row in final.items():
            j=joined[i]
            if not j['matched'] or j['ambiguous'] or j['n_calls']!=1:
                raise ValueError('sample lacks an unambiguous single-operation join')
            call=calls[j['calls'][0]['call_id']]; request=call['request']
            if util.sha256_bytes(util.canonical_json(request))!=call['request_sha256']:
                raise ValueError('sample request body digest differs')
            if request.get('temperature')!=controls.PLAN['temperature'] or 'seed' in request:
                raise ValueError('sample provider settings differ')
            if call['reuse_salt']!='s'+str(sample) or call['job']['sample']!=sample:
                raise ValueError('sample replay namespace differs')
            label=controls.vote_label(ds,row['prediction'],grader.extract_final_answer)
            rows[i]={'sample':sample,'example_sha256':row['example_sha256'], 'was_correct':row['was_correct'],
                     'prediction_sha256':row['prediction_sha256'],
                     'normalized_label_sha256':util.sha256_bytes(util.canonical_json(label)),
                     'request_sha256':call['request_sha256'],'call_id':call['call_id'],
                     'call_outcome':call['outcome'],'trace_url':url(jid),'sidecar_seq':row['seq'],
                     'source':ev,'empty_final_response':row['prediction_chars']==0}
        samples.setdefault(ds,{})[sample]=rows
    for ds,n in expected.items():
        baseline[ds]=samples[ds][1]
        counts=Counter(); agreement={}
        for i in range(n):
            rows=[samples[ds][s][i] for s in range(1,6)]; voted=arm[ds][i]
            if len({r['request_sha256'] for r in rows})!=1:
                raise ValueError('request bodies differ between samples of the same task')
            labels=Counter(r['normalized_label_sha256'] for r in rows)
            largest=max(labels.values()); selected=next(r for r in rows if labels[r['normalized_label_sha256']]==largest)
            if voted['selected_sample']!=selected['sample'] or voted['votes']!=largest:
                raise ValueError('stored selection differs from independently rebuilt label vote')
            correct_samples=sum(r['was_correct'] for r in rows); correct=voted['was_correct']
            group='both_correct' if correct and rows[0]['was_correct'] else 'gain' if correct else 'loss' if rows[0]['was_correct'] else 'both_wrong'
            counts[group]+=1; counts['any_correct']+=bool(correct_samples)
            counts['all_wrong']+=correct_samples==0; counts['all_correct']+=correct_samples==5
            counts['vote_missed_available_correct']+=bool(correct_samples) and not correct
            counts['unanimous_wrong']+=largest==5 and not correct
            counts['tied']+=voted['tied']; counts['correct']+=correct
            cell=agreement.setdefault(str(largest),{'tasks':0,'correct':0});cell['tasks']+=1;cell['correct']+=correct
            tasks.append({'dataset':ds,'index':i,'example_sha256':rows[0]['example_sha256'],
                          'selected_sample':selected['sample'],'votes':largest,'tied':voted['tied'],
                          'was_correct':correct,'correct_samples':correct_samples,'versus_first_sample':group,'samples':rows})
        dataset_stats[ds]={'total':n,**dict(counts),'agreement':agreement,
                          'sample_correct':[sum(r['was_correct'] for r in samples[ds][s].values()) for s in range(1,6)]}
    macro=lambda key:sum(v[key]/v['total'] for v in dataset_stats.values())/len(dataset_stats)
    return {'schema':'mhforge.voting-analysis/1','campaign':manifest['campaign'],'plan':controls.PLAN,
            'tasks':tasks,'datasets':dataset_stats,'source_evidence':evidence,
            'all_sample_outcomes_regraded':sum(expected.values())*5,
            'identical_request_groups':len(tasks),'vote_macro_accuracy':macro('correct'),
            'oracle_any_correct_macro':macro('any_correct'),
            'oracle_caveat':'Diagnostic ceiling requiring the true answer to choose among samples; not an executable inference policy.',
            'vote_vs_first_sample':analysis.paired(baseline,arm),
            'secondary_analysis':'Agreement, oracle ceiling and comparison with sample one were chosen for analysis after execution began. No scores or selection rules were changed.',
            'limitation':'The primary temperature-zero baseline comparison changes both temperature and sample count. Sample-one comparison isolates aggregation within these five draws but does not establish sampling-population performance.',
            'script_sha256':util.sha256_file(Path(__file__))}


if __name__ == '__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--state',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);a=p.parse_args();result=inspect(a.state)
    util.write_json(a.output,result,fsync=True)
    print({k:result[k] for k in ('datasets','all_sample_outcomes_regraded','identical_request_groups','vote_macro_accuracy','oracle_any_correct_macro')})
