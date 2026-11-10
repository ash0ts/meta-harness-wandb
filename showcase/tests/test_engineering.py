# SPDX-License-Identifier: MIT
import sys
import types
import unittest
from pathlib import Path

from mhforge.engineering import candidate_source, vote_indices


class EngineeringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Use the real memory interface and extraction code from the pinned worker.
        import os
        p = Path(os.environ.get('MHFORGE_EXAMPLE_DIR', Path(__file__).resolve().parents[2] / 'reference_examples' / 'text_classification')).parent
        sys.path.insert(0, str(p))

    def agent(self, limit):
        module = types.ModuleType('text_classification.agents.engineering_test')
        module.__package__ = 'text_classification.agents'
        exec(candidate_source(limit), module.__dict__)
        return module.EngineeringContext(lambda prompt: '{"final_answer":"ok"}')

    def test_nested_examples_and_no_labels_in_zero_context(self):
        agents = [self.agent(n) for n in (0, 5, 9999)]
        training = [{'input':f'item-{i}', 'ground_truth':f'label-{i}'} for i in range(12)]
        for a in agents:
            a.learn_from_batch(training)
        zero, small, large = [a.format_examples('unseen-input') for a in agents]
        self.assertEqual(zero, ('', 0))
        self.assertEqual(small[1], 5)
        self.assertEqual(large[1], 12)
        self.assertTrue(large[0].startswith(small[0]+'\n\n'))
        self.assertEqual(small, agents[1].format_examples('unseen-input'))
        restored = self.agent(5); restored.set_state(agents[1].get_state())
        self.assertEqual(small, restored.format_examples('unseen-input'))
        agents[0].predict('unseen-input')
        self.assertNotIn('label-', agents[0].get_last_prompt_info()['prompt_text'])

    def test_character_cap_and_reported_count_are_actual(self):
        a = self.agent(9999)
        a.learn_from_batch([{'input':'x'*10000, 'ground_truth':'y'} for _ in range(8)])
        text, n = a.format_examples('q')
        self.assertLessEqual(len(text), 30000)
        self.assertEqual(n, 2)
        _, meta = a.predict('q')
        self.assertEqual(meta['num_examples'], n)
        self.assertEqual(meta['example_chars'], len(text))

    def test_vote_prefix_and_earliest_tie(self):
        labels = ['a', 'b', 'b', 'a', 'c']
        self.assertEqual([vote_indices(labels,k) for k in (1,3,5)], [0,1,0])
        self.assertEqual(vote_indices(['x','y','z'],3),0)

    def test_score_bundle_preserves_task_weight_and_vote_tradeoff(self):
        from engineering_stats import summarize
        rows = []
        for system in ('context_0','context_5','context_all','context_all_t1'):
            for ds,n in [('USPTO',2),('Symptom2Disease',4),('LawBench',2)]:
                for i in range(n):
                    for sample in range(1,6):
                        rows.append(dict(system=system,dataset=ds,index=i,sample=sample,
                            correct=int(sample>1),label_class=int(sample>1),empty=0,
                            strict_json_valid=1,examples=0 if system=='context_0' else 5))
        result = summarize(rows)
        self.assertEqual(result['unique_tasks'],8)
        self.assertEqual(result['systems']['context_all']['mean_accuracy_percent'],80)
        self.assertEqual(result['systems']['context_all']['vote_accuracy_percent'],{'1':0.,'3':100.,'5':100.})
        self.assertEqual(result['paired']['context_all_vote3_minus_vote1']['ci95_pp'],[100.,100.])
        with self.assertRaises(ValueError): summarize(rows[:-1],bootstrap=False)
