#!/usr/bin/env python3
"""Reproduce the article's numeric claims with no network or model calls."""
from pathlib import Path
import csv,json,random,math,collections
ROOT=Path(__file__).resolve().parent
rows=list(csv.DictReader((ROOT/'test-scores.csv').open()))
expected=json.loads((ROOT/'aggregates.json').read_text())
DATASETS=sorted({r['dataset'] for r in rows})
def mean_score(column, source=rows):
 return sum(sum(int(r[column]) for r in source if r['dataset']==d)/sum(r['dataset']==d for r in source) for d in DATASETS)/len(DATASETS)
def paired(left,right,source=rows):
 strata=[[int(r[right])-int(r[left]) for r in source if r['dataset']==d] for d in DATASETS]
 rng=random.Random(20261110)
 draws=sorted(sum(sum(rng.choices(s,k=len(s)))/len(s) for s in strata)/len(strata) for _ in range(10000))
 def q(p):
  pos=(len(draws)-1)*p;lo=int(pos)
  return draws[lo]+(draws[min(lo+1,len(draws)-1)]-draws[lo])*(pos-lo)
 return dict(delta=sum(sum(s)/len(s) for s in strata)/len(strata),ci95=[q(.025),q(.975)])
def verify_pair(actual,target):
 assert math.isclose(actual['delta'],target['macro_delta'],abs_tol=1e-12)
 assert all(math.isclose(x,y,abs_tol=1e-12) for x,y in zip(actual['ci95'],target['macro_delta_ci95']))
result={'test_tasks':len(rows),'mean_dataset_accuracy_percent':{name:100*mean_score(col) for name,col in [('direct','direct_correct'),('examples','examples_correct'),('vote','vote_correct')]}}
for name,l,r,k in [('examples_minus_direct','direct_correct','examples_correct','baseline_paired'),('vote_minus_examples','examples_correct','vote_correct','voting_paired'),('vote_minus_first_sample','first_sample_correct','vote_correct','voting_vs_first_temperature_one_sample')]:
 x=paired(l,r);verify_pair(x,expected[k]);result[name]=x
sensitivity=[r for r in rows if not int(r['overlap'])]
for name,l,r in [('baseline','direct_correct','examples_correct'),('voting','examples_correct','vote_correct')]:
 x=paired(l,r,sensitivity);verify_pair(x,expected['overlap_sensitivity'][name]);result[name+'_overlap_removed']=x
val=list(csv.DictReader((ROOT/'validation-scores.csv').open()));groups=collections.defaultdict(list)
for r in val:groups[(r['dataset'],r['task'],r['baseline'])].append(r)
flips=sum(len({r['correct'] for r in g})>1 for g in groups.values())
assert flips==expected['repeatability']['groups_with_correctness_changes']==31
assert len(groups)==260 and len(val)==1300
result['repeatability']={'groups':len(groups),'outcomes':len(val),'correctness_changes':flips,'label_changes':sum(int(g[0]['normalized_label_changed']) for g in groups.values())}
result['voting_diagnostics']={'all_five_wrong':sum(int(r['correct_samples'])==0 for r in rows),'available_correct_missed':sum(int(r['correct_samples'])>0 and not int(r['vote_correct']) for r in rows),'unanimous_wrong':sum(int(r['vote_count'])==5 and not int(r['vote_correct']) for r in rows)}
assert result['voting_diagnostics']=={'all_five_wrong':156,'available_correct_missed':25,'unanimous_wrong':33}
print(json.dumps(result,indent=2));print('PASS: saved headline estimates and all five paired intervals reproduced.')
