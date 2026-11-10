#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Assemble the reconciled baseline, repetition and voting evidence for review."""
import argparse
import csv
import json
from pathlib import Path

from mhforge import util


def assemble(root):
    names = ('calibration-paired-analysis.json','voting-paired-analysis.json',
             'validation-repeatability.json','voting-diagnostics.json',
             'calibration-no-memory-cost.json','calibration-fewshot-cost.json','voting-inference-cost.json')
    docs = {name:util.read_json(root/name) for name in names}
    baseline, voting = docs[names[0]], docs[names[1]]
    repeat, diagnostic = docs[names[2]], docs[names[3]]
    if (baseline['left'],baseline['right'],voting['left'],voting['right']) != ('no_memory','fewshot_all','fewshot_all','fewshot_vote5'):
        raise ValueError('comparison arms differ')
    if baseline['campaign']!=voting['campaign'] or diagnostic['campaign']!=voting['right_campaign']:
        raise ValueError('campaign identities differ')
    if set(baseline['datasets']) != set(voting['datasets']):
        raise ValueError('dataset membership differs')
    for ds,b in baseline['datasets'].items():
        v=voting['datasets'][ds]
        if b['total']!=v['total'] or b['right_correct']!=v['left_correct'] or v['right_correct']!=diagnostic['datasets'][ds]['correct']:
            raise ValueError('comparison scores differ')
    rows=[]
    for system,source,side,cost_name,temp,k,phase,campaign in (
            ('no_memory',baseline,'left',names[4],0,1,'calibration',baseline['campaign']),
            ('fewshot_all',baseline,'right',names[5],0,1,'calibration',baseline['campaign']),
            ('fewshot_vote5',voting,'right',names[6],1,5,'controls',voting['right_campaign'])):
        cost=docs[cost_name]
        if cost['campaign']!=campaign or cost['phases']!=[phase] or cost['system']!=('fewshot_all' if k==5 else system):
            raise ValueError('cost scope differs')
        expected_jobs=len(source['datasets'])*k
        if len(cost['jobs'])!=expected_jobs:
            raise ValueError('cost report lacks all comparison jobs')
        scores={ds:d[side+'_correct']/d['total'] for ds,d in source['datasets'].items()}
        totals=cost['totals'];counts=totals['provider_attempts']
        rows.append({'system':system,'campaign':campaign,'temperature':temp,'samples_per_task':k,
                     'test_tasks':sum(d['total'] for d in source['datasets'].values()),
                     'macro_accuracy_percent':100*sum(scores.values())/len(scores),
                     'per_dataset_accuracy_percent':{ds:100*x for ds,x in scores.items()},
                     'usage_priced_inference_usd':totals['usage_priced_micro_usd']/1e6,
                     'unknown_inference_upper_usd':totals['unknown_cost_upper_micro_usd']/1e6,
                     'new_provider_completions':counts.get('complete',0),
                     'rejected_provider_attempts':counts.get('rejected',0),
                     'bounded_unknown_attempts':counts.get('bounded_unknown',0)})
    ordinary=rows[1]['usage_priced_inference_usd'];many=rows[2]
    def compact(d):
        return {k:d[k] for k in ('datasets','macro_delta','macro_delta_ci95','bootstrap')}
    return {'schema':'mhforge.article-evidence/1','scope':'Live baselines, repeatability and sampling controls; no optimized-harness/search result.',
            'comparison':rows,'baseline_paired':compact(baseline),'voting_paired':compact(voting),
            'overlap_sensitivity':{k:compact(d['overlap_sensitivity']) for k,d in [('baseline',baseline),('voting',voting)]},
            'voting_vs_first_temperature_one_sample':compact(diagnostic['vote_vs_first_sample']),
            'voting_cost_ratio_to_single_fewshot_bounds':[many['usage_priced_inference_usd']/ordinary,
                (many['usage_priced_inference_usd']+many['unknown_inference_upper_usd'])/ordinary],
            'repeatability':{k:repeat[k] for k in ('repeats','task_system_groups','task_outcomes_regraded',
                'groups_with_identical_requests','groups_with_correctness_changes','groups_with_normalized_label_changes','macro_validation_accuracy')},
            'voting_diagnostics':diagnostic['datasets'],
            'limitations':[
                'Equal-dataset macro accuracy, not pooled task accuracy; 412 released test tasks and 130 unique validation tasks.',
                'Primary voting comparison changes both temperature and sample count. The sample-one comparison is a secondary within-draw analysis.',
                'Paired bootstrap intervals condition on these datasets and runs; they exclude search and model-sampling uncertainty.',
                'Exact-label graders do not establish chemical, clinical or legal validity.',
                'Inference costs are marginal observed-usage estimates at frozen prices, not invoices or total experiment cost. Earlier replay sources, compute, proposer, telemetry and development are excluded.',
                'Unknown provider usage remains a separate bounded amount. Campaigns overlapped and experienced throttling, so no clean latency comparison is claimed.',
                'Native CES creation failed. Verified W&B Tables are an explicitly amended fallback; full proposer qualification and optimized search are not established.'],
            'source_files':[{'path':name,'sha256':util.sha256_file(root/name)} for name in names],
            'script_sha256':util.sha256_file(Path(__file__))}


if __name__ == '__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--input-dir',type=Path,required=True)
    p.add_argument('--output-dir',type=Path,required=True);a=p.parse_args();doc=assemble(a.input_dir)
    a.output_dir.mkdir(parents=True,exist_ok=True);util.write_json(a.output_dir/'experiment-summary.json',doc,fsync=True)
    rows=[{k:v for k,v in r.items() if k!='per_dataset_accuracy_percent'} for r in doc['comparison']]
    with (a.output_dir/'experiment-comparison.csv').open('w',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
    print(json.dumps(doc['comparison'],indent=2))
