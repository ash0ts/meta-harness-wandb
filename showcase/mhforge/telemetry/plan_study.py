# SPDX-License-Identifier: MIT
"""Rebuild aggregate control evidence before planning hash-only study tables.

No network or export side effects. Every source sidecar is checked against the
successful attempt, and every aggregate is recomputed before it can be exported.
"""
from __future__ import annotations

import hashlib
import json

from .. import controls, qualification, util
from . import plan_runs, plan_tables


def _job(paths, key, *, system, dataset, split, repeat=None, sample=None):
    key = util.safe_component(key)
    job = util.read_json(paths.job_dir(key) / 'job.json')
    expected = dict(candidate=system, dataset=dataset, split=split, repeat=repeat, sample=sample)
    if job.get('status') != 'succeeded' or any(job.get(k) != v for k, v in expected.items()):
        raise ValueError('study job identity differs or job is incomplete')
    good = [a for a in job['attempts'] if a.get('outcome') == 'ok']
    if not good:
        raise ValueError('study successful attempt missing')
    attempt = good[-1]
    out = paths.attempt_dir(key, attempt['attempt']) / 'out'
    for name in ('sidecar.jsonl', split + '.json'):
        if util.sha256_file(out / name) != attempt['outputs'].get(name):
            raise ValueError('study source digest changed')
    rows = util.read_jsonl(out / 'sidecar.jsonl')
    result = util.read_json(out / (split + '.json'))
    if any(result.get(k) != v for k, v in job['result'].items()):
        raise ValueError('study result and job record differ')
    evidence = {'job_key': key, 'attempt': attempt['attempt'],
                'sidecar_sha256': util.sha256_file(out / 'sidecar.jsonl')}
    return job, rows, result, evidence


def noise(paths, manifest, lens_url):
    stored = util.read_json(paths.noise)
    sidecars, results, evidence, rows = {}, {}, [], []
    for system in manifest['upstream']['expect']['baselines']:
        for ds in manifest['upstream']['expect']['datasets']:
            for repeat in range(1, manifest['calibration']['noise_repeats'] + 1):
                key = f'val.{system}.{ds}' + (f'.r{repeat}' if repeat > 1 else '')
                job, source, result, ev = _job(paths, key, system=system, dataset=ds, split='val',
                                               repeat=repeat if repeat > 1 else None)
                final = controls.final_evals(source, split='val')
                n = manifest['upstream']['expect']['sizes'][ds][1]
                if set(final) != set(range(n)) or result['total'] != n:
                    raise ValueError('noise task membership differs')
                sidecars.setdefault(system, {}).setdefault(ds, {})[repeat] = source
                results.setdefault(system, {}).setdefault(ds, {})[repeat] = result
                evidence.append(ev)
                url = lens_url(f"{key}.a{ev['attempt']}")
                if not url:
                    raise ValueError('noise trace readback is not verified')
                rows.append([system, ds, repeat, result['correct'], n, result['correct']/n,
                             ev['sidecar_sha256'], url])
    rebuilt = qualification.noise_stats(sidecars, results)
    if rebuilt['problems'] or rebuilt != stored:
        raise ValueError('noise aggregate does not reproduce from its source evidence')
    summary = {f'noise/{system}/macro_std_pp': value for system, value in rebuilt['macro_std'].items()}
    for system, values in rebuilt['macro_validation_accuracy'].items():
        summary[f'noise/{system}/macro_min_percent'] = min(values)
        summary[f'noise/{system}/macro_max_percent'] = max(values)
    for ds, by_system in rebuilt['per_dataset'].items():
        for system, result in by_system.items():
            summary[f'noise/{system}/{ds}/flip_rate'] = result['flip_rate']
    return (['system', 'dataset', 'repeat', 'correct', 'total', 'accuracy', 'sidecar_sha256', 'trace_url'],
            rows, evidence, summary)


