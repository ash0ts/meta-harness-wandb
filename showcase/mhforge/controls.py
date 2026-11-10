# SPDX-License-Identifier: MIT
"""Five independent few-shot samples, votes, and paired task-level comparisons.

Normalization mirrors the pinned upstream data/evaluators.py; the selected raw
prediction is then scored again with those checked upstream bytes.
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from types import ModuleType

from . import util
from .errors import CampaignStop
from .telemetry.join import parse_sidecar

PLAN = {'schema': 'mhforge.controls/1', 'system': 'fewshot_all', 'k': 5, 'temperature': 1.0,
        'split': 'test', 'tie_break': 'earliest_sample_among_tied_labels', 'seed': 42,
        'replay': 'independent_sample_identity', 'missingness': 'stop_incomplete',
        'selection': 'predeclared; no test-based candidate selection'}


def load_grader(paths):
    """Load only two hash-checked upstream modules, without altering sys.path."""
    files = util.read_json(paths.pristine)['files']
    names = ('_mhf_grader', '_mhf_grader.constants', '_mhf_grader.evaluators')
    saved = {name: sys.modules.get(name) for name in names}
    try:
        package = ModuleType(names[0])
        package.__path__ = []
        sys.modules[names[0]] = package
        for leaf in ('constants', 'evaluators'):
            rel = 'data/' + leaf + '.py'
            path = paths.ws / rel
            source = path.read_bytes()
            if util.sha256_bytes(source) != files[rel]:
                raise CampaignStop(reason='upstream_contract', detail='control grader changed')
            module = ModuleType('_mhf_grader.' + leaf)
            module.__package__ = '_mhf_grader'
            module.__file__ = str(path)
            sys.modules[module.__name__] = module
            exec(compile(source, str(path), 'exec', dont_inherit=True), module.__dict__)
        return module
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


def vote_label(dataset, prediction, extract):
    text = extract(prediction)
    if dataset == 'USPTO':
        return tuple(sorted({p.strip().lower() for p in text.strip().split('.') if p.strip()}))
    if dataset == 'Symptom2Disease':
        match = re.search(r'\[DIAGNOSIS\](.*?)\[/DIAGNOSIS\]', text, re.I | re.S)
        if match:
            text = match.group(1).strip()
        else:
            match = re.search(r'(?:diagnosis|final diagnosis|conclusion)[:：]\s*([^\n]+)', text, re.I)
            if match:
                text = match.group(1).strip()
        return re.sub(r'[.!?]+$', '', re.sub(r'\s+', ' ', text.lower().strip()))
    if dataset == 'LawBench':
        text = text.strip()
        match = re.search(r'\[罪名\](.*?)(?:<eoa>|$)', text)
        if match:
            text = match.group(1).strip()
        elif '罪名:' in text:
            text = text.split('罪名:')[-1]
        text = re.sub(r'<eoa>.*', '', text).strip()
        for sep in [';', '；', ',', '，', '、']:
            if sep in text:
                return tuple(sorted({p.strip() for p in text.split(sep) if p.strip()}))
        return (text,) if text else ()
    raise ValueError('unsupported control dataset')


def final_evals(rows, split='test'):
    sidecar = parse_sidecar(rows)
    if sidecar['problems'] or sidecar['done'] is None:
        raise ValueError('control sidecar incomplete')
    final = {}
    for row in sidecar['evals']:
        if row['split'] != split:
            continue
        index = row['index']
        if index is None or row.get('ambiguous') or row.get('prediction_chars') != len(row['prediction']):
            raise ValueError('control prediction missing, ambiguous or truncated')
        old = final.get(index)
        if old is None or (row['pass'], row['seq']) > (old['pass'], old['seq']):
            final[index] = row
    return final


def aggregate(dataset, samples, grader, *, expected_count):
    if len(samples) != PLAN['k']:
        raise ValueError('all five samples required')
    indices = set(range(expected_count))
    if any(set(s) != indices for s in samples):
        raise ValueError('sample task membership differs')
    evaluator = grader.get_evaluator(dataset)
    rows = []
    for index in sorted(indices):
        group = [sample[index] for sample in samples]
        if any((row['example_sha256'], row['target_sha256']) !=
               (group[0]['example_sha256'], group[0]['target_sha256']) for row in group):
            raise ValueError('sample task or target identity differs')
        labels = [vote_label(dataset, row['prediction'], grader.extract_final_answer) for row in group]
        counts = Counter(labels)
        best = max(counts.values())
        chosen = next(i for i, label in enumerate(labels) if counts[label] == best)
        raw = evaluator(group[chosen]['prediction'], group[chosen]['target'])
        correct = raw['was_correct'] if isinstance(raw, dict) else raw
        if bool(correct) != group[chosen]['was_correct']:
            raise ValueError('host grader disagrees with worker sidecar')
        rows.append({'index': index, 'example_sha256': group[0]['example_sha256'],
                     'selected_sample': chosen + 1, 'votes': best,
                     'tied': sum(n == best for n in counts.values()) > 1,
                     'sample_prediction_sha256': [r['prediction_sha256'] for r in group],
                     'sample_correct': [r['was_correct'] for r in group], 'was_correct': bool(correct)})
    correct = sum(r['was_correct'] for r in rows)
    return {'dataset': dataset, 'correct': correct, 'total': expected_count,
            'accuracy': correct / expected_count, 'rows': rows}
