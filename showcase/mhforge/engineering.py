# SPDX-License-Identifier: MIT
"""Frozen, opt-in context-size and inference-budget study; no adaptive search."""
from __future__ import annotations

from dataclasses import replace
import random

from . import controls, jobs, qualification, util
from .errors import CampaignStop

ARMS = {'context_0': (0, 0.0), 'context_5': (5, 0.0),
        'context_all': (9999, 0.0), 'context_all_t1': (9999, 1.0)}
PLAN = {'schema': 'mhforge.engineering/1', 'arms': ARMS, 'samples': 5,
        'max_example_chars': 30000, 'sample_counts': [1, 3, 5],
        'order_seed': 20261111, 'split': 'test',
        'selection': 'most frequent normalized answer; earliest sample breaks ties',
        'primary_context': 'paired per-task mean correctness over five temperature-zero draws',
        'primary_sampling': 'first 1, 3, 5 draws of context_all at temperature zero',
        'secondary_sampling': 'first 1, 3, 5 draws of context_all_t1 at temperature one',
        'test_status': 'previously inspected released test; prospective follow-up, not untouched generalization',
        'prompt': 'common neutral instructions; examples are a nested prefix of one input-hash shuffle',
        'latency': 'record individual request latency and queue delay; no simulated policy latency claim',
        'missingness': 'preserve scored failures; stop incomplete jobs; retry transport under manifest policy',
        'bootstrap': 'paired tasks stratified by dataset, keeping all draws of each task together; 10000 resamples, seed 20261111'}

SOURCE = '''# SPDX-License-Identifier: MIT
import random
from . import fewshot_memory as fm
from ..memory_system import extract_json_field

LIMIT = __LIMIT__
PROMPT = """Solve the problem below. Reference examples, when present, demonstrate the task and answer conventions.

Reference examples:
{examples}

Problem:
{problem}

Return a JSON object with \\"reasoning\\" and \\"final_answer\\". Put only the concise answer in \\"final_answer\\".
"""

class EngineeringContext(fm.FewShotMemory):
    def __init__(self, llm):
        super().__init__(llm, max_examples=9999)

    def format_examples(self, problem):
        examples = list(self.examples)
        random.Random(fm._seed_for_input(problem)).shuffle(examples)
        parts, chars = [], 0
        for ex in examples[:LIMIT]:
            part = "Q: " + ex.get("raw_question", ex["input"]) + "\\nA: " + ex["target"]
            extra = len(part) + (2 if parts else 0)
            if chars + extra > 30000:
                break
            parts.append(part)
            chars += extra
        return "\\n\\n".join(parts), len(parts)

    def get_context_length(self):
        # Representative stored-context size; predict records the actual per-query size.
        return len(self.format_examples("")[0])

    def predict(self, input):
        examples, count = self.format_examples(input)
        response = self.call_llm(PROMPT.format(examples=examples, problem=input))
        return extract_json_field(response, "final_answer"), {
            "full_response": response, "num_examples": count,
            "example_chars": len(examples)}
'''


def candidate_source(limit):
    return SOURCE.replace('__LIMIT__', str(limit))


def vote_indices(labels, k):
    """Return the earliest sample among tied plurality winners; never inspect truth."""
    from collections import Counter
    counts = Counter(labels[:k])
    best = max(counts.values())
    return next(i for i, label in enumerate(labels[:k]) if counts[label] == best)


