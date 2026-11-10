# SPDX-License-Identifier: MIT
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from mhforge import controls, util
from tests import helpers


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        ws = helpers.example_dir()
        pins = {n: util.sha256_file(ws / n) for n in ('data/constants.py', 'data/evaluators.py')}
        util.write_json(self.root / 'pristine.json', {'files': pins})
        self.grader = controls.load_grader(SimpleNamespace(ws=ws, pristine=self.root / 'pristine.json'))

    def row(self, prediction, target='flu'):
        return {'example_sha256': 'a' * 64, 'target_sha256': util.sha256_bytes(target.encode()),
                'prediction_sha256': util.sha256_bytes(prediction.encode()), 'prediction': prediction,
                'target': target, 'was_correct': self.grader.eval_symptom2disease(prediction, target)}

    def test_normalization_matches_released_evaluator_semantics(self):
        examples = [('USPTO', 'C.CO', 'co.c'), ('LawBench', '[罪名]盗窃，抢劫<eoa>', '抢劫，盗窃'),
                    ('Symptom2Disease', '{"final_answer":"[DIAGNOSIS]Flu![/DIAGNOSIS]"}', 'flu')]
        for ds, a, b in examples:
            self.assertEqual(controls.vote_label(ds, a, self.grader.extract_final_answer),
                             controls.vote_label(ds, b, self.grader.extract_final_answer))

    def test_majority_is_regraded_and_ties_use_earliest_sample(self):
        samples = [{0: self.row(p)} for p in ['cold', 'flu', '[DIAGNOSIS]Flu[/DIAGNOSIS]', 'cold', 'other']]
        result = controls.aggregate('Symptom2Disease', samples, self.grader, expected_count=1)
        self.assertEqual(result['correct'], 0)
        self.assertTrue(result['rows'][0]['tied'])
        self.assertEqual(result['rows'][0]['selected_sample'], 1)
        samples[-1] = {0: self.row('flu.')}
        result = controls.aggregate('Symptom2Disease', samples, self.grader, expected_count=1)
        self.assertEqual(result['correct'], 1)
        self.assertEqual(result['rows'][0]['votes'], 3)

    def test_missing_sample_and_identity_drift_fail(self):
        samples = [{0: self.row('flu')} for _ in range(5)]
        with self.assertRaises(ValueError):
            controls.aggregate('Symptom2Disease', samples[:4], self.grader, expected_count=1)
        samples[-1][0]['example_sha256'] = 'b' * 64
        with self.assertRaises(ValueError):
            controls.aggregate('Symptom2Disease', samples, self.grader, expected_count=1)

    def test_grader_disagreement_fails(self):
        samples = [{0: self.row('flu')} for _ in range(5)]
        samples[0][0]['was_correct'] = False
        with self.assertRaises(ValueError):
            controls.aggregate('Symptom2Disease', samples, self.grader, expected_count=1)
