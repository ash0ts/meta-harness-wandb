"""Reproduce live-policy accuracy, costs, latency and same-output preservation without model calls."""
import csv,json,math
from collections import defaultdict,Counter
from decimal import Decimal,ROUND_CEILING
from pathlib import Path
from adaptive_stats import summarize
ROOT=Path(__file__).resolve().parent

def read(name):
    with (ROOT/name).open() as f:return list(csv.DictReader(f))

def run():
    expected=json.loads((ROOT/'adaptive-aggregates.json').read_text());rows=read('adaptive-decisions.csv');samples=read('adaptive-samples.csv');costs=read('adaptive-costs.csv')
    for r in rows:
        for k in ('temperature','repeat','index','correct','calls','early_stop','pause_flag','cost_micro','unknown_micro'):r[k]=int(r[k])
        r['latency_s']=float(r['latency_s']) if r['latency_s'] else None
    for r in samples:
        for k in ('index','position','ok','label_class','nonempty','correct','cost_micro'):r[k]=int(r[k])
    rebuilt=summarize(rows)
    def same(a,b):
        if isinstance(a,dict):return a.keys()==b.keys() and all(same(a[k],b[k]) for k in a)
        if isinstance(a,list):return len(a)==len(b) and all(same(x,y) for x,y in zip(a,b))
        if isinstance(a,(int,float)):return math.isclose(a,b,abs_tol=1e-10,rel_tol=1e-10)
        return a==b
    assert same(rebuilt,expected['by_temperature']), 'Headline statistics differ'
    grouped=defaultdict(list)
    for r in samples:grouped[r['block_id'],r['index']].append(r)
    proof=Counter()
    def choose(rs):
        counts=Counter(r['label_class'] for r in rs);top=max(counts.values());return next(r for r in rs if counts[r['label_class']]==top)
    def stop(rs):return len(rs)==3 and all(r['ok'] and r['nonempty'] for r in rs) and len({r['label_class'] for r in rs})==1
    for r in rows:
        ss=sorted(grouped[r['block_id'],r['index']],key=lambda x:x['position'])
        if r['status']!='complete':assert r['correct']==0;continue
        assert all(x['ok'] for x in ss)
        picked=choose(ss);assert picked['correct']==r['correct']
        if r['policy']=='parallel5':
            assert len(ss)==5
            early=stop(ss[:3]);other=choose(ss[:3] if early else ss)
            proof['audited_fixed_five']+=1;proof['would_stop_after_three']+=int(early);proof['answer_changes']+=int(picked['position']!=other['position'])
        else:assert (len(ss)==3)==stop(ss[:3]) and r['early_stop']==int(len(ss)==3)
    assert dict(proof)==expected['same_output_audit'] and proof['answer_changes']==0
    paid=unknown=0;statuses=Counter();by_decision=defaultdict(lambda:[0,0])
    for r in costs:
        status=r['status'];statuses[status]+=1;key=(r['dataset'],int(r['temperature']),int(r['repeat']),r['policy'],int(r['index']))
        if status=='complete':
            price=int((Decimal(r['prompt_tokens'])*Decimal('.03')+Decimal(r['completion_tokens'])*Decimal('.17')).to_integral_value(rounding=ROUND_CEILING));assert price==int(r['spent_micro']);paid+=price;by_decision[key][0]+=price
        elif status=='bounded_unknown':assert r['spent_micro']=='';v=int(r['reserved_micro']);unknown+=v;by_decision[key][1]+=v
        elif status=='rejected':assert int(r['spent_micro'])==0
        else:raise AssertionError('Unsettled attempt')
    for r in rows:assert by_decision[r['dataset'],r['temperature'],r['repeat'],r['policy'],r['index']]==[r['cost_micro'],r['unknown_micro']]
    assert paid==expected['costs']['usage_priced_micro_usd'] and unknown==expected['costs']['unknown_cost_upper_micro_usd'];assert dict(statuses)==expected['costs']['provider_attempts']
    print('Adaptive reproduction passed:',len(rows),'policy decisions,',len(samples),'samples,',len(costs),'provider attempts; both paired intervals, costs, latency percentiles and fixed-output audit agree.')
if __name__=='__main__':run()
