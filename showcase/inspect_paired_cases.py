#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Hash-only trace-to-score examples and explicitly post-hoc format diagnostics."""
import argparse
from pathlib import Path

import analyze_campaign as analysis
from mhforge import controls, util
from mhforge.paths import StatePaths
from mhforge.telemetry import join


CRITERIA = {
    'USPTO': 'Exact equality of lowercase, dot-separated reactant string sets; no chemical equivalence check.',
    'Symptom2Disease': 'Exact normalized diagnosis-label equality after the released answer extraction.',
    'LawBench': 'Exact equality of extracted charge-label sets; no independent legal judgment.'}


def inspect(root, left_name, right_name):
    paths=StatePaths(root);m=util.read_json(paths.manifest)
    expected={ds:sizes[2] for ds,sizes in m['upstream']['expect']['sizes'].items()}
    grader=controls.load_grader(paths)
    arms, sources={},{}
    for name in (left_name,right_name):
        if name=='fewshot_vote5':
            raise ValueError('use the vote study evidence for multi-sample control cases')
        arm,evidence=analysis.read_arm(root,name,expected)
        analysis.verify_source_identities(root,m,arm);arms[name]=arm;sources[name]={}
        for ev in evidence:
            job=util.read_json(paths.job_dir(ev['job_key'])/'job.json');ds=job['dataset']
            raw=util.read_jsonl(root/ev['path']);final=controls.final_evals(raw)
            jid=f"{ev['job_key']}.a{ev['attempt']}"
            calls={d['call_id']:d for d in (util.read_json(p) for p in paths.calls_dir(jid).glob('*.json'))}
            joined={r['index']:r for r in join.join_examples(join.parse_sidecar(raw),list(calls.values()),dataset=ds,split='test')}
            receipt=util.read_json(paths.receipt_file('job:'+jid,'agent_lens'))
            table=util.read_json(paths.receipt_file(f'eval:test.{name}','wandb'))
            if receipt['status']!='verified' or table['status']!='verified' or not table['readback'].get('table_contents_checked'):
                raise ValueError('case evidence is not remotely verified')
            sources[name][ds]=(ev,final,joined,calls,receipt['urls'][0],table['urls'][0])
    comparison=analysis.paired(arms[left_name],arms[right_name],repeats=1)
    cases=[]
    for ds in sorted(expected):
        for change in ('both_correct','gain','loss','both_wrong'):
            eligible=[r for r in comparison['tasks'] if r['dataset']==ds and r['change']==change]
            if not eligible:continue
            task=eligible[0];i=task['index'];case=dict(task,criterion=CRITERIA[ds],arms={})
            for name in (left_name,right_name):
                ev,final,joined,calls,trace_url,table_url=sources[name][ds]
                row=final[i];j=joined[i]
                if not j['matched'] or j['ambiguous']:
                    raise ValueError('case does not have an unambiguous operation join')
                ops=[]
                for call in j['calls']:
                    source=calls[call['call_id']]
                    ops.append({k:source.get(k) for k in ('call_id','outcome','finish_reason','usage','flags','cost_micro','cost_basis')})
                case['arms'][name]={'was_correct':row['was_correct'],'metrics':row['metrics'],
                    'prediction_sha256':row['prediction_sha256'],'target_sha256':row['target_sha256'],
                    'prediction_chars':row['prediction_chars'],'source':ev,'sidecar_seq':row['seq'],
                    'trace_url':trace_url,'evaluation_run_url':table_url,'operations':ops}
            cases.append(case)
    # This diagnostic is prompted by case inspection, not a predeclared treatment.
    # It leaves every benchmark score unchanged and makes no semantic-validity claim.
    law_gains=[]
    for task in comparison['tasks']:
        if task['dataset']!='LawBench' or task['change']!='gain':continue
        i=task['index'];row=sources[left_name]['LawBench'][1][i]
        predicted=set(controls.vote_label('LawBench',row['prediction'],grader.extract_final_answer))
        target=set(controls.vote_label('LawBench',row['target'],grader.extract_final_answer))
        if {label.removesuffix('罪') for label in predicted}==target:
            law_gains.append(i)
    return {'schema':'mhforge.case-audit/1','campaign':m['campaign'],'left':left_name,'right':right_name,
        'selection':'First evaluation index in each dataset/outcome stratum; illustrative, not a random sample.',
        'grader_sha256':util.sha256_file(paths.ws/'data/evaluators.py'),
        'raw_content':'Withheld; private sidecar locators and hashes retained.', 'cases':cases,
        'post_hoc_diagnostic':{'name':'LawBench gains whose left labels match after removing a trailing 罪 per label',
            'count':len(law_gains),'gain_count':comparison['datasets']['LawBench']['gain'],'evaluation_indices':law_gains,
            'limitation':'String-format diagnostic only; official scores unchanged; not an assessment of legal correctness.'}}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--state',type=Path,required=True);p.add_argument('--left',required=True)
    p.add_argument('--right',required=True);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();doc=inspect(a.state,a.left,a.right);util.write_json(a.output,doc,fsync=True)
    print({'cases':len(doc['cases']),'post_hoc_diagnostic':doc['post_hoc_diagnostic']})
