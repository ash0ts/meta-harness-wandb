#!/usr/bin/env python3
"""Export allowlisted numeric study evidence only, after all Forge readbacks verify."""
import argparse,ast,csv,json,sqlite3
from pathlib import Path
from adaptive_core import PLAN,digest
from analyze_adaptive import inspect
from mhforge import util
from mhforge.paths import StatePaths

def main():
    p=argparse.ArgumentParser();p.add_argument('--state',type=Path,required=True);p.add_argument('--metrics',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args();paths=StatePaths(a.state.resolve())
    receipt=util.read_json(a.metrics)
    if not receipt['passed'] or receipt['campaign']!=paths.root.name:raise ValueError('Native readback not verified for this study')
    contract=util.read_json(paths.root/'adaptive/contract.json')
    for b in contract['schedule']:
        rec=util.read_json(paths.receipt_file('adaptive:'+b['block_id'],'agent_lens'))
        if rec['status']!='verified':raise ValueError('Lens readback missing')
    if util.read_json(paths.receipt_file('study:adaptive','wandb'))['status']!='verified':raise ValueError('W&B readback missing')
    summary,rows,samples=inspect(paths)
    a.output.mkdir(parents=True,exist_ok=True)
    rows=[{k:v for k,v in r.items() if k not in ('example_sha256','decision_sha256')} for r in rows]
    samples=[{k:v for k,v in r.items() if k!='call_sha256'} for r in samples]
    costs=[]
    db=sqlite3.connect('file:'+str(paths.ledger)+'?mode=ro',uri=True);db.row_factory=sqlite3.Row
    for x in db.execute('SELECT * FROM calls ORDER BY id'):
        j=json.loads(db.execute('SELECT identity_json FROM jobs WHERE job_id=?',(x['job_id'],)).fetchone()[0]);candidate=j['candidate'];pol,temp_idx=candidate.rsplit('_t',1);temp,index=temp_idx.split('_i');u=json.loads(x['usage_json']) if x['usage_json'] else {}
        costs.append({'policy':pol,'temperature':int(temp),'dataset':j['dataset'],'repeat':j['repeat'] or 1,'index':int(index),'position':j['sample'],'attempt':x['n'],'status':x['status'],'prompt_tokens':u.get('prompt_tokens'),'completion_tokens':u.get('completion_tokens'),'cached_tokens':(u.get('prompt_tokens_details') or {}).get('cached_tokens',0) if u else None,'spent_micro':x['spent_micro'],'reserved_micro':x['reserved_micro']})
    db.close()
    for name,data in [('adaptive-decisions',rows),('adaptive-samples',samples),('adaptive-costs',costs)]:
        with (a.output/(name+'.csv')).open('w') as f:w=csv.DictWriter(f,fieldnames=list(data[0]));w.writeheader();w.writerows(data)
    util.write_json(a.output/'adaptive-aggregates.json',summary)
    # Copy only the two pure, frozen standard-library estimators into the public supplement.
    source=(Path(__file__).parent/'analyze_adaptive.py').read_text();tree=ast.parse(source)
    helpers='\n\n'.join(ast.get_source_segment(source,n) for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in ('percentile','summarize'))
    (a.output/'adaptive_stats.py').write_text('import random,statistics\nfrom collections import defaultdict,Counter\nPLAN = '+repr({k:PLAN[k] for k in ('temperatures','policies','bootstrap_seed','bootstrap_resamples')})+'\n\n'+helpers+'\n')
    print({'decisions':len(rows),'samples':len(samples),'provider_attempts':len(costs),'output':str(a.output)})
if __name__=='__main__':main()
