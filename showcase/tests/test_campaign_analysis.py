# SPDX-License-Identifier: MIT
import copy
import json
import tempfile
import unittest
from pathlib import Path
import analyze_campaign as analysis
from tests.helpers import make_manifest


class PairedAnalysisTests(unittest.TestCase):
    def arm(self, flags):
        return {'small': {i: {'example_sha256': str(i), 'was_correct': flag}
                          for i, flag in enumerate(flags)}}

    def test_equal_arms_have_exact_zero_interval(self):
        arm = self.arm([False, True, True])
        result = analysis.paired(arm, arm, repeats=200)
        self.assertEqual(result['macro_delta_ci95'], [0.0, 0.0])
        self.assertEqual(result['datasets']['small']['both_correct'], 2)

    def test_gains_and_losses_are_paired_not_independent(self):
        result = analysis.paired(self.arm([True, False, False, True]),
                                 self.arm([False, True, False, True]), repeats=200)
        self.assertEqual(result['macro_delta'], 0)
        self.assertEqual([result['datasets']['small'][k] for k in
                          ('gain', 'loss', 'both_correct', 'both_wrong')], [1, 1, 1, 1])

    def test_macro_uses_equal_dataset_weights(self):
        left = self.arm([False])
        right = self.arm([True])
        left['large'] = right['large'] = {i: {'example_sha256': str(i), 'was_correct': False} for i in range(100)}
        result = analysis.paired(left, right, repeats=200)
        self.assertEqual(result['macro_delta'], .5)
        self.assertEqual(result['macro_delta_ci95'], [.5, .5])

    def test_missing_outcome_or_different_identity_stops(self):
        for value in (None, 0):
            with self.assertRaises(ValueError):
                analysis.paired(self.arm([True]), self.arm([value]), repeats=2)
        left, right = self.arm([True]), self.arm([True])
        right['small'][0]['example_sha256'] = 'other'
        with self.assertRaises(ValueError):
            analysis.paired(left, right, repeats=2)

    def test_campaign_names_can_differ_but_solver_and_data_cannot(self):
        left = make_manifest()
        right = copy.deepcopy(left); right['campaign'] = 'b'
        analysis.compatible_campaigns(left, right)
        right['provider']['wire_model'] = 'other'
        with self.assertRaisesRegex(ValueError, 'provider.wire_model'):
            analysis.compatible_campaigns(left, right)
        right = copy.deepcopy(left); right['upstream']['tree_sha256'] = 'changed'
        with self.assertRaisesRegex(ValueError, 'upstream.tree_sha256'):
            analysis.compatible_campaigns(left, right)

    def test_overlap_file_index_is_mapped_to_evaluation_order(self):
        # Frozen released overlap: file row 140 is evaluation occurrence 11.
        self.assertEqual(analysis.evaluation_indices([140], 212, 42), [11])
        self.assertEqual(analysis.evaluation_indices([], 100, 42), [])
        for indices in ([212], [-1], [140, 140], [True]):
            with self.assertRaises(ValueError):
                analysis.evaluation_indices(indices, 212, 42)

    def test_identity_uses_the_released_loader_target_normalization(self):
        from mhforge import util
        expected = util.sha256_bytes('question\0盗窃'.encode())
        self.assertEqual(analysis.source_identity('LawBench', {'question':'question', 'answer':'罪名: 盗窃 '}), expected)
        self.assertEqual(analysis.source_identity('USPTO', {'question':'question', 'target':'盗窃'}), expected)
        with self.assertRaises(ValueError):
            analysis.source_identity('other', {'question':'question','answer':'盗窃'})

    def test_reordered_or_modified_source_identity_is_rejected(self):
        from mhforge import util
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);ws=root/'workspace/text_classification'
            names=('data/api.py','data/loaders.py','data/symptom_diagnosis/test.jsonl')
            rows=[{'question':'first','answer':'one'},{'question':'second','answer':'two'}]
            for rel in names:
                path=ws/rel;path.parent.mkdir(parents=True,exist_ok=True)
                path.write_text('# fixture\n')
            (ws/names[-1]).write_text(''.join(json.dumps(row)+'\n' for row in rows))
            util.write_json(root/'workspace/pristine.json',{'files':{n:util.sha256_file(ws/n) for n in names}})
            m={'upstream':{'expect':{'seed':42,'dataset_dirs':{'Symptom2Disease':'symptom_diagnosis'}}}}
            arm={'Symptom2Disease':{i:{'example_sha256':analysis.source_identity('Symptom2Disease',r)}
                                   for i,r in enumerate(reversed(rows))}}
            analysis.verify_source_identities(root,m,arm)
            arm['Symptom2Disease'][0],arm['Symptom2Disease'][1]=arm['Symptom2Disease'][1],arm['Symptom2Disease'][0]
            with self.assertRaisesRegex(ValueError,'shuffled released task identity'):
                analysis.verify_source_identities(root,m,arm)
            (ws/names[-1]).write_text('{}\n')
            with self.assertRaisesRegex(ValueError,'released test data changed'):
                analysis.verify_source_identities(root,m,arm)


if __name__ == '__main__':
    unittest.main()