def run(driver):
    r, m = driver.rt, driver.rt.manifest
    def action(up, runner):
        root = r.paths.root / 'engineering'
        plan = dict(PLAN, arms={k: list(v) for k, v in ARMS.items()},
                    engine_sha256=qualification.engine_snapshot(__import__('pathlib').Path(__file__).parent)['engine_sha256'])
        path = root / 'plan.json'
        if path.exists() and util.read_json(path) != plan:
            raise CampaignStop(reason='upstream_contract', detail='engineering plan changed; use new campaign')
        util.write_json(path, plan, fsync=True)
        for name, (limit, _) in ARMS.items():
            p = r.paths.ws / 'agents' / (name + '.py')
            content = candidate_source(limit)
            if p.exists() and p.read_text() != content:
                raise CampaignStop(reason='upstream_contract', detail='engineering candidate changed')
            p.write_text(content)
        r.journal.record('engineering', m['campaign'], 'started', detail={'plan_sha256': util.sha256_file(path)})
        # The upstream offline learning operation only stores released training examples.
        # All four arms run; validation scores never select an arm or prompt.
        for name in ARMS:
            planned, error, skipped = jobs.plan_jobs(runner.facade, name, test=False,
                logs_dir=driver.logs, results_dir=driver.logs/'results', manifest=m)
            if error or skipped:
                raise CampaignStop(reason='qualification_failed', detail='engineering preparation missing')
            for p in planned:
                driver._halt()
                if driver.stop_requested:
                    raise CampaignStop(reason='interrupted')
                record = runner.run_job(p, phase='controls', iteration=None, publish=True)
                if record['status'] != 'succeeded':
                    raise CampaignStop(reason='qualification_failed', detail='engineering preparation incomplete')
            # Upstream omits already usable validation jobs on resume. Verify the
            # retained job records instead of requiring those jobs to be replanned.
            for ds in m['upstream']['expect']['datasets']:
                saved = r.paths.job_dir(f'val.{name}.{ds}') / 'job.json'
                if not saved.exists() or util.read_json(saved)['status'] != 'succeeded':
                    raise CampaignStop(reason='qualification_failed', detail='engineering preparation evidence missing')
        schedule = []
        for name, (_, temperature) in ARMS.items():
            planned, error, skipped = jobs.plan_jobs(runner.facade, name, test=True,
                logs_dir=driver.logs, results_dir=r.paths.scratch_root/'engineering-results', manifest=m)
            if error or skipped or len(planned) != len(m['upstream']['expect']['datasets']):
                raise CampaignStop(reason='qualification_failed', detail='engineering test inputs missing')
            for p in planned:
                argv = list(p.inner_argv)
                argv[argv.index('--temperature')+1] = str(temperature)
                p = replace(p, inner_argv=tuple(argv))
                schedule.extend((p, sample) for sample in range(1, PLAN['samples']+1))
        random.Random(PLAN['order_seed']).shuffle(schedule)
        util.write_json(root/'schedule.json', [{'candidate':p.candidate, 'dataset':p.dataset, 'sample':s}
                                             for p,s in schedule], fsync=True)
        evidence = []
        for p, sample in schedule:
            driver._halt()
            if driver.stop_requested:
                raise CampaignStop(reason='interrupted')
            record = runner.run_job(p, phase='controls', iteration=None, publish=False, sample=sample)
            if record['status'] != 'succeeded':
                raise CampaignStop(reason='qualification_failed', detail='engineering sample incomplete')
            a = next(a for a in reversed(record['attempts']) if a.get('outcome') == 'ok')
            sidecar = r.paths.attempt_dir(record['job_key'], a['attempt'])/'out/sidecar.jsonl'
            final = controls.final_evals(util.read_jsonl(sidecar))
            n = m['upstream']['expect']['sizes'][p.dataset][2]
            if set(final) != set(range(n)):
                raise CampaignStop(reason='qualification_failed', detail='engineering task membership differs')
            evidence.append({'job_key':record['job_key'], 'attempt':a['attempt'],
                             'sidecar_sha256':util.sha256_file(sidecar)})
        result = {'schema':'mhforge.engineering-result/1', 'plan':plan, 'evidence':evidence,
                  'test_jobs':len(evidence), 'test_outcomes':sum(m['upstream']['expect']['sizes'][p.dataset][2] for p,s in schedule)}
        util.write_json(root/'result.json', result, fsync=True)
        r.journal.record('engineering', m['campaign'], 'complete')
        return {'status':'complete', 'test_jobs':result['test_jobs'], 'test_outcomes':result['test_outcomes']}
    return driver._execute('engineering', action)
