#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Hash-only paired held-out analysis; never reads test results into the proposer.

Usage: python analyze_campaign.py --state STATE --left fewshot_all --right SYSTEM --output result.json
Use --right fewshot_vote5 for the predeclared five-sample control.
Intervals condition on the selected harness and fixed datasets, not search randomness.
"""
import argparse
import json
import random
from pathlib import Path

from mhforge import controls, util
from mhforge.paths import StatePaths


def evaluation_indices(source_indices, count, seed):
    """The released MCE loader shuffles each split before assigning sidecar indices."""
    if len(set(source_indices)) != len(source_indices) or any(type(i) is not int or not 0 <= i < count for i in source_indices):
        raise ValueError('invalid source-file index')
    order = list(range(count))
    random.Random(seed).shuffle(order)
    inverse = {source: evaluated for evaluated, source in enumerate(order)}
    return sorted(inverse[i] for i in source_indices)


def source_identity(dataset, row):
    """Mirror only the three pinned released loaders; validate against every sidecar identity."""
    if dataset == 'USPTO':
        target = row['target']
    elif dataset == 'Symptom2Disease':
        target = row['answer']
    elif dataset == 'LawBench':
        target = row['answer']
        if '罪名:' in target:
            target = target.split('罪名:')[-1].strip()
    else:
        raise ValueError('source identity check has no pinned loader for '+dataset)
    return util.sha256_bytes((str(row['question'])+'\0'+str(target)).encode())


def verify_source_identities(root, manifest, arm):
    paths = StatePaths(root)
    pins = util.read_json(paths.pristine)['files']
    for name in ('data/api.py', 'data/loaders.py'):
        if util.sha256_file(paths.ws/name) != pins[name]:
            raise ValueError('upstream loader changed')
    for ds, outcomes in arm.items():
        rel = 'data/'+manifest['upstream']['expect']['dataset_dirs'][ds]+'/test.jsonl'
        if util.sha256_file(paths.ws/rel) != pins[rel]:
            raise ValueError('released test data changed')
        source = util.read_jsonl(paths.ws/rel)
        if len(source) != len(outcomes):
            raise ValueError('analysis requires every released test occurrence')
        random.Random(manifest['upstream']['expect']['seed']).shuffle(source)
        for index, row in enumerate(source):
            if outcomes[index]['example_sha256'] != source_identity(ds, row):
                raise ValueError('sidecar index does not match the shuffled released task identity')


def paired(left, right, *, repeats=10000, seed=20261110):
    if set(left) != set(right) or not left:
        raise ValueError('both arms must cover the same nonempty dataset set')
    strata, datasets, tasks = [], {}, []
    for ds in sorted(left):
        a, b = left[ds], right[ds]
        if set(a) != set(b) or not a:
            raise ValueError('paired task membership differs')
        counts = dict(both_correct=0, gain=0, loss=0, both_wrong=0)
        diffs = []
        for i in sorted(a):
            x, y = a[i], b[i]
            if x['example_sha256'] != y['example_sha256']:
                raise ValueError('paired task identity differs')
            ac, bc = x['was_correct'], y['was_correct']
            if not isinstance(ac, bool) or not isinstance(bc, bool):
                raise ValueError('missing correctness is not a scored failure')
            label = 'both_correct' if ac and bc else 'loss' if ac else 'gain' if bc else 'both_wrong'
            counts[label] += 1
            diffs.append(int(bc) - int(ac))
            tasks.append(dict(dataset=ds, index=i, example_sha256=x['example_sha256'],
                              left_correct=ac, right_correct=bc, change=label))
        n = len(a)
        datasets[ds] = dict(total=n, left_correct=counts['both_correct']+counts['loss'],
                           right_correct=counts['both_correct']+counts['gain'], **counts,
                           delta=(counts['gain']-counts['loss'])/n)
        strata.append(diffs)
    rng = random.Random(seed)
    draws = sorted(sum(sum(rng.choices(s, k=len(s)))/len(s) for s in strata)/len(strata)
                   for _ in range(repeats))
    def quantile(p):
        pos = (len(draws)-1)*p
        low = int(pos)
        return draws[low] + (draws[min(low+1,len(draws)-1)]-draws[low])*(pos-low)
    return dict(datasets=datasets, macro_delta=sum(d['delta'] for d in datasets.values())/len(datasets),
                macro_delta_ci95=[quantile(.025),quantile(.975)],
                bootstrap=dict(method='paired task bootstrap stratified by dataset; equal dataset weighting',
                               repeats=repeats, seed=seed, interval='percentile',
                               limitation='conditional on selected harness and fixed datasets; excludes search and model-sampling uncertainty'),
                tasks=tasks)


def read_arm(root, name, expected):
    evidence, arm = [], {}
    grader = controls.load_grader(StatePaths(root))
    if name == 'fewshot_vote5':
        p = root/'controls/result.json'
        doc = util.read_json(p)
        if any(doc['plan'].get(k) != v for k, v in controls.PLAN.items()):
            raise ValueError('control plan differs from predeclared protocol')
        for ds, result in doc['datasets'].items():
            samples, seen = [], set()
            for number, record in enumerate(result['evidence'], 1):
                key = util.safe_component(record['job_key'])
                job = util.read_json(root/'jobs'/key/'job.json')
                if (key in seen or job.get('sample') != number or job.get('candidate') != controls.PLAN['system']
                        or job.get('split') != 'test' or job.get('phase') != 'controls'
                        or job.get('dataset') != ds or job.get('status') != 'succeeded'):
                    raise ValueError('invalid or duplicate control sample evidence')
                argv = job['inner_argv']
                if float(argv[argv.index('--temperature')+1]) != controls.PLAN['temperature']:
                    raise ValueError('control sample temperature differs')
                if type(record['attempt']) is not int or record['attempt'] < 1:
                    raise ValueError('invalid control attempt')
                seen.add(key)
                sidecar = root/'jobs'/key/('a'+str(record['attempt']))/'out/sidecar.jsonl'
                if util.sha256_file(sidecar) != record['sidecar_sha256']:
                    raise ValueError('control sidecar digest changed')
                attempt = next(a for a in job['attempts'] if a['attempt'] == record['attempt'])
                if attempt.get('outcome') != 'ok' or attempt['outputs']['sidecar.jsonl'] != record['sidecar_sha256']:
                    raise ValueError('control job evidence differs')
                samples.append(controls.final_evals(util.read_jsonl(sidecar)))
                evidence.append(dict(path=str(sidecar.relative_to(root)), sha256=record['sidecar_sha256'],
                                     job_key=key, attempt=record['attempt']))
            rebuilt = controls.aggregate(ds, samples, grader, expected_count=expected[ds])
            if any(result.get(k) != v for k, v in rebuilt.items()):
                raise ValueError('control aggregate does not reproduce from its sample evidence')
            arm[ds] = {r['index']:r for r in result['rows']}
        evidence.append(dict(path=str(p.relative_to(root)), sha256=util.sha256_file(p)))
    else:
        for p in sorted((root/'jobs').glob('*/job.json')):
            job = util.read_json(p)
            if (job['candidate'] != name or job['split'] != 'test' or job['sample'] is not None
                    or job['repeat'] is not None or job['phase'] not in ('test','calibration')):
                continue
            if job['status'] != 'succeeded':
                raise ValueError('selected arm has an incomplete test job')
            attempts = [a for a in job['attempts'] if a['outcome'] == 'ok']
            if not attempts:
                raise ValueError('successful attempt missing')
            a = attempts[-1]
            sidecar = p.parent/('a'+str(a['attempt']))/'out/sidecar.jsonl'
            if util.sha256_file(sidecar) != a['outputs']['sidecar.jsonl']:
                raise ValueError('sidecar digest changed')
            rows = controls.final_evals(util.read_jsonl(sidecar))
            ds = job['dataset']
            judge = grader.get_evaluator(ds)
            for row in rows.values():
                scored = judge(row['prediction'], row['target'])
                correct = scored['was_correct'] if isinstance(scored, dict) else scored
                if bool(correct) != row['was_correct']:
                    raise ValueError('host grader disagrees with the stored task score')
            # Calibration reuse can produce two records. Accept only identical task outcomes.
            compact = {i:{k:r[k] for k in ('example_sha256','prediction_sha256','was_correct')} for i,r in rows.items()}
            if ds in arm and arm[ds] != compact:
                raise ValueError('ambiguous calibration/final-test outcomes')
            arm[ds] = compact
            evidence.append(dict(path=str(sidecar.relative_to(root)), sha256=util.sha256_file(sidecar),
                                 job_key=job['job_key'], attempt=a['attempt']))
    if set(arm) != set(expected) or any(set(arm[ds]) != set(range(n)) for ds,n in expected.items()):
        raise ValueError('arm does not have every predeclared test task')
    return arm, evidence


def compatible_campaigns(left, right):
    """A cross-campaign comparison must retain task, worker and solver pins.

    Proposer settings, campaign names and service bookkeeping may differ;
    the five-vote arm's predeclared temperature is verified in its own plan.
    """
    keys = ('upstream.commit', 'upstream.tree_sha256', 'upstream.worker_lock_sha256', 'upstream.expect',
            'provider.kind', 'provider.base_url', 'provider.wire_model', 'provider.expected_response_model',
            'gateway.max_output_tokens', 'sandbox.base_image', 'sandbox.image', 'sandbox.wheelhouse')
    for key in keys:
        try:
            a, b = util.lookup(left, key), util.lookup(right, key)
        except KeyError:
            raise ValueError('cross-campaign input is missing: '+key) from None
        if a != b:
            raise ValueError('cross-campaign input differs: '+key)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--state', type=Path, required=True)
    p.add_argument('--left', required=True)
    p.add_argument('--right', required=True)
    p.add_argument('--right-state', type=Path, help='Separate frozen campaign for the right arm; pins must match')
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--split-audit', type=Path, help='Frozen audit for the separately labelled overlap sensitivity')
    args=p.parse_args()
    m=util.read_json(args.state/'manifest.json')
    expected={ds:sizes[2] for ds,sizes in m['upstream']['expect']['sizes'].items()}
    left, le=read_arm(args.state,args.left,expected)
    verify_source_identities(args.state,m,left)
    right_state=args.right_state or args.state
    right_manifest=util.read_json(right_state/'manifest.json')
    compatible_campaigns(m,right_manifest)
    right, re=read_arm(right_state,args.right,expected)
    verify_source_identities(right_state,right_manifest,right)
    result=dict(schema='mhforge.paired-analysis/1',campaign=m['campaign'],left=args.left,right=args.right,
                source_manifest_sha256=util.sha256_file(args.state/'manifest.json'),
                right_campaign=right_manifest['campaign'],
                right_manifest_sha256=util.sha256_file(right_state/'manifest.json'),
                analysis_sha256=util.sha256_file(Path(__file__)),evidence=dict(left=le,right=re),
                **paired(left,right))
    if args.split_audit:
        audit=util.read_json(args.split_audit)
        for ds, data in audit['datasets'].items():
            for split, f in data['files'].items():
                path=args.state/'workspace/text_classification/data'/m['upstream']['expect']['dataset_dirs'][ds]/(split+'.jsonl')
                if util.sha256_file(path) != f['sha256']:
                    raise ValueError('split audit refers to different data')
        source_exclude=audit['sensitivity_exclude_test_indices']
        exclude={ds:evaluation_indices(indices,expected[ds],m['upstream']['expect']['seed'])
                 for ds,indices in source_exclude.items()}
        def filtered(arm):
            return {ds:{i:r for i,r in rows.items() if i not in exclude[ds]} for ds,rows in arm.items()}
        result['overlap_sensitivity']=dict(excluded_test_indices=exclude,
            excluded_source_file_indices=source_exclude,
            index_mapping='Source file order mapped through the pinned loader shuffle; every task identity verified.',
            split_audit_sha256=util.sha256_file(args.split_audit),**paired(filtered(left),filtered(right)))
    util.write_json(args.output,result,fsync=True)
    print(json.dumps({k:v for k,v in result.items() if k not in ('tasks','evidence')},indent=2))


if __name__ == '__main__':
    main()
