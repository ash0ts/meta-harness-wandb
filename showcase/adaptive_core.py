# SPDX-License-Identifier: MIT
"""Frozen policy semantics and input validation for the live stopping experiment."""
import ast
from collections import Counter
import copy
import json
from pathlib import Path
import random
import re
import threading
import time

from mhforge import controls, util

PLAN = {
    'schema': 'mhforge.adaptive-protocol/1',
    'policies': ['parallel5', 'stop3_else5'],
    'temperatures': [0, 1], 'repeats': [1, 2],
    'tasks_per_block': 16, 'concurrent_tasks': 4, 'concurrent_calls': 20,
    'order_seed': 20261112, 'bootstrap_seed': 20261113, 'bootstrap_resamples': 10000,
    'primary_temperature': 0,
    'stopping': 'Stop after three successful, nonempty, equal normalized labels; otherwise request two more. A transport failure makes the policy unsuccessful.',
    'selection': 'Normalized-label plurality; earliest sample position breaks ties. No target access during selection.',
    'latency': 'Admission to policy worker through answer selection; includes prompt-copy, gateway queue, provider retry/backoff and second batch; excludes waiting for a task-worker slot and export.',
    'load': 'Four admitted tasks at a time within a single-policy block, at most twenty simultaneous provider calls; no competing experimental policy block.',
    'failure': 'Keep every attempt. Transport-exhausted policies score zero and have a separate failure rate. Started policies are never relaunched after a host interruption; their latency is unavailable. Resume only unstarted tasks.',
    'pause': 'Heartbeat every second; gaps exceeding 10 seconds or wall/monotonic disagreement exceeding 5 seconds flag overlapping policies. Report all and unflagged latency separately.',
    'population': '412 previously inspected released tasks; new model calls, not an untouched test or optimizer result.',
    'inference': 'Same capped large-context prompt, solver and native graders as engineering E; no response reuse across policies, repetitions, temperatures or sample positions.',
    'execution': 'Reviewed fixed Python orchestration on host through the existing metered gateway; no model-generated code, cloud sandbox or worker filesystem/network isolation claim.',
}


def digest(value):
    return util.sha256_bytes(util.canonical_json(value))


def choose(labels):
    counts = Counter(labels)
    top = max(counts.values())
    return next(i for i, label in enumerate(labels) if counts[label] == top)


def can_stop(samples):
    return (len(samples) == 3 and all(s['ok'] and s['label'] for s in samples)
            and samples[0]['label'] == samples[1]['label'] == samples[2]['label'])


def execute_policy(policy, sample_fn):
    """sample_fn receives ordered positions, runs them concurrently, returns in that order."""
    if policy not in PLAN['policies']:
        raise ValueError('unknown policy')
    first = [1, 2, 3, 4, 5] if policy == 'parallel5' else [1, 2, 3]
    samples = sample_fn(first)
    if [s['position'] for s in samples] != first:
        raise ValueError('sample order changed')
    if not all(s['ok'] for s in samples):
        return samples, None, 'provider_failure'
    if policy == 'stop3_else5' and not can_stop(samples):
        more = sample_fn([4, 5])
        if [s['position'] for s in more] != [4, 5]:
            raise ValueError('second-batch order changed')
        samples += more
    if not all(s['ok'] for s in samples):
        return samples, None, 'provider_failure'
    return samples, choose([s['label'] for s in samples]), 'complete'


def schedule(tasks):
    """Randomized adjacent policy blocks; pair membership fixed without using outcomes."""
    rng = random.Random(PLAN['order_seed'])
    pairs = []
    for repeat in PLAN['repeats']:
        for temperature in PLAN['temperatures']:
            for dataset in sorted(tasks):
                indices = sorted(int(x) for x in tasks[dataset])
                rng.shuffle(indices)
                for ordinal, start in enumerate(range(0, len(indices), PLAN['tasks_per_block'])):
                    policies = PLAN['policies'].copy()
                    rng.shuffle(policies)
                    pair = f'{dataset}.t{temperature}.r{repeat}.b{ordinal:03}'
                    pairs.append([{'block_id': pair+'.'+policy, 'pair_id': pair, 'dataset': dataset,
                                   'temperature': temperature, 'repeat': repeat, 'policy': policy,
                                   'indices': indices[start:start+PLAN['tasks_per_block']]}
                                  for policy in policies])
    rng.shuffle(pairs)
    return [block for pair in pairs for block in pair]


def request_for(task, temperature):
    request = copy.deepcopy(task['request'])
    request['temperature'] = temperature
    return request


def load_extract(paths):
    """Compile exactly the pinned parsing function; do not import the upstream agent package."""
    p = paths.ws/'memory_system.py'
    pinned = util.read_json(paths.pristine)['files']
    if util.sha256_file(p) != pinned['memory_system.py']:
        raise ValueError('answer extractor changed')
    tree = ast.parse(p.read_text())
    function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'extract_json_field')
    scope = {'json': json, 're': re}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(p), 'exec'), scope)
    return scope['extract_json_field']


class RecordingIds(util.IdSource):
    """Associate a synchronous gateway call with its thread without changing the gateway."""
    def __init__(self):
        super().__init__()
        self.local = threading.local()

    def call_id(self):
        value = super().call_id()
        self.local.call_id = value
        return value


class Heartbeat:
    def __init__(self, path):
        self.path = Path(path)
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def run(self):
        wall, mono = time.time(), time.monotonic()
        while not self.stop_event.wait(1):
            now, tick = time.time(), time.monotonic()
            if now-wall > 10 or tick-mono > 10 or abs((now-wall)-(tick-mono)) > 5:
                with self.path.open('a') as f:
                    f.write(json.dumps({'start_epoch': wall, 'end_epoch': now,
                                        'wall_gap_s': now-wall, 'monotonic_gap_s': tick-mono})+'\n')
            wall, mono = now, tick

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stop_event.set()
        self.thread.join()


def runtime_hashes(root):
    root = Path(root)
    paths = [root/name for name in ('adaptive_core.py','run_adaptive.py','prepare_adaptive.py','adaptive_export.py','analyze_adaptive.py')]
    paths += sorted((root/'mhforge').rglob('*.py'))
    return {str(p.relative_to(root)): util.sha256_file(p) for p in paths}


def verify_inputs(paths, root):
    contract = util.read_json(paths.root/'adaptive/contract.json')
    if contract['plan'] != PLAN or contract['runtime_hashes'] != runtime_hashes(root):
        raise ValueError('frozen adaptive runtime or protocol differs')
    packed = paths.root/'adaptive/inputs-private.json'
    if util.sha256_file(packed) != contract['inputs_sha256']:
        raise ValueError('frozen input pack differs')
    inputs = util.read_json(packed)
    for dataset, tasks in inputs['tasks'].items():
        for index, task in tasks.items():
            if digest(task['request']) != task['request_sha256']:
                raise ValueError('request digest differs')
            if not isinstance(task['target'], str):
                raise ValueError('target is not text')
    if schedule(inputs['tasks']) != contract['schedule']:
        raise ValueError('frozen schedule differs')
    return contract, inputs
