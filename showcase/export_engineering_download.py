#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Export only scores, numeric usage and methods from a verified engineering study.

The input analysis directory also contains private records; never copy it wholesale.
This tool does not publish, access the network, regrade, or run inference.
"""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3


def export(state, analysis, metrics_receipt, output):
    summary=json.loads((analysis/'engineering-summary.json').read_text())
    metrics=json.loads(metrics_receipt.read_text())
    if (summary['all_outputs_regraded']!=8240 or summary['identical_request_groups']!=1648
            or not summary['trace_readback_verified'] or not metrics['passed']):
        raise ValueError('Full engineering outcome and remote-metric verification are required')
    manifest=json.loads((state/'manifest.json').read_text())
    # The engine's canonical encoder uses UTF-8 compact sorted JSON.
    from mhforge.util import canonical_json
    if (summary['campaign']!=manifest['campaign'] or
            summary['frozen_manifest_sha256']!=hashlib.sha256(canonical_json(manifest)).hexdigest()):
        raise ValueError('Analysis does not belong to this frozen campaign')
    receipts=[json.loads(p.read_text()) for p in (state/'export/receipts').glob('*.wandb.json')]
    paths={r['remote']['run_path'] for r in receipts if r['status']=='verified'}
    if not metrics['checks'] or any(c['run_path'] not in paths for c in metrics['checks']):
        raise ValueError('Metric receipt does not belong to this verified campaign')
    if (manifest['executor']['kind']!='local_relay' or
            json.loads((state/'local-network-guard.json').read_text())['guard']!='sandbox-exec'):
        raise ValueError('This export description requires the recorded guarded local execution')
    output.mkdir(parents=True,exist_ok=True)
    public={k:summary[k] for k in ['systems','paired','unique_tasks','scored_outputs','sensitivity','costs','cost_scope','latency_scope']}
    public['method']={
        'context':'Per-task mean of five draws; equal dataset weights',
        'sampling':'First 1/3/5 draws; plurality of normalized labels; earliest tied sample',
        'bootstrap':'10000 paired task resamples within datasets; seed 20261111; retain all five draws of a task',
        'temperature':'0 primary; 1 supporting full-context arm',
        'context_limit':'30000 characters in reference examples',
        'execution':'Reviewed local workers with a loopback network guard; Forge inference and Agent Lens/W&B telemetry',
        'generation_limit':manifest['upstream']['expect']['max_tokens'],
        'prices_usd_per_million':{k:manifest['prices'][k] for k in ['input_per_million','cached_per_million','output_per_million']},
        'evaluation_status':'Prospective follow-up on previously inspected released test tasks; no untouched holdout or optimization result'}
    (output/'engineering-aggregates.json').write_text(json.dumps(public,indent=2)+'\n')
    fields=['system','dataset','index','sample','correct','empty','strict_json_valid','examples','example_chars','label_class']
    with (analysis/'engineering-scores.csv').open() as handle:
        reader=csv.DictReader(handle)
        if reader.fieldnames!=fields:raise ValueError('Score export columns are not the fixed public schema')
        score_rows=list(reader)
    if len(score_rows)!=8240:raise ValueError('Score rows are incomplete')
    for row in score_rows:
        if row['system'] not in summary['systems'] or row['dataset'] not in manifest['upstream']['expect']['datasets']:
            raise ValueError('Score group is outside the frozen study')
        for field in fields[2:]:row[field]=int(row[field])
        if not 1<=row['sample']<=5 or not 0<=row['label_class']<=4:
            raise ValueError('Sample or ordinal label class is out of range')
        if any(row[field] not in [0,1] for field in ['correct','empty','strict_json_valid']):
            raise ValueError('Score or format flag is not binary')
    with (output/'engineering-scores.csv').open('w',newline='') as handle:
        writer=csv.DictWriter(handle,fieldnames=fields,lineterminator='\n')
        writer.writeheader();writer.writerows(score_rows)
    shutil.copy2(Path(__file__).with_name('engineering_stats.py'),output/'engineering_stats.py')
    jobs={p.parent.name:json.loads(p.read_text()) for p in (state/'jobs').glob('*/job.json')}
    connection=sqlite3.connect('file:'+str(state.resolve()/'ledger.sqlite')+'?mode=ro',uri=True)
    connection.row_factory=sqlite3.Row
    rows=[]
    try:
        for r in connection.execute('select * from calls order by id'):
            job=jobs[r['job_key']]
            if job['status']!='succeeded' or r['status'] not in ['complete','bounded_unknown','rejected']:
                raise ValueError('All job and provider attempts must be settled or explicitly bounded')
            usage=json.loads(r['usage_json']) if r['usage_json'] else {}
            rows.append({'system':job['candidate'],'dataset':job['dataset'],'split':job['split'],'sample':job['sample'] or 0,
                'status':r['status'],'prompt_tokens':usage.get('prompt_tokens',''),'completion_tokens':usage.get('completion_tokens',''),
                'usage_priced_micro_usd':r['spent_micro'] or 0,
                'unknown_cost_upper_micro_usd':r['reserved_micro'] if r['status']=='bounded_unknown' else 0})
    finally:
        connection.close()
    if not rows:raise ValueError('No provider-attempt evidence')
    with (output/'engineering-costs.csv').open('w',newline='') as handle:
        writer=csv.DictWriter(handle,fieldnames=list(rows[0]),lineterminator='\n')
        writer.writeheader();writer.writerows(rows)
    return {'test_score_rows':8240,'provider_attempt_rows':len(rows),'published':False}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ['state','analysis','metrics-receipt','output']:parser.add_argument('--'+name,type=Path,required=True)
    args=parser.parse_args()
    print(json.dumps(export(args.state,args.analysis,args.metrics_receipt,args.output),indent=2))