def voting(paths, manifest, lens_url):
    stored = util.read_json(paths.root / 'controls/result.json')
    if any(stored['plan'].get(k) != v for k, v in controls.PLAN.items()):
        raise ValueError('control plan differs')
    datasets = manifest['upstream']['expect']['datasets']
    if set(stored['datasets']) != set(datasets):
        raise ValueError('control dataset membership differs')
    grader = controls.load_grader(paths)
    rows, evidence, scores, summary = [], [], [], {}
    for ds in datasets:
        result = stored['datasets'][ds]
        samples, sources, urls = [], [], []
        for sample in range(1, controls.PLAN['k'] + 1):
            key = f'test.fewshot_all.{ds}.s{sample}'
            job, source, _, ev = _job(paths, key, system='fewshot_all', dataset=ds, split='test', sample=sample)
            argv = job['inner_argv']
            if job.get('phase') != 'controls' or float(argv[argv.index('--temperature')+1]) != controls.PLAN['temperature']:
                raise ValueError('control sampling protocol differs')
            samples.append(controls.final_evals(source))
            sources.append(ev)
            url = lens_url(f"{key}.a{ev['attempt']}")
            if not url:
                raise ValueError('control trace readback is not verified')
            urls.append(url)
        n = manifest['upstream']['expect']['sizes'][ds][2]
        rebuilt = controls.aggregate(ds, samples, grader, expected_count=n)
        if {**rebuilt, 'evidence': sources} != result:
            raise ValueError('control aggregate does not reproduce from its source evidence')
        evidence += sources
        scores.append(rebuilt['accuracy'])
        prefix = f'controls/fewshot_vote5/{ds}/'
        summary.update({prefix+'accuracy':rebuilt['accuracy'], prefix+'correct':rebuilt['correct'],
                        prefix+'total':n, prefix+'tied_tasks':sum(r['tied'] for r in rebuilt['rows'])})
        for row in rebuilt['rows']:
            rows.append([ds, row['index'], row['example_sha256'], row['selected_sample'], row['votes'],
                         row['tied'], row['was_correct'], json.dumps(row['sample_correct']),
                         json.dumps(row['sample_prediction_sha256']), json.dumps(urls)])
    macro = sum(scores)/len(scores)
    if macro != stored['macro_accuracy']:
        raise ValueError('control macro accuracy differs')
    summary['controls/fewshot_vote5/macro_accuracy'] = macro
    return (['dataset', 'index', 'example_sha256', 'selected_sample', 'votes', 'tied', 'was_correct',
             'sample_correct', 'sample_prediction_sha256', 'sample_trace_urls'],
            rows, evidence, summary)


