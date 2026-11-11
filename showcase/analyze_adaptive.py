#!/usr/bin/env python3
"""Regrade every fresh answer, reconcile every attempt, and compute task-clustered results."""
import argparse,csv,json,random,sqlite3,statistics
from pathlib import Path
from collections import defaultdict,Counter
from adaptive_core import PLAN,digest,choose,can_stop,request_for,load_extract,verify_inputs
from mhforge import util,controls,ledger
from mhforge.paths import StatePaths
from analyze_inference_cost import settle

def percentile(values,p):
    xs=sorted(values)
    if not xs:return None
    at=(len(xs)-1)*p;lo=int(at);hi=min(lo+1,len(xs)-1)
    return xs[lo]+(xs[hi]-xs[lo])*(at-lo)

def summarize(rows,bootstrap=True):
    out={}
    for temp in PLAN['temperatures']:
        groups={pol:[r for r in rows if r['temperature']==temp and r['policy']==pol] for pol in PLAN['policies']}
        stats={}
        for pol,rs in groups.items():
            ds={name:[r for r in rs if r['dataset']==name] for name in sorted({r['dataset'] for r in rs})}
            stats[pol]={'decisions':len(rs),'macro_accuracy':statistics.mean(statistics.mean(r['correct'] for r in x) for x in ds.values()),'calls':sum(r['calls'] for r in rs),'mean_calls':statistics.mean(r['calls'] for r in rs),'early_stops':sum(r['early_stop'] for r in rs),'failed_policies':sum(r['status']!='complete' for r in rs),'usage_priced_usd':sum(r['cost_micro'] for r in rs)/1e6,'unknown_upper_usd':sum(r['unknown_micro'] for r in rs)/1e6,'flagged_latency':sum(r['pause_flag'] for r in rs),'latency_p50_s':percentile([r['latency_s'] for r in rs if r['latency_s'] is not None],.5),'latency_p95_s':percentile([r['latency_s'] for r in rs if r['latency_s'] is not None],.95),'unflagged_latency_p50_s':percentile([r['latency_s'] for r in rs if r['latency_s'] is not None and not r['pause_flag']],.5),'unflagged_latency_p95_s':percentile([r['latency_s'] for r in rs if r['latency_s'] is not None and not r['pause_flag']],.95),'per_dataset':{name:{'n':len(x),'accuracy':statistics.mean(r['correct'] for r in x),'early_stops':sum(r['early_stop'] for r in x),'latency_p50_s':percentile([r['latency_s'] for r in x if r['latency_s'] is not None],.5),'latency_p95_s':percentile([r['latency_s'] for r in x if r['latency_s'] is not None],.95)} for name,x in ds.items()}}
        left,right=groups['parallel5'],groups['stop3_else5'];lm={(r['dataset'],r['index'],r['repeat']):r for r in left};rm={(r['dataset'],r['index'],r['repeat']):r for r in right}
        if lm.keys()!=rm.keys():raise ValueError('Unpaired task membership')
        strata=defaultdict(lambda:defaultdict(list));pairs=Counter()
        for key,a in lm.items():
            b=rm[key];strata[key[0]][key[1]].append(b['correct']-a['correct']);pairs['both_correct' if a['correct'] and b['correct'] else 'loss' if a['correct'] else 'gain' if b['correct'] else 'both_wrong']+=1
        clusters=[[statistics.mean(v) for v in xs.values()] for xs in strata.values()];delta=statistics.mean(statistics.mean(xs) for xs in clusters)
        rng=random.Random(PLAN['bootstrap_seed']+temp)
        draws=[statistics.mean(statistics.mean(rng.choices(xs,k=len(xs))) for xs in clusters) for _ in range(PLAN['bootstrap_resamples'])] if bootstrap else []
        out[str(temp)]={'policies':stats,'accuracy_difference_pp':100*delta,'accuracy_difference_95ci_pp':[100*percentile(draws,p) for p in (.025,.975)] if draws else None,'paired_counts':dict(pairs),'call_reduction_fraction':1-stats['stop3_else5']['calls']/stats['parallel5']['calls'],'observed_cost_reduction_fraction':1-stats['stop3_else5']['usage_priced_usd']/stats['parallel5']['usage_priced_usd']}
    return out

