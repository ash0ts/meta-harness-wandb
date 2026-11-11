#!/usr/bin/env python3
"""Recompute context and fixed-temperature sample-budget results offline."""
from pathlib import Path
from decimal import Decimal, ROUND_CEILING
from collections import Counter
import csv,json,math
from engineering_stats import summarize
ROOT=Path(__file__).resolve().parent

def same(actual,expected):
 if isinstance(expected,dict):
  assert set(actual)==set(expected)
  for k in expected:same(actual[k],expected[k])
 elif isinstance(expected,list):
  assert len(actual)==len(expected)
  for a,b in zip(actual,expected):same(a,b)
 elif isinstance(expected,(int,float)) and not isinstance(expected,bool):
  assert math.isclose(actual,expected,abs_tol=1e-10), (actual,expected)
 else:assert actual==expected,(actual,expected)

def main():
 rows=list(csv.DictReader((ROOT/'engineering-scores.csv').open()))
 expected=json.loads((ROOT/'engineering-aggregates.json').read_text())
 actual=summarize(rows);sensitivity=summarize(rows,exclude_overlap=True)
 for k in ['systems','paired','unique_tasks','scored_outputs']:same(actual[k],expected[k])
 same(sensitivity,expected['sensitivity'])
 costs=list(csv.DictReader((ROOT/'engineering-costs.csv').open()))
 for r in costs:
  assert r['status'] in ['complete','bounded_unknown','rejected']
  if r['status']=='complete':
   price=int((Decimal(r['prompt_tokens'])*Decimal('.03')+Decimal(r['completion_tokens'])*Decimal('.17')).to_integral_value(rounding=ROUND_CEILING))
   assert price==int(r['usage_priced_micro_usd'])
  else:assert int(r['usage_priced_micro_usd'])==0
 for arm,target in expected['costs'].items():
  all_rows=[r for r in costs if r['system']==arm]
  for k in [1,3,5]:
   selected=[r for r in all_rows if r['split']=='test' and int(r['sample'])<=k]
   same({field:sum(int(r[field]) for r in selected) for field in ['usage_priced_micro_usd','unknown_cost_upper_micro_usd']},target['policies'][str(k)])
  for field in ['usage_priced_micro_usd','unknown_cost_upper_micro_usd']:
   assert sum(int(r[field]) for r in all_rows)==target['all_jobs'][field]
  same(dict(Counter(r['status'] for r in all_rows)),target['all_jobs']['provider_attempts'])
 print(json.dumps({'context_accuracy_percent':{k:v['mean_accuracy_percent'] for k,v in actual['systems'].items()},'paired':actual['paired'],'provider_attempts':len(costs)},indent=2))
 print('PASS: all engineering headline means, 18 primary/sensitivity paired intervals and every provider-attempt cost reproduced.')

if __name__=='__main__':main()