def engineering(paths, manifest, lens_url):
    from .. import engineering as experiment
    stored = util.read_json(paths.root / 'engineering/result.json')
    if stored['plan']['arms'] != {k:list(v) for k,v in experiment.ARMS.items()}:
        raise ValueError('engineering arm definitions differ')
    grader = controls.load_grader(paths)
    rows, evidence, summary = [], [], {}
    for system, (_, temperature) in experiment.ARMS.items():
        ds_means = []; policy_scores = {k:[] for k in (1,3,5)}
        for ds in manifest['upstream']['expect']['datasets']:
            samples, urls = [], []
            for sample in range(1,6):
                key = f'test.{system}.{ds}.s{sample}'
                job, source, result, ev = _job(paths, key, system=system, dataset=ds, split='test', sample=sample)
                if float(job['inner_argv'][job['inner_argv'].index('--temperature')+1]) != temperature:
                    raise ValueError('engineering temperature differs')
                final = controls.final_evals(source)
                n = manifest['upstream']['expect']['sizes'][ds][2]
                if set(final) != set(range(n)):
                    raise ValueError('engineering task membership differs')
                evaluator = grader.get_evaluator(ds)
                for row in final.values():
                    grade = evaluator(row['prediction'], row['target'])
                    correct = grade['was_correct'] if isinstance(grade,dict) else grade
                    if bool(correct) != row['was_correct']:
                        raise ValueError('engineering grader mismatch')
                if sum(r['was_correct'] for r in final.values()) != result['correct']:
                    raise ValueError('engineering result total differs')
                url = lens_url(f"{key}.a{ev['attempt']}")
                if not url:
                    raise ValueError('engineering trace is not verified')
                urls.append(url); samples.append(final); evidence.append(ev)
            means = []; scores = {k:[] for k in (1,3,5)}
            for i in range(n):
                group = [s[i] for s in samples]
                if len({(r['example_sha256'],r['target_sha256']) for r in group}) != 1:
                    raise ValueError('engineering sample identity differs')
                labels = [controls.vote_label(ds,r['prediction'],grader.extract_final_answer) for r in group]
                correct = [r['was_correct'] for r in group]
                selected = [experiment.vote_indices(labels,k) for k in (1,3,5)]
                mean = sum(correct)/5; means.append(mean)
                for k,idx in zip((1,3,5),selected): scores[k].append(correct[idx])
                rows.append([system,ds,i,mean,json.dumps(correct),*[correct[idx] for idx in selected],
                             json.dumps([idx+1 for idx in selected]),json.dumps(urls)])
            ds_means.append(sum(means)/n)
            summary[f'engineering/{system}/{ds}/mean_accuracy'] = sum(means)/n
            for k in scores:
                value = sum(scores[k])/n; policy_scores[k].append(value)
                summary[f'engineering/{system}/{ds}/vote{k}_accuracy'] = value
        summary[f'engineering/{system}/macro_mean_accuracy'] = sum(ds_means)/len(ds_means)
        for k,values in policy_scores.items():
            summary[f'engineering/{system}/macro_vote{k}_accuracy'] = sum(values)/len(values)
    if sorted(evidence,key=lambda x:x['job_key']) != sorted(stored['evidence'],key=lambda x:x['job_key']):
        raise ValueError('engineering source evidence differs')
    return (['system','dataset','index','mean_accuracy','sample_correct','vote1_correct','vote3_correct',
             'vote5_correct','selected_samples','trace_urls'],rows,evidence,summary)


def plan(name, paths, manifest, lens_url, *, export_attempt):
    columns, rows, evidence, summary = {'noise':noise,'vote5':voting,'engineering':engineering}[name](paths, manifest, lens_url)
    unit, key = f'study:{name}', f'study/{name}'
    digest = hashlib.sha256(util.canonical_json(evidence)).hexdigest()
    campaign = manifest['campaign']
    table = {'key': key, 'columns': columns, 'rows': rows,
             'backend': plan_tables.table_backend(manifest), 'input_columns': [], 'output_columns': [],
             'score_columns': ['mean_accuracy','vote1_correct','vote3_correct','vote5_correct'] if name == 'engineering'
                              else ['accuracy'] if name == 'noise' else ['was_correct']}
    table_doc = {'schema': 'mhforge.study-tables/1', 'backend': table['backend'], 'tables': [table]}
    artifact = {'op': 'log', 'name': f'{campaign}-{name}-evidence', 'type': 'study-evidence',
                'aliases': [campaign], 'files': [], 'metadata': {'sources': evidence, 'sha256': digest}}
    run = plan_runs._run(manifest, unit=unit,
        rid=hashlib.sha256(f'{campaign}:{unit}:e{export_attempt}'.encode()).hexdigest()[:24],
        job_type='study', name=f'{campaign}-{name}', tags=['mhforge', name, 'hash-only'], resume=False,
        config=plan_runs._config(manifest, {'study': name, 'evidence_sha256': digest}),
        summary={**summary, 'study/rows': len(rows), 'study/evidence_sha256': digest},
        define_metrics=[], history=[{**summary, 'study/rows': len(rows)}], tables=[key],
        artifacts=[artifact], export_attempt=export_attempt)
    return run, table_doc