def inspect(paths,*,require_complete=True,bootstrap=True):
    contract,inputs=verify_inputs(paths,Path(__file__).parent);m=util.read_json(paths.manifest);grader=controls.load_grader(paths);extract=load_extract(paths)
    if require_complete and not (paths.root/'adaptive/completed.json').exists():raise ValueError('Study incomplete')
    db=sqlite3.connect('file:'+str(paths.ledger)+'?mode=ro',uri=True);db.row_factory=sqlite3.Row
    ledger_rows=[dict(r) for r in db.execute('SELECT * FROM calls')];open_jobs=db.execute('SELECT count(*) FROM jobs WHERE closed_utc IS NULL').fetchone()[0];db.close()
    if open_jobs:raise ValueError('Open job scopes')
    by_call=defaultdict(list)
    for r in ledger_rows:by_call[r['call_id']].append(r)
    pauses=util.read_jsonl(paths.root/'adaptive/pauses.jsonl')
    rows=[];samples_public=[];seen=set();proof=Counter();call_ids=set();prices=ledger.prices_from_manifest(m)
    for block in contract['schedule']:
        for index in block['indices']:
            path=paths.root/'adaptive/decisions'/block['block_id']/f'{index:04}.json'
            if not path.exists():
                if require_complete:raise ValueError('Missing decision')
                continue
            r=util.read_json(path);task=inputs['tasks'][block['dataset']][str(index)]
            if r['request_sha256']!=digest(request_for(task,block['temperature'])) or r['example_sha256']!=task['example_sha256']:raise ValueError('Decision input changed')
            candidate=f"{block['policy']}_t{block['temperature']}_i{index:04}"
            jobkey=f"test.{candidate}.{block['dataset']}"+('' if block['repeat']==1 else '.r'+str(block['repeat']))
            calls=[(p,util.read_json(p)) for p in paths.calls_root.glob(jobkey+'.s*.a1/*.json')]
            cmap={c['call_id']:(p,c) for p,c in calls};costrows=[]
            for p,c in calls:
                if c['call_id'] in call_ids:raise ValueError('Call counted twice')
                call_ids.add(c['call_id'])
                if c.get('outcome') in ('replayed','coalesced'):raise ValueError('Fresh-call condition violated')
                if c['request_sha256']!=r['request_sha256'] or digest(c['request'])!=r['request_sha256']:raise ValueError('Call request changed')
                if c.get('outcome'):
                    if {a['ledger_id'] for a in c['attempts']}!={x['id'] for x in by_call[c['call_id']]}:raise ValueError('Call/ledger attempt membership differs')
                    for a in c['attempts']:
                        match=next(x for x in by_call[c['call_id']] if x['id']==a['ledger_id'])
                        if any(match[x]!=a[y] for x,y in [('status','ledger_status'),('spent_micro','spent_micro'),('reserved_micro','reserved_micro')]):raise ValueError('Ledger and call disagree')
                # Admission-only gateway files deliberately predate provider execution;
                # after interruption, the durable ledger owns their bounded attempt facts.
                for match in by_call[c['call_id']]:
                    if match['id'] in seen:raise ValueError('Attempt counted twice')
                    seen.add(match['id']);costrows.append(match)
            costs=settle(costrows,prices)
            samples=r['samples'];labels=[];label_classes={}
            for s in samples:
                p,c=cmap[s['call_id']]
                if util.sha256_file(p)!=s['call_sha256']:raise ValueError('Saved call changed')
                prediction=extract(c['response']['choices'][0]['message']['content'] or '','final_answer') if s['ok'] else ''
                label=controls.vote_label(r['dataset'],prediction,grader.extract_final_answer)
                if prediction!=s['prediction'] or digest(label)!=digest(s['label']):raise ValueError('Response extraction differs')
                labels.append(label)
                score=grader.get_evaluator(r['dataset'])(prediction,task['target']);correct=bool(score['was_correct'] if isinstance(score,dict) else score)
                samples_public.append({'block_id':r['block_id'],'index':index,'position':s['position'],'ok':int(s['ok']),'label_class':label_classes.setdefault(digest(label),len(label_classes)),'nonempty':int(bool(label)),'correct':int(correct and s['ok']),'cost_micro':settle(by_call[c['call_id']],prices)['usage_priced_micro_usd'],'call_sha256':s['call_sha256']})
            if r['status']=='complete':
                chosen=choose(labels)
                score=grader.get_evaluator(r['dataset'])(samples[chosen]['prediction'],task['target']);correct=bool(score['was_correct'] if isinstance(score,dict) else score)
                if chosen+1!=r['chosen_position'] or int(correct)!=r['correct']:raise ValueError('Selection or native grade differs')
                if block['policy']=='stop3_else5' and (len(samples)==3)!=can_stop(samples[:3]):raise ValueError('Stopping rule differs')
                if block['policy']=='parallel5':
                    if len(samples)!=5:raise ValueError('Incomplete fixed five')
                    stop=can_stop(samples[:3]);reconstructed=choose(labels[:3] if stop else labels)
                    proof['audited_fixed_five']+=1;proof['would_stop_after_three']+=int(stop);proof['answer_changes']+=int(reconstructed!=chosen)
            elif r['correct']!=0:raise ValueError('Failure scored as success')
            pause=any(x['start_epoch']<r['end_epoch'] and x['end_epoch']>r['start_epoch'] for x in pauses)
            rows.append({k:r[k] for k in ('block_id','dataset','temperature','repeat','policy','index','status','correct','latency_s','example_sha256')}|{'calls':len(calls),'early_stop':int(r.get('early_stop',False)),'pause_flag':int(pause),'cost_micro':costs['usage_priced_micro_usd'],'unknown_micro':costs['unknown_cost_upper_micro_usd'],'decision_sha256':util.sha256_file(path)})
    if seen!={r['id'] for r in ledger_rows}:raise ValueError('Unattributed ledger attempts')
    if call_ids!={util.read_json(p)['call_id'] for p in paths.calls_root.glob('*/*.json')}:raise ValueError('Unattributed gateway calls')
    if proof['answer_changes']:raise ValueError('Deterministic proof violated')
    totals=settle(ledger_rows,prices)
    summary={'schema':'mhforge.adaptive-results/1','campaign':m['campaign'],'contract_sha256':digest(contract),'cohort':contract['cohort'],'decisions':len(rows),'unique_tasks':sum(map(len,inputs['tasks'].values())),'same_output_audit':dict(proof),'costs':totals,'heartbeat_gaps':len(pauses),'by_temperature':summarize(rows,bootstrap=bootstrap)}
    return summary,rows,samples_public

def main():
    p=argparse.ArgumentParser();p.add_argument('--state',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args();summary,rows,samples=inspect(StatePaths(a.state.resolve()));a.output.mkdir(parents=True,exist_ok=True)
    util.write_json(a.output/'summary.json',summary)
    for name,data in [('decisions',rows),('samples',samples)]:
        with (a.output/(name+'.csv')).open('w') as f:w=csv.DictWriter(f,fieldnames=list(data[0]));w.writeheader();w.writerows(data)
    print(json.dumps(summary,indent=2))
if __name__=='__main__':main()
