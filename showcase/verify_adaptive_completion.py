#!/usr/bin/env python3
"""Independent operational invariants and persisted decision-attribute checks."""
import argparse,json,math,sqlite3
from pathlib import Path
from collections import Counter
from adaptive_core import digest,verify_inputs,PLAN
from mhforge import util
from mhforge.paths import StatePaths
from mhforge.telemetry import readback

def peak(events):
    active=maximum=0
    for _,delta in sorted(events):active+=delta;maximum=max(maximum,active)
    if active:raise ValueError('Unbalanced timing intervals')
    return maximum

def verify_attributes(paths):
    counts=Counter();attribute_count=0
    for p in sorted((paths.root/'adaptive/blocks').glob('*.json')):
        unit='adaptive:'+p.stem;receipt=util.read_json(paths.receipt_file(unit,'agent_lens'))
        if receipt['status']!='verified':raise ValueError('Unverified trace: '+unit)
        plan=util.read_json(paths.plan_dir(unit)/'spans.json');rows=util.read_json(paths.stored_spans_file(unit));prefix=readback._plan_prefix(plan)
        wanted=readback._plan_items(plan,prefix);ids={str(s.get('response_id')):ident for kind,ident,s in wanted if kind=='llm' and s.get('response_id')}
        stored={readback._row_identity(row,readback._row_attrs(row),prefix,ids):row for row in rows}
        if len(stored)!=len(wanted):raise ValueError('Trace cardinality differs')
        for kind,ident,span in wanted:
            got=readback._row_attrs(stored[kind,ident])
            for k,v in span['attributes'].items():
                value=got.get(k)
                equal=math.isclose(v,value,rel_tol=1e-12,abs_tol=1e-9) if isinstance(v,float) and isinstance(value,(int,float)) else type(v)==type(value) and v==value
                if not equal:raise ValueError('Persisted decision attribute differs: '+k)
                attribute_count+=1
            counts[kind]+=1
    return {'span_kinds':dict(counts),'attributes_verified':attribute_count}

def inspect(paths,metrics):
    contract,inputs=verify_inputs(paths,Path(__file__).parent)
    if not (paths.root/'adaptive/completed.json').exists():raise ValueError('Execution incomplete')
    counts=Counter();worker_events=[];call_events=[];previous_end=None;serial_blocks=True;second_batches=0;response_ids=[]
    for block in contract['schedule']:
        marker=util.read_json(paths.root/'adaptive/blocks'/(block['block_id']+'.json'))
        if marker['block']!=block:raise ValueError('Block differs from frozen schedule')
        expected=[f"adaptive/decisions/{block['block_id']}/{i:04}.json" for i in block['indices']]
        if marker['decisions']!=expected:raise ValueError('Block membership differs')
        batch=[]
        for rel,index in zip(expected,block['indices']):
            row=util.read_json(paths.root/rel);batch.append(row)
            if any(row[k]!=block[k] for k in ('block_id','pair_id','dataset','temperature','repeat','policy')) or row['index']!=index:raise ValueError('Decision identity differs')
            counts[row['status']]+=1
            if row['latency_s'] is not None:worker_events.extend([(row['start_epoch'],1),(row['end_epoch'],-1)])
            calls=[]
            for s in row['samples']:
                c=util.read_json(paths.calls_dir(s['job_id'])/(s['call_id']+'.json'));calls.append(c)
                j=c['job']
                if j['sample']!=s['position'] or (j['repeat'] or 1)!=block['repeat'] or j['dataset']!=block['dataset']:raise ValueError('Sample identity differs')
                if c.get('ended_utc'):call_events.extend([(util.parse_utc(c['received_utc']),1),(util.parse_utc(c['ended_utc']),-1)])
                if c.get('response_id'):response_ids.append(c['response_id'])
            if row['status']=='complete' and block['policy']=='stop3_else5' and len(calls)==5:
                if min(util.parse_utc(c['received_utc']) for c in calls[3:])<max(util.parse_utc(c['ended_utc']) for c in calls[:3]):raise ValueError('Second batch started before first batch ended')
                second_batches+=1
        low=min(r['start_epoch'] for r in batch);high=max(r['end_epoch'] for r in batch)
        if previous_end is not None and low<previous_end:serial_blocks=False
        previous_end=high
    pauses=util.read_jsonl(paths.root/'adaptive/pauses.jsonl')
    if not pauses:
        if not serial_blocks or peak(worker_events)>PLAN['concurrent_tasks'] or peak(call_events)>PLAN['concurrent_calls']:raise ValueError('Concurrency/serialization differs')
    db=sqlite3.connect('file:'+str(paths.ledger)+'?mode=ro',uri=True)
    open_jobs=db.execute('SELECT count(*) FROM jobs WHERE closed_utc IS NULL').fetchone()[0]
    open_meters=db.execute("SELECT count(*) FROM meter_events WHERE status='open'").fetchone()[0]
    unsettled=db.execute("SELECT count(*) FROM calls WHERE status NOT IN ('complete','bounded_unknown','rejected')").fetchone()[0];db.close()
    if open_jobs or open_meters or unsettled:raise ValueError('Unsettled resources')
    attrs=verify_attributes(paths);rec=util.read_json(paths.receipt_file('study:adaptive','wandb'));native=util.read_json(metrics)
    if rec['status']!='verified' or not native['passed'] or native['campaign']!=paths.root.name or native['run_path']!=rec['remote']['run_path']:raise ValueError('Native study readback missing or mismatched')
    if native['table_rows']!=sum(counts.values()):raise ValueError('Study table denominator differs')
    return {'schema':'mhforge.adaptive-completion/1','campaign':paths.root.name,'verified_utc':util.utc_now(),'passed':True,'contract_sha256':digest(contract),'decision_statuses':dict(counts),'unique_tasks':sum(map(len,inputs['tasks'].values())),'blocks':len(contract['schedule']),'second_batches_after_first_batch_verified':second_batches,'maximum_observed_timed_workers':peak(worker_events),'maximum_observed_logical_calls':peak(call_events),'serialized_policy_blocks':serial_blocks,'heartbeat_gaps':len(pauses),'repeated_provider_response_ids':len(response_ids)-len(set(response_ids)),'open_jobs':open_jobs,'open_meters':open_meters,'unsettled_provider_attempts':unsettled,'native_table_rows':native['table_rows'],'native_numeric_history_summary_lineage_verified':True,**attrs}

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--state',type=Path,required=True);p.add_argument('--metrics',type=Path);p.add_argument('--attributes-only',action='store_true');p.add_argument('--output',type=Path,required=True);a=p.parse_args();paths=StatePaths(a.state.resolve())
    result=verify_attributes(paths) if a.attributes_only else inspect(paths,a.metrics);util.write_json(a.output,result);print(json.dumps(result,indent=2))
